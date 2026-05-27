#!/usr/bin/env python3
"""
Compare two runs' outputs to validate the warehouse path against the --combined
reference. Dev/validation tool — not part of the production pipeline.

Usage:
    # Tier CSVs (compare two files, keyed by a column):
    python3 deploy/compare_outputs.py csv OLD.csv NEW.csv --key Account_Name

    # All tier_updates_*.csv in two dirs (auto-pairs latest in each):
    python3 deploy/compare_outputs.py tiers OLD_DIR NEW_DIR

    # Dashboard JSON dirs (compare every *.json present in both):
    python3 deploy/compare_outputs.py json OLD_DIR NEW_DIR

Comparison rules:
  - Floats / money: equal within --tol (default 0.01).
  - Ints / strings / tier labels: exact.
  - Reports the first --max mismatches per column, plus row-count and
    key-set differences.

Exit code 0 = equivalent within tolerance, 1 = differences found.
"""

import argparse
import glob
import json
import math
import os
import sys

import pandas as pd


def _vals_equal(a, b, tol):
    if pd.isna(a) and pd.isna(b):
        return True
    if pd.isna(a) or pd.isna(b):
        return False
    if isinstance(a, float) or isinstance(b, float):
        try:
            return math.isclose(float(a), float(b), abs_tol=tol)
        except (TypeError, ValueError):
            return str(a) == str(b)
    return a == b


def compare_frames(old: pd.DataFrame, new: pd.DataFrame, key: str, tol: float,
                   maxshow: int, label: str) -> bool:
    ok = True
    print(f"\n=== {label} ===")
    if key not in old.columns or key not in new.columns:
        # Fall back to positional compare if no key column.
        print(f"  (no key column {key!r}; comparing row-aligned)")
        old = old.reset_index(drop=True)
        new = new.reset_index(drop=True)
        if len(old) != len(new):
            print(f"  ROW COUNT differs: old={len(old)} new={len(new)}")
            return False
        old.index.name = new.index.name = "_row"
        oidx, nidx = old, new
    else:
        oidx = old.set_index(key)
        nidx = new.set_index(key)
        ok_keys = set(oidx.index)
        nk_keys = set(nidx.index)
        only_old = ok_keys - nk_keys
        only_new = nk_keys - ok_keys
        if only_old:
            print(f"  KEYS only in OLD ({len(only_old)}): {list(only_old)[:maxshow]}")
            ok = False
        if only_new:
            print(f"  KEYS only in NEW ({len(only_new)}): {list(only_new)[:maxshow]}")
            ok = False
        common = sorted(ok_keys & nk_keys, key=str)
        oidx = oidx.loc[common]
        nidx = nidx.loc[common]

    cols = [c for c in oidx.columns if c in nidx.columns]
    only_old_cols = [c for c in oidx.columns if c not in nidx.columns]
    only_new_cols = [c for c in nidx.columns if c not in oidx.columns]
    if only_old_cols:
        print(f"  COLUMNS only in OLD: {only_old_cols}")
        ok = False
    if only_new_cols:
        print(f"  COLUMNS only in NEW: {only_new_cols}")
        ok = False

    for col in cols:
        mism = []
        for k in oidx.index:
            a = oidx.at[k, col]
            b = nidx.at[k, col]
            # at[] can return a Series if duplicate index; coerce.
            if isinstance(a, pd.Series):
                a = a.iloc[0]
            if isinstance(b, pd.Series):
                b = b.iloc[0]
            if not _vals_equal(a, b, tol):
                mism.append((k, a, b))
        if mism:
            ok = False
            print(f"  COLUMN {col!r}: {len(mism)} mismatch(es); first {min(maxshow, len(mism))}:")
            for k, a, b in mism[:maxshow]:
                print(f"      {k}: old={a!r} new={b!r}")
    if ok:
        print(f"  OK — {len(cols)} columns match within tol={tol}")
    return ok


def _latest(dirpath, pattern):
    files = sorted(glob.glob(os.path.join(dirpath, pattern)))
    return files[-1] if files else None


def cmd_csv(args):
    old = pd.read_csv(args.old)
    new = pd.read_csv(args.new)
    return compare_frames(old, new, args.key, args.tol, args.max,
                          f"{os.path.basename(args.old)} vs {os.path.basename(args.new)}")


def cmd_tiers(args):
    old = _latest(args.old_dir, "tier_updates_*.csv")
    new = _latest(args.new_dir, "tier_updates_*.csv")
    if not old or not new:
        print(f"Could not find tier_updates_*.csv in both dirs (old={old}, new={new})")
        return False
    a = compare_frames(pd.read_csv(old), pd.read_csv(new), "Account_Name",
                       args.tol, args.max, "tier_updates")
    # Industry summary too, if present.
    ois = _latest(args.old_dir, "industry_summary_*.csv")
    nis = _latest(args.new_dir, "industry_summary_*.csv")
    b = True
    if ois and nis:
        b = compare_frames(pd.read_csv(ois), pd.read_csv(nis), "Industry",
                           args.tol, args.max, "industry_summary")
    return a and b


def _flatten_json(path):
    """Load a JSON file into a frame if it's a list of records, else a 1-row
    frame of scalars. Returns (df, key_guess)."""
    with open(path) as f:
        data = json.load(f)
    if isinstance(data, list) and data and isinstance(data[0], dict):
        df = pd.DataFrame(data)
        for k in ("AccountId", "account_id", "date", "month", "EventId", "tier"):
            if k in df.columns:
                return df, k
        return df, df.columns[0]
    if isinstance(data, dict):
        # dict of records or scalar map; normalise to rows
        if data and all(isinstance(v, dict) for v in data.values()):
            df = pd.DataFrame.from_dict(data, orient="index").reset_index().rename(columns={"index": "_key"})
            return df, "_key"
        return pd.DataFrame([data]), None
    return pd.DataFrame([{"value": data}]), None


def cmd_json(args):
    old_files = {os.path.basename(p) for p in glob.glob(os.path.join(args.old_dir, "*.json"))}
    new_files = {os.path.basename(p) for p in glob.glob(os.path.join(args.new_dir, "*.json"))}
    only_old = old_files - new_files
    only_new = new_files - old_files
    if only_old:
        print(f"JSON files only in OLD: {sorted(only_old)}")
    if only_new:
        print(f"JSON files only in NEW: {sorted(only_new)}")
    ok = not (only_old or only_new)
    for name in sorted(old_files & new_files):
        if name == "metadata.json":
            continue  # timestamps differ by design
        odf, key = _flatten_json(os.path.join(args.old_dir, name))
        ndf, _ = _flatten_json(os.path.join(args.new_dir, name))
        ok = compare_frames(odf, ndf, key or "_row", args.tol, args.max, name) and ok
    return ok


def cmd_dedupe(args):
    """Spot-check that the warehouse upsert survivor equals the combined-pickle
    keep='last' survivor for BookingTransactionIds present in both feeds.

    Loads the combined pickle (built by `prepare_data.py --combined`, or
    load_combined_booking_data) and the warehouse, samples shared IDs, and
    compares the surviving row's key fields. Run on the Mac where the pickle
    fits in memory.
    """
    import pickle
    from modules import warehouse as w

    with open(args.pickle, "rb") as f:
        combined = pickle.load(f)
    conn = w.connect(args.db)

    key = "BookingTransactionId"
    pk_ids = set(pd.to_numeric(combined[key], errors="coerce").dropna().astype("int64").tolist())
    wh_ids = pd.read_sql_query(f"SELECT {key} FROM bookings", conn)
    wh_ids = set(pd.to_numeric(wh_ids[key], errors="coerce").dropna().astype("int64").tolist())

    shared = sorted(pk_ids & wh_ids)
    print(f"Combined rows: {len(combined):,}  Warehouse rows: {len(wh_ids):,}  Shared IDs: {len(shared):,}")
    if not shared:
        print("No shared IDs to check.")
        return True

    import random
    sample = random.sample(shared, min(args.sample, len(shared)))
    placeholders = ",".join("?" * len(sample))
    wh_rows = pd.read_sql_query(
        f"SELECT * FROM bookings WHERE {key} IN ({placeholders})", conn, params=sample
    ).set_index(key)
    combined_idx = combined.copy()
    combined_idx[key] = pd.to_numeric(combined_idx[key], errors="coerce").astype("int64")
    combined_idx = combined_idx.drop_duplicates(subset=[key], keep="last").set_index(key)

    check_cols = [c for c in ("PaymentReceived", "TicketQuantity", "Status",
                              "TransactionDate", "TicketFee") if c in wh_rows.columns and c in combined_idx.columns]
    ok = True
    mism = 0
    for tid in sample:
        for c in check_cols:
            a = combined_idx.at[tid, c]
            b = wh_rows.at[tid, c]
            if c == "TransactionDate":
                a = str(pd.to_datetime(a, utc=True)); b = str(pd.to_datetime(b, utc=True))
            if not _vals_equal(a, b, args.tol):
                if mism < args.max:
                    print(f"  ID {tid} col {c}: pickle={a!r} warehouse={b!r}")
                mism += 1
                ok = False
    if ok:
        print(f"OK — {len(sample)} sampled shared IDs have identical survivors across {len(check_cols)} cols")
    else:
        print(f"{mism} field mismatch(es) across sampled survivors")
    return ok


def main():
    p = argparse.ArgumentParser(description="Validate warehouse output vs --combined reference")
    sub = p.add_subparsers(dest="cmd", required=True)

    pd_ = sub.add_parser("dedupe", help="spot-check dedupe survivor equivalence")
    pd_.add_argument("--pickle", required=True, help="path to combined_booking.pkl")
    pd_.add_argument("--db", default=None, help="warehouse.db path (default: WAREHOUSE_DB env)")
    pd_.add_argument("--sample", type=int, default=200, help="number of shared IDs to sample")

    pc = sub.add_parser("csv"); pc.add_argument("old"); pc.add_argument("new")
    pc.add_argument("--key", default="Account_Name")

    pt = sub.add_parser("tiers"); pt.add_argument("old_dir"); pt.add_argument("new_dir")

    pj = sub.add_parser("json"); pj.add_argument("old_dir"); pj.add_argument("new_dir")

    for sp in (pc, pt, pj, pd_):
        sp.add_argument("--tol", type=float, default=0.01, help="abs tolerance for floats")
        sp.add_argument("--max", type=int, default=10, help="max mismatches shown per column")

    args = p.parse_args()
    ok = {"csv": cmd_csv, "tiers": cmd_tiers, "json": cmd_json,
          "dedupe": cmd_dedupe}[args.cmd](args)
    print("\n" + ("ALL EQUIVALENT (within tolerance)" if ok else "DIFFERENCES FOUND"))
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()

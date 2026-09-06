#!/usr/bin/env python3
"""Check that every runtime dependency the pipeline needs is installed & working.

Rebuilds miss packages that only a specific job imports (matplotlib for the
tier-movement chart emails, weasyprint for the commission PDF, google-* for GA4),
so the failure only shows up when that job runs at 02:45 / month-end. This
verifies them all up front.

It does a REAL import of each module (not just a presence check) so it also
catches "installed but broken" cases — e.g. matplotlib importing, or weasyprint
missing its pango/cairo system libraries. It also resolves the DuckDB CLI binary
the same way prepare_data.py does.

Exit code: 0 if every REQUIRED dep (used by a scheduled cron job) is OK,
1 otherwise. OPTIONAL deps (manual/on-demand reports) only warn.

Usage:
    python3 deploy/check_deps.py
    python3 deploy/check_deps.py --all      # treat optional as required too
"""
import argparse
import importlib
import os
import shutil
import subprocess
import sys

# (import_name, pip_package, required, feature)
# required=True  -> used by a scheduled cron job (nightly/weekly/monthly)
# required=False -> manual / on-demand reports only
DEPS = [
    ("boto3",        "boto3",                 True,  "AWS S3 data pull (prepare_data, s3_to_sharepoint)"),
    ("pandas",       "pandas",                True,  "warehouse build + all calcs"),
    ("numpy",        "numpy",                 True,  "tier / metric calcs"),
    ("pytz",         "pytz",                  True,  "timezone handling (imported via modules.utils)"),
    ("dateutil",     "python-dateutil",       True,  "date windows"),
    ("requests",     "requests",              True,  "Zoho / Graph / Vero / Mailshake HTTP"),
    ("msal",         "msal",                  True,  "Azure Graph auth (email, SharePoint)"),
    ("cryptography", "cryptography",          True,  "backup encrypt/decrypt (backup_to_sharepoint, restore)"),
    ("matplotlib",   "matplotlib",            True,  "tier-movement chart emails (zoho_tiers, 02:45)"),
    ("weasyprint",   "weasyprint",            True,  "commission report PDF (monthly, 2nd)"),
    # On-demand / manual reports:
    ("google.analytics.data_v1beta", "google-analytics-data", False, "GA4: PPC + planning reports (manual)"),
    ("yaml",         "PyYAML",                False, "pricing scenario modeller (manual)"),
]


def _check_import(mod):
    """Return (status, detail): 'ok' | 'missing' | 'broken'."""
    try:
        importlib.import_module(mod)
        return "ok", ""
    except ImportError as e:
        # distinguish "module absent" from "module present but a sub-import failed"
        missing = getattr(e, "name", "") or ""
        top = mod.split(".")[0]
        if missing == mod or missing == top or f"No module named '{top}'" in str(e):
            return "missing", str(e)
        return "broken", str(e)   # e.g. weasyprint present but libpango missing
    except Exception as e:
        return "broken", f"{type(e).__name__}: {e}"


def _check_duckdb():
    """Resolve the duckdb CLI the way prepare_data._materialise_duckdb does."""
    binp = (os.environ.get("DUCKDB_BIN")
            or shutil.which("duckdb")
            or next((p for p in ("/usr/local/bin/duckdb", "/usr/bin/duckdb",
                                 "/opt/homebrew/bin/duckdb", "/root/.duckdb/cli/latest/duckdb")
                     if os.path.exists(p)), None))
    if not binp:
        return "missing", "duckdb binary not found (set DUCKDB_BIN or put it on PATH)"
    try:
        out = subprocess.run([binp, "--version"], capture_output=True, text=True, timeout=15)
        ver = (out.stdout or out.stderr).strip().splitlines()[0] if out.returncode == 0 else ""
        if out.returncode == 0:
            return "ok", f"{binp} ({ver})"
        return "broken", f"{binp} exited {out.returncode}"
    except Exception as e:
        return "broken", f"{binp}: {type(e).__name__}: {e}"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--all", action="store_true", help="treat optional deps as required for the exit code")
    args = ap.parse_args()

    tty = sys.stdout.isatty()
    def c(code, s): return f"\033[{code}m{s}\033[0m" if tty else s
    OK, BAD, WARN, DIM = (lambda s: c("32", s)), (lambda s: c("31", s)), (lambda s: c("33", s)), (lambda s: c("2", s))

    print("Checking pipeline dependencies...\n")
    missing_required, missing_optional, broken = [], [], []

    # DuckDB CLI (not a pip package) — required for the nightly materialise.
    status, detail = _check_duckdb()
    mark = {"ok": OK("✓ ok"), "missing": BAD("✗ MISSING"), "broken": BAD("✗ BROKEN")}[status]
    print(f"  {mark:<20} {'duckdb (CLI)':<26} {DIM('nightly materialise — ' + detail)}")
    if status == "missing": missing_required.append("duckdb-cli (install the DuckDB CLI, set DUCKDB_BIN)")
    elif status == "broken": broken.append(("duckdb-cli", detail))

    # Python packages.
    for mod, pip_name, required, feature in DEPS:
        status, detail = _check_import(mod)
        tag = "" if required else DIM(" [optional]")
        mark = {"ok": OK("✓ ok"), "missing": (BAD if required else WARN)("✗ MISSING" if required else "• missing"),
                "broken": BAD("✗ BROKEN")}[status]
        line = f"  {mark:<20} {pip_name:<26} {DIM(feature)}{tag}"
        print(line)
        if status == "broken":
            broken.append((pip_name, detail))
        elif status == "missing":
            (missing_required if required else missing_optional).append(pip_name)

    # Summary + remediation.
    print()
    if broken:
        print(BAD("  Installed but broken (import failed):"))
        for name, d in broken:
            print(f"    - {name}: {d[:160]}")
    if missing_required:
        print(BAD("  Missing REQUIRED: ") + ", ".join(missing_required))
    if missing_optional:
        print(WARN("  Missing optional: ") + ", ".join(missing_optional))

    pip_missing = [m for m in missing_required if not m.startswith("duckdb-cli")] + (missing_optional if args.all else [])
    pip_missing += [n for n, _ in broken if n != "duckdb-cli"]
    if pip_missing:
        print("\n  Install with:")
        print("    pip install --break-system-packages " + " ".join(sorted(set(pip_missing))))
        if any(n == "weasyprint" for n in pip_missing):
            print(DIM("    (weasyprint also needs system libs: libpango-1.0-0 libpangocairo-1.0-0 "
                      "libgdk-pixbuf-2.0-0 libcairo2 — apt-get install them)"))

    fail = bool(missing_required) or bool(broken) or (args.all and bool(missing_optional))
    print()
    if fail:
        print(BAD("  Dependencies incomplete — the pipeline will fail on the jobs above."))
    else:
        extra = "" if not missing_optional else WARN(" (optional deps missing — on-demand reports only)")
        print(OK("  All required dependencies present.") + extra)
    return 1 if fail else 0


if __name__ == "__main__":
    raise SystemExit(main())

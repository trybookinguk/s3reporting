#!/usr/bin/env python3
"""Upload the report-recipients JSON to SharePoint (root of Platform Data).

This seeds / overwrites the live `report_recipients.json` that the reporting
scripts read at send time to decide who each report goes to. Non-technical staff
then edit that file directly in SharePoint — this script is only for the initial
seed, or to push a corrected copy if the live one gets broken.

Usage:
    # Seed from the version-controlled copy in deploy/
    python3 upload_recipients.py

    # Upload a specific file
    python3 upload_recipients.py --file path/to/report_recipients.json

    # Show what's currently live in SharePoint without uploading
    python3 upload_recipients.py --show
"""

import argparse
import json
import sys

from modules.utils import sharepoint
from modules.utils.config import (
    SHAREPOINT_DRIVE_ID, RECIPIENTS_FILENAME, RECIPIENTS_FOLDER,
)

DEFAULT_SOURCE = "deploy/report_recipients.json"


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--file", default=DEFAULT_SOURCE,
                        help=f"JSON file to upload (default: {DEFAULT_SOURCE})")
    parser.add_argument("--show", action="store_true",
                        help="Print the live SharePoint copy and exit")
    args = parser.parse_args()

    if not SHAREPOINT_DRIVE_ID:
        sys.exit("SHAREPOINT_DRIVE_ID is not set — source the .env first.")

    token = sharepoint.authenticate_graph()
    if not token:
        sys.exit("Graph authentication failed — check Azure credentials.")

    if args.show:
        raw = sharepoint.download_file(token, SHAREPOINT_DRIVE_ID,
                                       RECIPIENTS_FILENAME, folder=RECIPIENTS_FOLDER)
        if raw is None:
            print(f"No {RECIPIENTS_FILENAME} found in {RECIPIENTS_FOLDER}.")
        else:
            print(raw.decode("utf-8"))
        return

    # Validate before uploading — never push a broken file.
    with open(args.file, "rb") as f:
        data = f.read()
    try:
        parsed = json.loads(data)
    except json.JSONDecodeError as e:
        sys.exit(f"{args.file} is not valid JSON: {e}")
    for key, entry in parsed.items():
        if not isinstance(entry, dict) or "to" not in entry:
            sys.exit(f"Invalid entry '{key}': every report needs a 'to' list.")

    ok = sharepoint.upload_small(token, SHAREPOINT_DRIVE_ID,
                                 RECIPIENTS_FILENAME, data, folder=RECIPIENTS_FOLDER)
    if not ok:
        sys.exit("Upload failed — see the error above.")
    print(f"Uploaded {args.file} → {RECIPIENTS_FOLDER}/{RECIPIENTS_FILENAME} "
          f"({len(parsed)} reports).")


if __name__ == "__main__":
    main()

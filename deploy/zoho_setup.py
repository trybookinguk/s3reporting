#!/usr/bin/env python3
"""Mint a Zoho refresh token and look up ZOHO_PORTAL_NAME / ZOHO_ORG_ID.

Headless-friendly: uses Zoho's **Self Client** grant-code flow, so no redirect
URL or local web server is needed — you generate a short-lived code in the Zoho
API console and paste it in here.

This tenant uses the Zoho **.com (US)** data centre (accounts.zoho.com,
crm.zoho.com, salesiq.zoho.com) — matching modules/utils/zoho_api.py.

Prereqs in .env (already added by you):
    ZOHO_CLIENT_ID, ZOHO_CLIENT_SECRET

Steps this script walks you through:
  1. Prints the scope string to paste into the Self Client.
  2. You paste the generated grant code.
  3. It exchanges the code for a refresh token (printed).
  4. It uses the access token to fetch the SalesIQ portal name and the CRM org id.
  5. It prints the exact .env lines to add.

Usage:
    python3 deploy/zoho_setup.py                    # reads client id/secret from env
    set -a && source .env && set +a && python3 deploy/zoho_setup.py
"""
import getpass
import os
import sys

try:
    import requests
except ImportError:
    sys.exit("ERROR: requests not installed — pip install --break-system-packages requests")

# Data centre — override with ZOHO_DC env or --dc (com|eu|in|com.au|jp).
# Must match the api-console.zoho.<dc> where you generate the grant code.
DC = os.environ.get("ZOHO_DC", "com").strip().lstrip(".") or "com"
if "--dc" in sys.argv:
    i = sys.argv.index("--dc")
    if i + 1 < len(sys.argv):
        DC = sys.argv[i + 1].strip().lstrip(".")
ACCOUNTS = f"https://accounts.zoho.{DC}"
CRM_API = f"https://www.zohoapis.{DC}"
SALESIQ_API = f"https://salesiq.zoho.{DC}"

# Scopes the pipeline needs: CRM upserts/reads (industry, tiers) + SalesIQ reads
# (weekly/monthly chat stats) + org read (ZOHO_ORG_ID).
SCOPES = ",".join([
    "ZohoCRM.modules.ALL",
    "ZohoCRM.settings.ALL",
    "ZohoCRM.users.READ",
    "ZohoCRM.org.READ",
    "SalesIQ.portals.READ",
    "SalesIQ.conversations.READ",
])


def main() -> int:
    client_id = os.environ.get("ZOHO_CLIENT_ID", "").strip() or input("ZOHO_CLIENT_ID: ").strip()
    client_secret = os.environ.get("ZOHO_CLIENT_SECRET", "").strip() or getpass.getpass("ZOHO_CLIENT_SECRET (hidden): ").strip()
    if not client_id or not client_secret:
        return _die("ZOHO_CLIENT_ID / ZOHO_CLIENT_SECRET are required.")

    print("\n" + "=" * 72)
    print("STEP 1 — generate a grant code (one-time, in a browser)")
    print("=" * 72)
    print(f"""
  1. Go to  https://api-console.zoho.{DC}/  and open your Self Client
     (Add Client -> Self Client if you don't have one yet).
  2. Click the 'Generate Code' tab and enter:
       Scope:        {SCOPES}
       Time Duration: 10 minutes
       Scope Desc:   s3reporting pi
     Pick the correct portal/org if prompted, then Create -> Copy the code.
  3. Paste the code below (it expires in ~10 min, single use).
""")
    code = input("Grant code: ").strip()
    if not code:
        return _die("no grant code entered.")

    # --- STEP 2: exchange grant code for tokens -----------------------------
    print("\nExchanging grant code for tokens ...")
    try:
        r = requests.post(f"{ACCOUNTS}/oauth/v2/token", data={
            "grant_type": "authorization_code",
            "client_id": client_id,
            "client_secret": client_secret,
            "code": code,
        }, timeout=30)
        tok = r.json()
    except Exception as e:
        return _die(f"token request failed: {e}")

    if "refresh_token" not in tok:
        return _die(f"no refresh_token returned: {tok.get('error', tok)}\n"
                    "(codes are single-use and short-lived — regenerate and retry, "
                    "and check the data centre is .com)")
    refresh_token = tok["refresh_token"]
    access_token = tok.get("access_token", "")
    print("✓ refresh token obtained")

    # --- STEP 3: look up SalesIQ portal name --------------------------------
    portal_name = ""
    try:
        pr = requests.get(f"{SALESIQ_API}/api/v2/portals",
                          headers={"Authorization": f"Zoho-oauthtoken {access_token}"}, timeout=30)
        if pr.ok:
            data = pr.json().get("data", [])
            # each portal has a 'screenname' (used in the API path) and 'name'
            names = [(p.get("screenname") or p.get("name")) for p in data if isinstance(p, dict)]
            names = [n for n in names if n]
            if len(names) == 1:
                portal_name = names[0]
                print(f"✓ SalesIQ portal: {portal_name}")
            elif names:
                print(f"• Multiple SalesIQ portals found: {names}")
                portal_name = names[0]
                print(f"  Using the first ({portal_name}) — change if wrong.")
            else:
                print("• No SalesIQ portals returned (is SalesIQ enabled for this org?).")
        else:
            print(f"• Could not list SalesIQ portals (HTTP {pr.status_code}): {pr.text[:120]}")
    except Exception as e:
        print(f"• SalesIQ portal lookup failed: {e}")

    # --- STEP 4: look up CRM org id (bonus) ---------------------------------
    org_id = ""
    try:
        orr = requests.get(f"{CRM_API}/crm/v3/org",
                           headers={"Authorization": f"Zoho-oauthtoken {access_token}"}, timeout=30)
        if orr.ok:
            orgs = orr.json().get("org", [])
            if orgs and isinstance(orgs, list):
                org_id = str(orgs[0].get("id", "") or orgs[0].get("zgid", ""))
                if org_id:
                    print(f"✓ CRM org id: {org_id}")
        else:
            print(f"• Could not read CRM org (HTTP {orr.status_code}): {orr.text[:120]}")
    except Exception as e:
        print(f"• CRM org lookup failed: {e}")

    # --- Summary ------------------------------------------------------------
    print("\n" + "=" * 72)
    print("Add / update these lines in /root/s3reporting/.env :")
    print("=" * 72)
    print(f"ZOHO_REFRESH_TOKEN={refresh_token}")
    if portal_name:
        print(f"ZOHO_PORTAL_NAME={portal_name}")
    else:
        print("ZOHO_PORTAL_NAME=          # look up manually: salesiq.zoho.com portal screenname")
    if org_id:
        print(f"ZOHO_ORG_ID={org_id}")
    print("=" * 72)
    print("\nThe refresh token is long-lived — store it in the password manager too.")
    print("Then verify:  python3 test_secrets.py --only zoho")
    return 0


def _die(msg: str) -> int:
    print(f"ERROR: {msg}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

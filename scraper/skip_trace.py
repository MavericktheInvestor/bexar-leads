"""
Skip Trace — Scraped Leads
===========================
Reads the top 100 NEW leads (by Seller Score) from the Master Google Sheet
that have not yet been skip traced, enriches them via DealMachine API,
writes phone numbers back to the sheet, and saves data/scraped_skip_traced.csv
for the GHL push step.

⚠️  SECURITY GATE: This script will NOT run unless the env var
    SKIP_TRACE_GREEN_LIGHT=YES_RUN_SKIP_TRACE is set.
    DO NOT remove this gate without explicit authorization.

Env vars required:
  SKIP_TRACE_GREEN_LIGHT  — must equal "YES_RUN_SKIP_TRACE"
  GOOGLE_CREDENTIALS      — service account JSON (raw or base64)
  GOOGLE_SHEET_ID         — master sheet ID
  DEALMACHINE_API_KEY     — DealMachine API key
"""

import os
import csv
import json
import base64
import time
from pathlib import Path

# ── SECURITY GATE ─────────────────────────────────────────────────────────────
GREEN_LIGHT = os.environ.get("SKIP_TRACE_GREEN_LIGHT", "")
if GREEN_LIGHT != "YES_RUN_SKIP_TRACE":
    print("⛔ Skip trace gate is CLOSED.")
    print("   Set SKIP_TRACE_GREEN_LIGHT=YES_RUN_SKIP_TRACE to enable.")
    print("   Exiting without skip tracing any leads.")
    exit(0)

import gspread
from google.oauth2.service_account import Credentials

# ── Config ────────────────────────────────────────────────────────────────────
SHEET_ID        = os.environ.get("GOOGLE_SHEET_ID", "1mC-bTqyRB-VHlLdNCzCfkaPRAq0TsLYSc93JhjbiVJk")
DM_API_KEY      = os.environ.get("DEALMACHINE_API_KEY", "")
DM_API_BASE     = "https://api.dealmachine.com"
TOP_N           = 100       # max leads to skip trace per run
RATE_DELAY      = 0.5       # seconds between DealMachine calls
DATA_DIR        = Path(__file__).parent.parent / "data"
OUTPUT_CSV      = DATA_DIR / "scraped_skip_traced.csv"

# Column indices in master sheet (0-based after header)
COL_LAST_NAME    = 0
COL_FIRST_NAME   = 1
COL_MAIL_ADDR    = 2
COL_MAIL_CITY    = 3
COL_MAIL_STATE   = 4
COL_MAIL_ZIP     = 5
COL_PROP_ADDR    = 6
COL_PROP_CITY    = 7
COL_PROP_STATE   = 8
COL_PROP_ZIP     = 9
COL_LEAD_TYPE    = 10
COL_DOC_TYPE     = 11
COL_DATE_FILED   = 12
COL_DOC_NUM      = 13
COL_AMOUNT       = 14
COL_SCORE        = 15
COL_FLAGS        = 16
COL_SOURCE       = 17
COL_URL          = 18
COL_STATUS       = 19
COL_NOTES        = 20
COL_DATE_CONTACT = 21

SHEET_HEADERS = [
    "Last Name ", "First name", "Mailing Address", "Mailing City",
    "Mailing State", "Mailing Zip", "Property Address", "Property City",
    "Property State", "Property Zip", "Lead Type", "Document Type",
    "Date Filed", "Document Number", "Amount/Debt Owed", "Seller Score",
    "Motivated Seller Flags", "Source", "Public Records URL",
    "Status", "Partner Notes", "Date Contacted",
]

# ── Google Sheets auth ────────────────────────────────────────────────────────
def get_sheet():
    raw = os.environ.get("GOOGLE_CREDENTIALS", "")
    if not raw:
        raise ValueError("GOOGLE_CREDENTIALS env var not set")
    try:
        creds_dict = json.loads(raw)
    except json.JSONDecodeError:
        creds_dict = json.loads(base64.b64decode(raw).decode())

    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
    client = gspread.authorize(creds)
    return client.open_by_key(SHEET_ID).sheet1


# ── DealMachine skip trace ────────────────────────────────────────────────────
def skip_trace_by_name(first_name: str, last_name: str, zip_code: str = "") -> dict:
    """
    Call DealMachine enrich-by-name API.
    Returns dict with phone1, phone2 (empty strings if not found).
    """
    import urllib.request, urllib.parse

    if not DM_API_KEY:
        raise ValueError("DEALMACHINE_API_KEY env var not set")

    params = {"last_name": last_name}
    if first_name:
        params["first_name"] = first_name
    if zip_code:
        params["location_type"] = "zip_code"
        params["location_code"] = zip_code

    url = f"{DM_API_BASE}/v2/people?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {DM_API_KEY}",
        "Accept": "application/json",
    })

    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read())
    except Exception as e:
        print(f"    DM API error for {last_name}, {first_name}: {e}")
        return {"phone1": "", "phone2": ""}

    people = data.get("people", data.get("data", []))
    if not people:
        return {"phone1": "", "phone2": ""}

    # Take first result's phones
    person = people[0]
    phones = person.get("phones", [])
    phone_numbers = [p.get("phone_number", "") for p in phones if p.get("phone_number")]

    return {
        "phone1": phone_numbers[0] if len(phone_numbers) > 0 else "",
        "phone2": phone_numbers[1] if len(phone_numbers) > 1 else "",
    }


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    DATA_DIR.mkdir(exist_ok=True)

    print("=" * 60)
    print("  Skip Trace — Scraped Leads")
    print("=" * 60)

    print("\nConnecting to Master Google Sheet...")
    sheet = get_sheet()
    all_rows = sheet.get_all_values()

    if not all_rows or len(all_rows) < 2:
        print("No leads in sheet. Exiting.")
        return

    header = all_rows[0]
    data_rows = all_rows[1:]  # skip header

    # Find leads that have NOT been skip traced yet
    # We use Status column — skip traced leads will have "Skip Traced" or "Sent to CRM"
    candidates = []
    for i, row in enumerate(data_rows, start=2):  # 2 = sheet row number (1-indexed + header)
        # Pad row to full width
        while len(row) < len(SHEET_HEADERS):
            row.append("")

        status = row[COL_STATUS].strip().lower()
        doc_num = row[COL_DOC_NUM].strip()
        last_name = row[COL_LAST_NAME].strip()
        first_name = row[COL_FIRST_NAME].strip()
        score_raw = row[COL_SCORE].strip()

        # Skip if already traced or pushed to CRM
        if status in ("skip traced", "sent to crm", "skip trace failed"):
            continue

        # Skip if no name
        if not last_name:
            continue

        try:
            score = int(score_raw)
        except ValueError:
            score = 0

        candidates.append({
            "sheet_row": i,
            "score": score,
            "last_name": last_name,
            "first_name": first_name,
            "doc_num": doc_num,
            "mail_zip": row[COL_MAIL_ZIP].strip(),
            "prop_zip": row[COL_PROP_ZIP].strip(),
            "mail_address": row[COL_MAIL_ADDR].strip(),
            "mail_city": row[COL_MAIL_CITY].strip(),
            "mail_state": row[COL_MAIL_STATE].strip(),
            "prop_address": row[COL_PROP_ADDR].strip(),
            "prop_city": row[COL_PROP_CITY].strip(),
            "prop_state": row[COL_PROP_STATE].strip(),
            "lead_type": row[COL_LEAD_TYPE].strip(),
            "doc_type": row[COL_DOC_TYPE].strip(),
            "date_filed": row[COL_DATE_FILED].strip(),
            "score_str": score_raw,
            "flags": row[COL_FLAGS].strip(),
            "source": row[COL_SOURCE].strip(),
            "url": row[COL_URL].strip(),
        })

    # Sort by score descending, take top N
    candidates.sort(key=lambda r: r["score"], reverse=True)
    to_trace = candidates[:TOP_N]

    print(f"  {len(candidates)} untraced leads found")
    print(f"  Skip tracing top {len(to_trace)} by Seller Score\n")

    if not to_trace:
        print("Nothing to skip trace today.")
        return

    # Skip trace each lead
    traced = []
    for i, lead in enumerate(to_trace, 1):
        last  = lead["last_name"]
        first = lead["first_name"]
        zip_  = lead["mail_zip"] or lead["prop_zip"]

        print(f"  [{i}/{len(to_trace)}] {last}, {first} (score {lead['score']})...")
        result = skip_trace_by_name(first, last, zip_)
        lead["phone1"] = result["phone1"]
        lead["phone2"] = result["phone2"]

        status = "Skip Traced" if result["phone1"] else "Skip Trace Failed"

        # Write status back to master sheet
        try:
            sheet.update_cell(lead["sheet_row"], COL_STATUS + 1, status)
        except Exception as e:
            print(f"    ⚠  Sheet update failed: {e}")

        if result["phone1"]:
            print(f"    ✅ Phone: {result['phone1']}")
            traced.append(lead)
        else:
            print(f"    ✗  No phone found")

        time.sleep(RATE_DELAY)

    print(f"\n✅ Skip traced {len(traced)} leads with phone numbers")

    # Save to CSV for GHL push step
    fieldnames = [
        "last", "first", "phone1", "phone2",
        "mail_address", "mail_city", "mail_state", "mail_zip",
        "prop_address", "prop_city", "prop_state", "prop_zip",
        "lead_type", "doc_type", "date_filed", "doc_num",
        "score", "flags", "source", "url",
    ]
    with open(OUTPUT_CSV, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for lead in traced:
            writer.writerow({
                "last":         lead["last_name"],
                "first":        lead["first_name"],
                "phone1":       lead["phone1"],
                "phone2":       lead["phone2"],
                "mail_address": lead["mail_address"],
                "mail_city":    lead["mail_city"],
                "mail_state":   lead["mail_state"],
                "mail_zip":     lead["mail_zip"],
                "prop_address": lead["prop_address"],
                "prop_city":    lead["prop_city"],
                "prop_state":   lead["prop_state"],
                "prop_zip":     lead["prop_zip"],
                "lead_type":    lead["lead_type"],
                "doc_type":     lead["doc_type"],
                "date_filed":   lead["date_filed"],
                "doc_num":      lead["doc_num"],
                "score":        lead["score"],
                "flags":        lead["flags"],
                "source":       lead["source"],
                "url":          lead["url"],
            })

    print(f"  Saved {len(traced)} leads to {OUTPUT_CSV}")
    print(f"\nDone. GHL push step will pick up scraped_skip_traced.csv next.")


if __name__ == "__main__":
    main()

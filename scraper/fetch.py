"""
Bexar County Clerk Lead Scraper
================================
Scrapes bexar.tx.publicsearch.us for 5 motivated-seller document types:
  - NOFC   : Notice of Foreclosure
  - SUBTRST: Substitute of Trustee
  - HEIRSHP: Affidavit of Heirship
  - PROB   : Probate
  - TAXD   : Tax Delinquency

Scores each lead, deduplicates against the Google Sheet, and appends new rows.

Runs daily via GitHub Actions (scrape.yml).
Requires env vars:
  GOOGLE_CREDENTIALS  — service account JSON (base64 or raw)
  GOOGLE_SHEET_ID     — target spreadsheet ID
"""

import os
import re
import json
import base64
import time
import asyncio
from datetime import date, timedelta
from pathlib import Path

from playwright.async_api import async_playwright, TimeoutError as PWTimeout
import gspread
from google.oauth2.service_account import Credentials

# ── Config ─────────────────────────────────────────────────────────────────
SHEET_ID       = os.environ.get("GOOGLE_SHEET_ID", "1mC-bTqyRB-VHlLdNCzCfkaPRAq0TsLYSc93JhjbiVJk")
BASE_URL       = "https://bexar.tx.publicsearch.us"
DATA_DIR       = Path(__file__).parent.parent / "data"
RECORDS_FILE   = DATA_DIR / "records.json"

# Document types to search: (doc_type_code, human_label, search_term)
DOC_TYPES = [
    ("NOFC",    "Notice of Foreclosure",   "NOFC"),
    ("SUBTRST", "Substitute Trustee",      "SUBTRST"),
    ("HEIRSHP", "Affidavit of Heirship",   "HEIRSHP"),
    ("PROB",    "Probate",                 "PROB"),
    ("TAXD",    "Tax Delinquency",         "TAXD"),
]

# How many days back to look each run
LOOKBACK_DAYS = 7

# Sheet columns (must match header row exactly)
SHEET_HEADERS = [
    "Last Name ", "First name", "Mailing Address", "Mailing City",
    "Mailing State", "Mailing Zip", "Property Address", "Property City",
    "Property State", "Property Zip", "Lead Type", "Document Type",
    "Date Filed", "Document Number", "Amount/Debt Owed", "Seller Score",
    "Motivated Seller Flags", "Source", "Public Records URL",
    "Status", "Partner Notes", "Date Contacted",
]

# ── Google Sheets auth ──────────────────────────────────────────────────────
def get_sheet():
    raw = os.environ.get("GOOGLE_CREDENTIALS", "")
    if not raw:
        raise ValueError("GOOGLE_CREDENTIALS env var not set")
    # Accept raw JSON or base64-encoded JSON
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


def load_existing_doc_numbers(sheet):
    """Return set of all Document Numbers already in the sheet."""
    try:
        col_idx = SHEET_HEADERS.index("Document Number") + 1
        values = sheet.col_values(col_idx)
        return set(v.strip() for v in values[1:] if v.strip())  # skip header
    except Exception:
        return set()


# ── Seller scoring ─────────────────────────────────────────────────────────
def score_lead(record: dict) -> tuple[int, list[str]]:
    """Return (score 0-100, list of flag strings)."""
    flags = []
    score = 0

    doc_type = record.get("doc_type_code", "")
    name = (record.get("grantor", "") or "").upper()
    today = date.today()
    filed_str = record.get("date_filed", "")

    # Document type scoring
    if doc_type == "SUBTRST":
        score += 40
        flags.append("Substitute trustee")
    elif doc_type == "NOFC":
        score += 35
        flags.append("Pre-foreclosure")
    elif doc_type == "HEIRSHP":
        score += 30
        flags.append("Affidavit of heirship")
    elif doc_type == "PROB":
        score += 25
        flags.append("Probate")
    elif doc_type == "TAXD":
        score += 20
        flags.append("Tax delinquency")

    # LLC / corp owner
    corp_keywords = ["LLC", "L.L.C", "INC", "CORP", "LTD", "TRUST", "ASSOCIATION", "FUND"]
    if any(k in name for k in corp_keywords):
        score += 15
        flags.append("Business / LLC owner")

    # Filed recently (within last 7 days)
    try:
        filed = date.fromisoformat(filed_str)
        if (today - filed).days <= 7:
            score += 20
            flags.append("New this week")
        elif (today - filed).days <= 30:
            score += 10
    except Exception:
        pass

    # Multiple distress signals
    if len(flags) >= 3:
        score += 10
        flags.append("Multiple distress signals")

    return min(score, 100), flags


# ── Playwright scraper ─────────────────────────────────────────────────────
async def scrape_doc_type(page, doc_code: str, doc_label: str, search_term: str,
                          lookback_days: int) -> list[dict]:
    """Search publicsearch.us for one document type, return list of record dicts."""
    records = []
    start_date = (date.today() - timedelta(days=lookback_days)).isoformat()
    end_date   = date.today().isoformat()

    search_url = f"{BASE_URL}/search/advanced"
    print(f"  Searching {doc_label} ({search_term}) from {start_date} to {end_date}...")

    try:
        await page.goto(search_url, wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_timeout(2000)

        # Fill document type field
        doc_input = page.locator("input[placeholder*='doc type'], input[name*='docType'], input[id*='docType']").first
        if await doc_input.count() == 0:
            # Try the main search box
            doc_input = page.locator("input[type='text']").first
        await doc_input.fill(search_term)
        await page.wait_for_timeout(500)

        # Set date range — from date
        date_inputs = page.locator("input[type='date'], input[placeholder*='mm/dd']")
        count = await date_inputs.count()
        if count >= 2:
            await date_inputs.nth(0).fill(start_date)
            await date_inputs.nth(1).fill(end_date)
        elif count == 1:
            await date_inputs.nth(0).fill(start_date)

        # Submit search
        search_btn = page.locator("button[type='submit'], button:has-text('Search')").first
        await search_btn.click()
        await page.wait_for_timeout(3000)

        # Parse results pages
        page_num = 1
        while True:
            print(f"    Page {page_num}...")
            rows = await page.locator("table tbody tr, .result-row, [data-testid='result-row']").all()

            if not rows:
                # Try JSON API fallback
                break

            for row in rows:
                try:
                    cells = await row.locator("td").all()
                    if len(cells) < 4:
                        continue
                    texts = [await c.inner_text() for c in cells]

                    # Extract grantor (owner name) — usually first column
                    grantor_raw = texts[0].strip() if texts else ""
                    # Split last/first
                    parts = grantor_raw.split(",", 1)
                    last_name  = parts[0].strip().title() if parts else grantor_raw.title()
                    first_name = parts[1].strip().title() if len(parts) > 1 else ""

                    # Document number — look for numeric-ish column
                    doc_num = ""
                    for t in texts:
                        if re.match(r"^\d{10,}", t.strip()):
                            doc_num = t.strip()
                            break

                    # Date filed
                    date_filed = ""
                    for t in texts:
                        m = re.search(r"\d{4}-\d{2}-\d{2}|\d{2}/\d{2}/\d{4}", t)
                        if m:
                            raw_date = m.group()
                            if "/" in raw_date:
                                parts_d = raw_date.split("/")
                                raw_date = f"{parts_d[2]}-{parts_d[0]}-{parts_d[1]}"
                            date_filed = raw_date
                            break

                    # Property address — look for street pattern
                    prop_address = ""
                    for t in texts:
                        if re.search(r"\d+ [A-Z]", t.upper()) and len(t) > 10:
                            prop_address = t.strip().title()
                            break

                    record = {
                        "last_name":      last_name,
                        "first_name":     first_name,
                        "mailing_address": "",
                        "mailing_city":   "",
                        "mailing_state":  "TX",
                        "mailing_zip":    "",
                        "prop_address":   prop_address,
                        "prop_city":      "San Antonio",
                        "prop_state":     "TX",
                        "prop_zip":       "",
                        "lead_type":      doc_label,
                        "doc_type_code":  doc_code,
                        "date_filed":     date_filed,
                        "doc_number":     doc_num,
                        "amount_owed":    "0.0",
                        "grantor":        grantor_raw.upper(),
                        "source":         "Bexar County Clerk",
                        "url":            BASE_URL,
                    }
                    records.append(record)
                except Exception as e:
                    print(f"    Row parse error: {e}")
                    continue

            # Try next page
            next_btn = page.locator("button:has-text('Next'), a:has-text('Next'), [aria-label='Next page']").first
            if await next_btn.count() == 0 or not await next_btn.is_enabled():
                break
            await next_btn.click()
            await page.wait_for_timeout(2000)
            page_num += 1

            if page_num > 20:  # safety cap
                break

    except PWTimeout:
        print(f"  Timeout on {doc_label} — skipping")
    except Exception as e:
        print(f"  Error scraping {doc_label}: {e}")

    print(f"  Found {len(records)} raw records for {doc_label}")
    return records


async def scrape_all() -> list[dict]:
    """Run scraper for all document types and return combined deduplicated records."""
    all_records = []
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-setuid-sandbox",
                  "--disable-dev-shm-usage", "--disable-gpu"],
        )
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            viewport={"width": 1280, "height": 900},
        )
        page = await context.new_page()

        # Accept cookies / handle any initial modal
        await page.goto(BASE_URL, wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_timeout(2000)
        for selector in ["button:has-text('Accept')", "button:has-text('OK')",
                         "button:has-text('Close')", "[aria-label='Close']"]:
            btn = page.locator(selector).first
            if await btn.count() > 0:
                try:
                    await btn.click()
                    await page.wait_for_timeout(500)
                except Exception:
                    pass

        for doc_code, doc_label, search_term in DOC_TYPES:
            records = await scrape_doc_type(
                page, doc_code, doc_label, search_term, LOOKBACK_DAYS
            )
            all_records.extend(records)
            await page.wait_for_timeout(1500)  # be polite between searches

        await browser.close()

    return all_records


# ── Main ───────────────────────────────────────────────────────────────────
async def main():
    DATA_DIR.mkdir(exist_ok=True)

    print("Connecting to Google Sheet...")
    sheet = get_sheet()
    existing_doc_numbers = load_existing_doc_numbers(sheet)
    print(f"  {len(existing_doc_numbers)} existing records in sheet")

    print("Starting Bexar County Clerk scrape...")
    raw_records = await scrape_all()
    print(f"Total raw records scraped: {len(raw_records)}")

    # Score, deduplicate, and prepare rows
    new_rows = []
    seen_doc_nums = set(existing_doc_numbers)

    for rec in raw_records:
        doc_num = rec.get("doc_number", "").strip()
        if not doc_num or doc_num in seen_doc_nums:
            continue
        seen_doc_nums.add(doc_num)

        score, flags = score_lead(rec)
        flags_str = " | ".join(flags)

        row = [
            rec.get("last_name", ""),
            rec.get("first_name", ""),
            rec.get("mailing_address", ""),
            rec.get("mailing_city", ""),
            rec.get("mailing_state", "TX"),
            rec.get("mailing_zip", ""),
            rec.get("prop_address", ""),
            rec.get("prop_city", "San Antonio"),
            rec.get("prop_state", "TX"),
            rec.get("prop_zip", ""),
            rec.get("lead_type", ""),
            rec.get("lead_type", ""),   # Document Type same as Lead Type display
            rec.get("date_filed", ""),
            doc_num,
            rec.get("amount_owed", "0.0"),
            str(score),
            flags_str,
            rec.get("source", "Bexar County Clerk"),
            rec.get("url", BASE_URL),
            "",   # Status
            "",   # Partner Notes
            "",   # Date Contacted
        ]
        new_rows.append(row)

    print(f"New records to append: {len(new_rows)}")

    if new_rows:
        # Sort by score descending so hottest leads are at top
        new_rows.sort(key=lambda r: int(r[15]) if r[15].isdigit() else 0, reverse=True)
        sheet.append_rows(new_rows, value_input_option="USER_ENTERED")
        print(f"✅ Appended {len(new_rows)} new leads to Google Sheet")
    else:
        print("No new leads found today.")

    # Save local records.json for GitHub Pages dashboard
    all_records_json = []
    for i, row in enumerate(new_rows):
        all_records_json.append(dict(zip(SHEET_HEADERS, row)))
    records_out = DATA_DIR / "records.json"
    records_out.write_text(json.dumps(all_records_json, indent=2))

    # Also save leads.csv
    import csv
    leads_out = DATA_DIR / "leads.csv"
    write_header = not leads_out.exists()
    with open(leads_out, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=SHEET_HEADERS)
        if write_header:
            writer.writeheader()
        for row in new_rows:
            writer.writerow(dict(zip(SHEET_HEADERS, row)))

    print(f"Done. records.json and leads.csv updated.")


if __name__ == "__main__":
    asyncio.run(main())

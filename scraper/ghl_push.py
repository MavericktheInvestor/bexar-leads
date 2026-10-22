"""
GoHighLevel Push — Scraped Leads
==================================
Reads data/scraped_skip_traced.csv (built by skip_trace.py)
Creates contacts + opportunities in GHL Acquisitions Pipeline.

Env vars required:
  GHL_API_KEY      — Private Integration Token
  GHL_LOCATION_ID  — Sub-account Location ID
  GHL_PIPELINE_ID  — Pipeline ID
  GHL_STAGE_ID     — Stage ID for "LEFT VOICEMAIL/RECALL"
  GOOGLE_CREDENTIALS — for updating sheet status
  GOOGLE_SHEET_ID    — master sheet ID
"""

import csv, json, os, sys, time, re
from datetime import datetime
from pathlib import Path

# ── Config ────────────────────────────────────────────────────────────────────
GHL_API_KEY     = os.environ.get("GHL_API_KEY", "")
GHL_LOCATION_ID = os.environ.get("GHL_LOCATION_ID", "")
GHL_PIPELINE_ID = os.environ.get("GHL_PIPELINE_ID", "")
GHL_STAGE_ID    = os.environ.get("GHL_STAGE_ID", "4ae6db3b-1537-4f10-a165-b73f067c9a3d")

API_BASE         = "https://services.leadconnectorhq.com"
API_VERSION      = "2021-07-28"
RATE_LIMIT_DELAY = 0.25

DATA_DIR    = Path(__file__).parent.parent / "data"
QUEUE_CSV   = DATA_DIR / "scraped_skip_traced.csv"
SHEET_ID    = os.environ.get("GOOGLE_SHEET_ID", "1mC-bTqyRB-VHlLdNCzCfkaPRAq0TsLYSc93JhjbiVJk")


# ── GHL API helpers ───────────────────────────────────────────────────────────
def ghl_headers():
    return {
        "Authorization": f"Bearer {GHL_API_KEY}",
        "Version":       API_VERSION,
        "Content-Type":  "application/json",
        "Accept":        "application/json",
        "User-Agent":    "Mozilla/5.0 (compatible; GHL-Integration/1.0)",
    }

def ghl_post(path, body):
    import urllib.request
    url  = f"{API_BASE}{path}"
    data = json.dumps(body).encode()
    req  = urllib.request.Request(url, data=data, headers=ghl_headers(), method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read()), resp.status
    except Exception as e:
        if hasattr(e, "read"):
            err = e.read().decode(errors="replace")
            raise RuntimeError(f"HTTP {getattr(e,'code','?')}: {err}") from e
        raise

def ghl_get(path, params=None):
    import urllib.request, urllib.parse
    url = f"{API_BASE}{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers=ghl_headers())
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())

def ghl_put(path, body):
    import urllib.request
    url  = f"{API_BASE}{path}"
    data = json.dumps(body).encode()
    req  = urllib.request.Request(url, data=data, headers=ghl_headers(), method="PUT")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read()), resp.status
    except Exception as e:
        if hasattr(e, "read"):
            err = e.read().decode(errors="replace")
            raise RuntimeError(f"HTTP {getattr(e,'code','?')}: {err}") from e
        raise


# ── Contact helpers ───────────────────────────────────────────────────────────
def urgency_tag(score):
    try:
        s = int(float(score))
    except (ValueError, TypeError):
        s = 0
    if s >= 90: return "Hot Lead 🔥"
    if s >= 80: return "Warm Lead"
    return "Cool Lead"

def build_contact(r):
    score = r.get("score", 0)
    tags = [
        "Bexar County Scraper Lead",
        urgency_tag(score),
        r.get("lead_type", "Scraped Lead"),
    ]

    custom_fields = []
    if r.get("prop_address"):
        custom_fields.append({"key": "property_address", "field_value": r["prop_address"]})
    if r.get("score"):
        custom_fields.append({"key": "seller_score", "field_value": str(r["score"])})
    if r.get("flags"):
        custom_fields.append({"key": "motivated_seller_flags", "field_value": r["flags"]})
    if r.get("doc_num"):
        custom_fields.append({"key": "document_number", "field_value": r["doc_num"]})
    if r.get("date_filed"):
        custom_fields.append({"key": "date_filed", "field_value": r["date_filed"]})

    body = {
        "locationId": GHL_LOCATION_ID,
        "firstName":  r.get("first", ""),
        "lastName":   r.get("last", ""),
        "tags":       tags,
        "source":     "Bexar County Scraper",
    }

    if r.get("phone1"):
        body["phone"] = r["phone1"]

    mail_addr = r.get("mail_address", "")
    prop_addr = r.get("prop_address", "")
    if mail_addr:
        body["address1"]   = mail_addr
        body["city"]       = r.get("mail_city", "")
        body["state"]      = r.get("mail_state", "TX")
        body["postalCode"] = r.get("mail_zip", "")
    elif prop_addr:
        body["address1"]   = prop_addr
        body["city"]       = r.get("prop_city", "San Antonio")
        body["state"]      = r.get("prop_state", "TX")
        body["postalCode"] = r.get("prop_zip", "")

    if custom_fields:
        body["customFields"] = custom_fields

    return body

def find_existing_contact(first, last):
    try:
        data = ghl_get("/contacts/search", {
            "locationId": GHL_LOCATION_ID,
            "query": f"{first} {last}".strip(),
            "limit": 5,
        })
        for c in data.get("contacts", []):
            fn = (c.get("firstName") or "").strip().lower()
            ln = (c.get("lastName") or "").strip().lower()
            if fn == first.lower() and ln == last.lower():
                return c["id"]
    except Exception:
        pass
    return None

def create_or_update_contact(r):
    existing_id = find_existing_contact(r.get("first", ""), r.get("last", ""))
    body = build_contact(r)
    if existing_id:
        try:
            ghl_put(f"/contacts/{existing_id}", {
                "locationId": GHL_LOCATION_ID,
                "tags": body.get("tags", []),
            })
            return existing_id, "updated"
        except Exception as e:
            print(f"    ⚠ Update failed: {e}")
            return existing_id, "update_failed"
    else:
        result, status = ghl_post("/contacts/", body)
        contact_id = result.get("contact", {}).get("id") or result.get("id", "")
        return contact_id, "created"

def create_opportunity(contact_id, r):
    prop = r.get("prop_address") or f"{r.get('last', '')} Property"
    name = f"{r.get('last', '')}, {r.get('first', '')} — {prop}"
    body = {
        "locationId":     GHL_LOCATION_ID,
        "pipelineId":     GHL_PIPELINE_ID,
        "pipelineStageId": GHL_STAGE_ID,
        "contactId":      contact_id,
        "name":           name,
        "status":         "open",
        "source":         "Bexar County Scraper",
    }
    result, status = ghl_post("/opportunities/", body)
    return result.get("opportunity", {}).get("id") or result.get("id", "")


# ── Update master sheet status ────────────────────────────────────────────────
def update_sheet_status(pushed_doc_nums):
    creds_json = os.environ.get("GOOGLE_CREDENTIALS", "")
    if not creds_json or not pushed_doc_nums:
        return
    try:
        import base64, json as _json
        import google.oauth2.service_account as sa
        import googleapiclient.discovery as discovery

        try:
            creds_dict = _json.loads(creds_json)
        except Exception:
            creds_dict = _json.loads(base64.b64decode(creds_json).decode())

        creds = sa.Credentials.from_service_account_info(
            creds_dict,
            scopes=["https://www.googleapis.com/auth/spreadsheets",
                    "https://www.googleapis.com/auth/drive"])
        svc = discovery.build("sheets", "v4", credentials=creds, cache_discovery=False)

        result = svc.spreadsheets().values().get(
            spreadsheetId=SHEET_ID,
            range="Sheet1!A:T"
        ).execute()
        rows = result.get("values", [])

        updates = []
        for i, row in enumerate(rows[1:], start=2):
            doc_num = row[13].strip() if len(row) > 13 else ""
            if doc_num in pushed_doc_nums:
                updates.append({
                    "range": f"Sheet1!T{i}",
                    "values": [["Sent to CRM"]],
                })

        if updates:
            svc.spreadsheets().values().batchUpdate(
                spreadsheetId=SHEET_ID,
                body={"valueInputOption": "RAW", "data": updates}
            ).execute()
            print(f"  ✅ Marked {len(updates)} leads as 'Sent to CRM' in master sheet")
    except Exception as e:
        print(f"  ⚠ Sheet status update failed: {e}")


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    print("=" * 60)
    print("  GHL Push — Scraped Leads")
    print(f"  {datetime.utcnow().isoformat()} UTC")
    print("=" * 60)

    missing = [v for v in ["GHL_API_KEY", "GHL_LOCATION_ID", "GHL_PIPELINE_ID"]
               if not os.environ.get(v)]
    if missing:
        print(f"\n❌ Missing env vars: {', '.join(missing)}")
        sys.exit(1)

    if not QUEUE_CSV.exists():
        print(f"\n⚠  No skip traced CSV found at {QUEUE_CSV}")
        print("   Run skip_trace.py first, or no leads qualified today.")
        sys.exit(0)

    leads = []
    with open(QUEUE_CSV, newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            leads.append(row)

    print(f"\n📂 {len(leads)} skip traced leads to push")

    if not leads:
        print("Nothing to push.")
        return

    created = updated = skipped = errors = 0
    pushed_doc_nums = set()

    print("\n🚀 Pushing to GHL...")
    for i, r in enumerate(leads, 1):
        first = r.get("first", "").strip()
        last  = r.get("last", "").strip()
        phone = r.get("phone1", "").strip()

        if not last or not phone:
            skipped += 1
            continue

        try:
            contact_id, action = create_or_update_contact(r)
            if not contact_id:
                print(f"  [{i}] ✗ No contact ID for {last}, {first}")
                errors += 1
                continue

            if action == "created":
                opp_id = create_opportunity(contact_id, r)
                created += 1
                print(f"  [{i}] ✅ Created: {last}, {first} (score {r.get('score',0)})")
            else:
                updated += 1
                print(f"  [{i}] 🔄 Updated: {last}, {first}")

            doc_num = r.get("doc_num", "")
            if doc_num:
                pushed_doc_nums.add(doc_num)

        except Exception as e:
            print(f"  [{i}] ✗ {last}, {first}: {e}")
            errors += 1

        time.sleep(RATE_LIMIT_DELAY)

    print(f"\n✅ Done:")
    print(f"   Created : {created}")
    print(f"   Updated : {updated}")
    print(f"   Skipped : {skipped}")
    print(f"   Errors  : {errors}")

    update_sheet_status(pushed_doc_nums)


if __name__ == "__main__":
    main()

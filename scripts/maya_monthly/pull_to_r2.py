# -*- coding: utf-8 -*-
"""
pull_to_r2.py — שומר ב-R2 את כל הדוחות החודשיים (TXT1) של קרנות הנאמנות
וקרנות הסל מ-Maya, גולמיים ודחוסים, כתשתית לניתוחים מהירים בהמשך.

מקור (זהה ל-Revach scripts/funds_info/exposure.py):
  maya.tase.co.il/api/v1/reports/mutual-funds  (freeText "דוח חודשי")
  -> maya.tase.co.il/api/v1/reports/{id}/attachments/file?attachmentType=TXT1

מבנה ב-R2 (bucket לפי R2_BUCKET; ב-workflow: funds-complete-list):
  monthly/raw/{report_id}.csv.gz  - קובץ ה-TXT1 כפי שהגיע (bytes מקוריים), gzip
  monthly/manifest.json           - לכל דוח: id, title, companies, month, publish,
                                    rows, bytes, gz_bytes, sha256, header, fetched_at

אינקרמנטלי: דוח שכבר במניפסט לא יורד שוב, ולכן ריצה חוזרת מורידה רק דוחות
חדשים. המניפסט נשמר כל SAVE_EVERY דוחות, כך שריצה שנקטעה ממשיכה מאותה נקודה.

משתני סביבה:
  CF_ACCOUNT_ID, CF_R2_TOKEN (או CF_D1_TOKEN)  - חובה
  R2_BUCKET     - ברירת מחדל maya-reports
  MONTHS_BACK   - מספר חודשים אחורה, או "all" (עד EMPTY_STOP חודשים ריקים ברצף)
"""

import gzip, hashlib, io, csv, json, os, sys, time
from datetime import date, datetime, timezone

import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from r2_client import R2

BASE = "https://maya.tase.co.il"
LIST_URL   = BASE + "/api/v1/reports/mutual-funds"
REPORT_URL = BASE + "/api/v1/reports/{id}/attachments/file?attachmentType=TXT1"
HEADERS = {
    "accept": "application/json, text/plain, */*",
    "accept-language": "he-IL",
    "content-type": "application/json",
    "x-maya-with": "allow",
    "user-agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36"),
    "referer": BASE + "/he/reports/mutual-funds",
}
MANIFEST_KEY = "monthly/manifest.json"
RAW_KEY      = "monthly/raw/{id}.csv.gz"
SAVE_EVERY   = 20
EMPTY_STOP   = 6          # ב-"all": עוצרים אחרי 6 חודשים ריקים ברצף
MAX_MONTHS   = 240
_BACKOFF = (2, 4, 8, 16, 30, 60)

HEB_MONTHS = {"ינואר": 1, "פברואר": 2, "מרץ": 3, "מרס": 3, "אפריל": 4, "מאי": 5, "יוני": 6,
              "יולי": 7, "אוגוסט": 8, "ספטמבר": 9, "אוקטובר": 10, "נובמבר": 11, "דצמבר": 12}


def _req(session, method, url, **kw):
    last = None
    for wait in (0,) + _BACKOFF:
        if wait:
            time.sleep(wait)
        try:
            r = session.request(method, url, timeout=90, **kw)
            if r.status_code == 200:
                return r
            last = f"HTTP {r.status_code}: {(r.text or '')[:200]!r}"
        except requests.RequestException as e:
            last = f"{type(e).__name__}: {e}"
    raise RuntimeError(f"{method} {url} נכשל: {last}")


def _report_month(title):
    """'דוח חודשי - אפריל 2026' -> '2026-04'. לא מזוהה -> ''."""
    year = mon = 0
    for p in (title or "").replace("-", " ").split():
        if p.isdigit() and len(p) == 4:
            year = int(p)
        elif p in HEB_MONTHS:
            mon = HEB_MONTHS[p]
    return f"{year:04d}-{mon:02d}" if year and mon else ""


def _month_windows():
    """חלונות חודשיים מהחודש הנוכחי אחורה (עוקף מגבלת offset<=1000 של Maya)."""
    today = date.today()
    y, m = today.year, today.month
    while True:
        start = date(y, m, 1)
        ny, nm = (y + 1, 1) if m == 12 else (y, m + 1)
        yield start, min(date(ny, nm, 1), today)
        y, m = (y - 1, 12) if m == 1 else (y, m - 1)


def list_month(session, start, end):
    frm = start.isoformat() + "T00:00:00.000Z"
    to = end.isoformat() + "T23:59:59.000Z"
    out = {}
    for page in range(1, 40):
        body = {"pageNumber": page, "fromDate": frm, "toDate": to,
                "noMeetings": False, "isSingle": False, "isIntendToTaseMember": False,
                "by": "company", "freeText": "דוח חודשי", "limit": 30,
                "offset": (page - 1) * 30}
        data = _req(session, "POST", LIST_URL, headers=HEADERS, data=json.dumps(body)).json()
        if not data:
            break
        new = 0
        for rep in data:
            rid = rep.get("id")
            title = rep.get("title") or ""
            if rid in out or "דוח חודשי" not in title:
                continue
            new += 1
            out[rid] = {
                "id": rid, "title": title, "month": _report_month(title),
                "companies": [(c.get("name") or "").strip() for c in (rep.get("companies") or [])],
                "publish": rep.get("publishDate") or rep.get("pubDate") or rep.get("date"),
            }
        if new == 0 or len(data) < 30:
            break
        time.sleep(0.3)
    return list(out.values())


def list_reports(session, months_back):
    reports, empty_run = {}, 0
    for i, (start, end) in enumerate(_month_windows()):
        if months_back is not None and i > months_back:
            break
        if i >= MAX_MONTHS:
            break
        found = list_month(session, start, end)
        for rep in found:
            reports.setdefault(rep["id"], rep)
        print(f"{start:%Y-%m}: {len(found)} דוחות (סה\"כ {len(reports)})", flush=True)
        empty_run = 0 if found else empty_run + 1
        if months_back is None and empty_run >= EMPTY_STOP:
            print(f"{EMPTY_STOP} חודשים ריקים ברצף - עוצר", flush=True)
            break
    return sorted(reports.values(), key=lambda r: r["id"])


def main():
    acct = os.environ["CF_ACCOUNT_ID"]
    token = os.environ.get("CF_R2_TOKEN") or os.environ["CF_D1_TOKEN"]
    mb = os.environ.get("MONTHS_BACK", "all").strip().lower()
    months_back = None if mb in ("", "all") else int(mb)

    r2 = R2(acct, token, os.environ.get("R2_BUCKET", "maya-reports"))
    if r2.ensure_bucket():
        print(f"נוצר bucket {r2.bucket}")
    raw = r2.get(MANIFEST_KEY)
    manifest = json.loads(raw) if raw else {"reports": {}}
    done = manifest["reports"]
    print(f"במניפסט כבר {len(done)} דוחות", flush=True)

    s = requests.Session()
    reports = list_reports(s, months_back)
    todo = [r for r in reports if str(r["id"]) not in done]
    print(f"נמצאו {len(reports)} דוחות, {len(todo)} חדשים להורדה", flush=True)

    def save():
        manifest["updated_at"] = datetime.now(timezone.utc).isoformat()
        r2.put(MANIFEST_KEY, json.dumps(manifest, ensure_ascii=False).encode("utf-8"),
               "application/json")

    hdr_get = {k: v for k, v in HEADERS.items() if k != "content-type"}
    failed = 0
    for i, rep in enumerate(todo, 1):
        rid = rep["id"]
        try:
            content = _req(s, "GET", REPORT_URL.format(id=rid), headers=hdr_get).content
        except RuntimeError as e:
            failed += 1
            print(f"[{i}/{len(todo)}] {rid} נכשל: {e}", flush=True)
            continue
        text = content.decode("utf-8-sig", errors="replace")
        rows = list(csv.reader(io.StringIO(text)))
        gz = gzip.compress(content, compresslevel=9)
        r2.put(RAW_KEY.format(id=rid), gz, "application/gzip")
        done[str(rid)] = dict(rep, rows=max(len(rows) - 1, 0), bytes=len(content),
                              gz_bytes=len(gz), sha256=hashlib.sha256(content).hexdigest(),
                              header=[h.strip() for h in rows[0]] if rows else [],
                              fetched_at=datetime.now(timezone.utc).isoformat())
        print(f"[{i}/{len(todo)}] {rid} {rep['month']} {rep['companies'][:1]}: "
              f"{len(rows) - 1} שורות, {len(content) // 1024}KB -> {len(gz) // 1024}KB", flush=True)
        if i % SAVE_EVERY == 0:
            save()
        time.sleep(0.3)
    save()

    tot_rows = sum(r.get("rows", 0) for r in done.values())
    tot_b = sum(r.get("bytes", 0) for r in done.values())
    tot_gz = sum(r.get("gz_bytes", 0) for r in done.values())
    print(f"סיום: {len(done)} דוחות ב-R2, {tot_rows:,} שורות, "
          f"{tot_b / 1e6:.1f}MB גולמי -> {tot_gz / 1e6:.1f}MB דחוס. נכשלו בריצה זו: {failed}")
    if todo and failed == len(todo):
        sys.exit(1)


if __name__ == "__main__":
    main()

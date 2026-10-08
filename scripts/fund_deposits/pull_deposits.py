# -*- coding: utf-8 -*-
"""
pull_deposits.py — שליפה חד-פעמית של שורות פיקדונות/מזומן מהדוחות החודשיים
של קרנות הנאמנות וקרנות הסל ב-Maya.

מקור (זהה ל-Revach scripts/funds_info/exposure.py):
  maya.tase.co.il/api/v1/reports/mutual-funds  (freeText "דוח חודשי")
  -> maya.tase.co.il/api/v1/reports/{id}/attachments/file?attachmentType=TXT1

שומרים את כל העמודות של כל שורה שסוג הנכס שלה ב-CODES (100/101/103/105),
מכל הדוחות בטווח (לא רק העדכני) - כדי לראות היסטוריה ותיקונים.

פלט (data/fund_deposits/):
  reports.json   - רשימת הדוחות שנמצאו (id, title, company, month) + סטטוס הורדה
  headers.json   - כותרות העמודות בכל דוח (לזיהוי שינויי פורמט)
  rows.csv       - כל השורות הרלוונטיות, כל העמודות + report_id/company/report_month
  sample_raw.csv - דוח TXT1 גולמי אחד במלואו, לעיון במבנה
"""

import csv, io, json, os, sys, time
from datetime import date, datetime, timezone

import requests

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
CODES = {100, 101, 103, 105}
OUT = os.environ.get("OUT_DIR", "data/fund_deposits")
_BACKOFF = (2, 4, 8, 16, 30, 60)


def _req(session, method, url, **kw):
    last = None
    for i, wait in enumerate((0,) + _BACKOFF):
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


def _month_windows(months_back):
    """חלונות חודשיים [from, to) מהחודש הנוכחי אחורה (עוקף מגבלת offset<=1000)."""
    today = date.today()
    y, m = today.year, today.month
    out = []
    for _ in range(months_back + 1):
        start = date(y, m, 1)
        ny, nm = (y + 1, 1) if m == 12 else (y, m + 1)
        end = min(date(ny, nm, 1), today)
        out.append((start, end))
        y, m = (y - 1, 12) if m == 1 else (y, m - 1)
    return out


def fetch_report_list(session, months_back):
    reports = {}
    for start, end in _month_windows(months_back):
        frm = start.isoformat() + "T00:00:00.000Z"
        to = end.isoformat() + "T23:59:59.000Z"
        for page in range(1, 40):
            body = {"pageNumber": page, "fromDate": frm, "toDate": to,
                    "noMeetings": False, "isSingle": False, "isIntendToTaseMember": False,
                    "by": "company", "freeText": "דוח חודשי", "limit": 30,
                    "offset": (page - 1) * 30}
            data = _req(session, "POST", LIST_URL, headers=HEADERS,
                        data=json.dumps(body)).json()
            if not data:
                break
            new = 0
            for rep in data:
                rid = rep.get("id")
                if rid in reports:
                    continue
                new += 1
                reports[rid] = {
                    "id": rid,
                    "title": rep.get("title"),
                    "companies": [(c.get("name") or "").strip() for c in (rep.get("companies") or [])],
                    "publish": rep.get("publishDate") or rep.get("pubDate") or rep.get("date"),
                }
            if new == 0 or len(data) < 30:
                break
            time.sleep(0.3)
        print(f"{start:%Y-%m}: סה\"כ {len(reports)} דוחות עד כה", flush=True)
    return list(reports.values())


def main():
    months_back = int(os.environ.get("MONTHS_BACK", "24"))
    os.makedirs(OUT, exist_ok=True)
    s = requests.Session()
    reports = fetch_report_list(s, months_back)
    # רק "דוח חודשי" (החיפוש החופשי יכול להחזיר גם דוחות אחרים)
    reports = [r for r in reports if "דוח חודשי" in (r.get("title") or "")]
    reports.sort(key=lambda r: r["id"])
    print(f"נמצאו {len(reports)} דוחות חודשיים ב-{months_back} חודשים", flush=True)

    hdr_get = {k: v for k, v in HEADERS.items() if k != "content-type"}
    all_cols, out_rows, headers_by_report = [], [], {}
    sample_saved = False
    for i, rep in enumerate(reports, 1):
        rid = rep["id"]
        try:
            r = _req(s, "GET", REPORT_URL.format(id=rid), headers=hdr_get)
        except RuntimeError as e:
            rep["status"] = f"error: {e}"
            print(f"[{i}/{len(reports)}] {rid} נכשל: {e}", flush=True)
            continue
        text = r.content.decode("utf-8-sig", errors="replace")
        if not sample_saved:
            with open(os.path.join(OUT, "sample_raw.csv"), "w", encoding="utf-8-sig") as f:
                f.write(text)
            sample_saved = True
        rows = list(csv.reader(io.StringIO(text)))
        if not rows:
            rep["status"] = "empty"
            continue
        header = [h.strip() for h in rows[0]]
        headers_by_report[rid] = header
        for h in header:
            if h not in all_cols:
                all_cols.append(h)
        try:
            ci = header.index("סוג נכס")
        except ValueError:
            rep["status"] = "no 'סוג נכס' column"
            continue
        n = 0
        for row in rows[1:]:
            try:
                code = int(float(row[ci]))
            except (ValueError, IndexError):
                continue
            if code not in CODES:
                continue
            rec = {h: (row[j] if j < len(row) else "") for j, h in enumerate(header)}
            rec.update({"_report_id": rid, "_report_title": rep["title"],
                        "_company": " | ".join(rep["companies"])})
            out_rows.append(rec)
            n += 1
        rep["status"] = "ok"
        rep["deposit_rows"] = n
        rep["total_rows"] = len(rows) - 1
        print(f"[{i}/{len(reports)}] {rid} {rep['title']}: {n} שורות", flush=True)
        time.sleep(0.3)

    cols = ["_report_id", "_report_title", "_company"] + all_cols
    with open(os.path.join(OUT, "rows.csv"), "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(out_rows)
    with open(os.path.join(OUT, "reports.json"), "w", encoding="utf-8") as f:
        json.dump({"fetched_at": datetime.now(timezone.utc).isoformat(),
                   "months_back": months_back, "codes": sorted(CODES),
                   "reports": reports}, f, ensure_ascii=False, indent=1)
    with open(os.path.join(OUT, "headers.json"), "w", encoding="utf-8") as f:
        json.dump(headers_by_report, f, ensure_ascii=False)
    ok = sum(1 for r in reports if r.get("status") == "ok")
    print(f"סיום: {ok}/{len(reports)} דוחות, {len(out_rows)} שורות פיקדון/מזומן")
    if ok == 0:
        sys.exit(1)


if __name__ == "__main__":
    main()

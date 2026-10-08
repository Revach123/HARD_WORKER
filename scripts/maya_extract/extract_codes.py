# -*- coding: utf-8 -*-
"""
extract_codes.py — שולף מהארכיון ב-R2 (ראה scripts/maya_monthly/pull_to_r2.py)
את כל השורות של קודי "סוג נכס" נבחרים, מכל הדוחות החודשיים, עם כל העמודות.

ברירת מחדל: 100/101/103/105 (מזומן ופיקדונות בבנק).

פלט (OUT_DIR, ברירת מחדל data/fund_deposits/):
  rows.csv         - כל השורות של הקודים, כל העמודות + מטא-דאטה של הדוח
  codes_catalog.csv - לכל קוד "סוג נכס" בכל הארכיון: מספר שורות, דוחות,
                      ודוגמאות לשמות - לאיתור קודים קשורים (למשל ריבית לקבל)
  headers.json     - הכותרות השונות שנמצאו ובכמה דוחות כל אחת

משתני סביבה: CF_ACCOUNT_ID, CF_R2_TOKEN (או CF_D1_TOKEN), R2_BUCKET, CODES, OUT_DIR
"""

import csv, gzip, io, json, os, sys
from collections import Counter, defaultdict

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "maya_monthly"))
from r2_client import R2

MANIFEST_KEY = "monthly/manifest.json"
RAW_KEY      = "monthly/raw/{id}.csv.gz"


def _name_cols(header):
    return [h for h in header if "שם" in h]


def main():
    acct = os.environ["CF_ACCOUNT_ID"]
    token = os.environ.get("CF_R2_TOKEN") or os.environ["CF_D1_TOKEN"]
    r2 = R2(acct, token, os.environ.get("R2_BUCKET", "funds-complete-list"))
    codes = {int(c) for c in os.environ.get("CODES", "100,101,103,105").split(",") if c.strip()}
    out = os.environ.get("OUT_DIR", "data/fund_deposits")
    os.makedirs(out, exist_ok=True)

    raw = r2.get(MANIFEST_KEY)
    if not raw:
        sys.exit("אין מניפסט ב-R2 - הרץ קודם את pull_to_r2.py")
    reports = sorted(json.loads(raw)["reports"].values(), key=lambda r: r["id"])
    print(f"{len(reports)} דוחות במניפסט; קודים: {sorted(codes)}", flush=True)

    all_cols, out_rows = [], []
    headers = Counter()
    cat = defaultdict(lambda: {"rows": 0, "reports": set(), "names": Counter()})
    for i, rep in enumerate(reports, 1):
        data = r2.get(RAW_KEY.format(id=rep["id"]))
        if data is None:
            print(f"[{i}] {rep['id']} חסר ב-R2", flush=True)
            continue
        text = gzip.decompress(data).decode("utf-8-sig", errors="replace")
        rows = list(csv.reader(io.StringIO(text)))
        if not rows:
            continue
        header = [h.strip() for h in rows[0]]
        headers[json.dumps(header, ensure_ascii=False)] += 1
        if "סוג נכס" not in header:
            print(f"[{i}] {rep['id']} בלי עמודת 'סוג נכס'", flush=True)
            continue
        ci = header.index("סוג נכס")
        name_idx = [header.index(h) for h in _name_cols(header)]
        for h in header:
            if h not in all_cols:
                all_cols.append(h)
        n = 0
        for row in rows[1:]:
            try:
                code = int(float(row[ci]))
            except (ValueError, IndexError):
                continue
            c = cat[code]
            c["rows"] += 1
            c["reports"].add(rep["id"])
            nm = " / ".join(row[j].strip() for j in name_idx if j < len(row) and row[j].strip())
            if nm:
                c["names"][nm] += 1
            if code in codes:
                rec = {h: (row[j] if j < len(row) else "") for j, h in enumerate(header)}
                rec.update({"_report_id": rep["id"], "_report_month": rep.get("month", ""),
                            "_report_title": rep.get("title", ""),
                            "_company": " | ".join(rep.get("companies", []))})
                out_rows.append(rec)
                n += 1
        print(f"[{i}/{len(reports)}] {rep['id']} {rep.get('month')}: {n} שורות", flush=True)

    meta = ["_report_id", "_report_month", "_report_title", "_company"]
    with open(os.path.join(out, "rows.csv"), "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=meta + all_cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(out_rows)
    with open(os.path.join(out, "codes_catalog.csv"), "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["סוג נכס", "שורות", "דוחות", "דוגמאות שמות (שכיחים)"])
        for code in sorted(cat):
            c = cat[code]
            w.writerow([code, c["rows"], len(c["reports"]),
                        " || ".join(n for n, _ in c["names"].most_common(15))])
    with open(os.path.join(out, "headers.json"), "w", encoding="utf-8") as f:
        json.dump([{"reports": n, "header": json.loads(h)} for h, n in headers.most_common()],
                  f, ensure_ascii=False, indent=1)
    print(f"סיום: {len(out_rows)} שורות לקודים {sorted(codes)}; {len(cat)} קודים שונים בארכיון")


if __name__ == "__main__":
    main()

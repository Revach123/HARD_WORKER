#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
scripts/legal_probe/stop_offering.py - בדיקה חד-פעמית (קריאה בלבד): דוחות על הפסקת הצעת יחידות / חידושה
(תקנה 20יד לתקנות הדוחות) והפסקת הצעה או פדיון לפי הוראת יו"ר הרשות (20טו) בקרנות הסל, 2018 עד היום.
שלב 1: סריקת קודי הטפסים (ק001-ק199) ברשימת הדוחות של מאיה (reports/etfs) לזיהוי הטופס. שלב 2: הורדת כל דוח
ושליפת מספרי הקרנות, התאריכים והנוסח. פלט: data/legal_probe/stop_offering.json
"""
import json
import os
import re
import sys
import time
from datetime import date

import requests

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "funds_info"))
import creation_limits as cl  # noqa: E402 - HEADERS, LIST_URL, _req, fetch_report_html, _clean

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "data", "legal_probe", "stop_offering.json")
FUND_RE = re.compile(r"(?:מספר\s*קרן|מס['׳]?\s*קרן)\s*:?\s*(\d{6,8})")


def list_form(session, form_id, frm, to, max_pages=40):
    out, seen = [], set()
    for page in range(1, max_pages + 1):
        body = {"pageNumber": page, "fromDate": frm, "toDate": to, "noMeetings": False, "isPriority": False,
                "isSingle": False, "isIntendToTaseMember": False, "by": "company", "formId": form_id,
                "freeText": "", "limit": 20, "offset": (page - 1) * 20}
        r = cl._req(session, "POST", cl.LIST_URL, f"{form_id} p{page}", timeout=30, headers=cl.HEADERS,
                    data=json.dumps(body))
        if r.status_code != 200:
            print(f"  {form_id} {frm[:10]}..{to[:10]} p{page}: HTTP {r.status_code} {r.text[:120]!r}")
            return out, r.status_code
        data = r.json() or []
        new = [x for x in data if x.get("id") not in seen]
        for x in new:
            seen.add(x.get("id"))
        out += new
        if not new or len(data) < 20:
            break
        time.sleep(0.15)
    return out, 200


def title(rep):
    return " ".join(str(rep.get(k) or "") for k in ("title", "subject", "formName", "formDescription", "name", "description"))


def main():
    s = requests.Session()
    today = date.today().isoformat() + "T21:00:00.000Z"
    # שלב 1: איזה טופס הוא "הפסקת הצעת יחידות / חידושה" - עמוד אחד לכל קוד, 2018 עד היום
    forms = {}
    # בדיקת שפיות: ק079 בשנה הנוכחית (עובד ב-creation_limits.py)
    y0 = date.today().year
    win = (f"{y0 - 1}-12-31T22:00:00.000Z", today)
    reps, st = list_form(s, "ק079", *win, max_pages=1)
    print("sanity ק079:", st, len(reps), json.dumps(reps[:1], ensure_ascii=False)[:600])
    for n in range(1, 200):
        fid = f"ק{n:03d}"
        reps, st = [], None
        for y in range(y0, y0 - 4, -1):   # חלון של שנה (טווח ארוך נדחה); 4 השנים האחרונות
            frm = f"{y - 1}-12-31T22:00:00.000Z"
            to = today if y == y0 else f"{y}-12-31T21:00:00.000Z"
            r_, st = list_form(s, fid, frm, to, max_pages=1)
            reps += r_
            if r_:
                break
        if reps:
            forms[fid] = {"status": st, "n_page1": len(reps), "sample": reps[0], "titles": sorted({title(r)[:120] for r in reps})[:8]}
            print(fid, len(reps), "|", " || ".join(forms[fid]["titles"][:3])[:300])
        time.sleep(0.1)
    hit = [f for f, v in forms.items() if any(re.search(r"הפסק|חידוש", t) for t in v["titles"])]
    print("candidate forms:", hit)
    # שלב 2: כל הדוחות בטפסים המועמדים, לפי שנים
    details = []
    for fid in hit:
        for y in range(date.today().year, 2017, -1):
            frm = f"{y - 1}-12-31T22:00:00.000Z"
            to = (date.today().isoformat() if y == date.today().year else f"{y}-12-31") + "T21:00:00.000Z"
            reps, _ = list_form(s, fid, frm, to)
            for rep in reps:
                rec = {"form": fid, "id": rep.get("id"), "list": rep}
                try:
                    html = cl.fetch_report_html(s, rep.get("id"))
                    text = cl._clean(re.sub(r"<script.*?</script>", " ", html, flags=re.S | re.I))
                    rec["funds"] = sorted(set(int(x) for x in FUND_RE.findall(text)))
                    k = max(text.find("הפסק"), 0)
                    rec["text"] = text[max(0, k - 400):k + 1200]
                    rec["has_stop"] = "הפסקת הצעת" in text
                    rec["has_renew"] = "חידוש הצעת" in text
                    rec["chair"] = "יושב ראש הרשות" in text
                except Exception as e:  # noqa: BLE001
                    rec["error"] = repr(e)[:200]
                details.append(rec)
                time.sleep(0.15)
            print(f"{fid} {y}: {len(reps)}")
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    json.dump({"generated": date.today().isoformat(), "forms": forms, "candidates": hit, "reports": details},
              open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print("reports:", len(details), "| with funds:", sum(1 for d in details if d.get("funds")),
          "| stop:", sum(1 for d in details if d.get("has_stop")), "| renew:", sum(1 for d in details if d.get("has_renew")),
          "| chair:", sum(1 for d in details if d.get("chair")))


if __name__ == "__main__":
    main()

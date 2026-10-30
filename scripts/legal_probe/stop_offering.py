#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
scripts/legal_probe/stop_offering.py - בדיקה חד-פעמית (קריאה בלבד): דוחות על הפסקת הצעת יחידות / חידושה
(תקנה 20יד לתקנות הדוחות) והפסקת הצעה או פדיון לפי הוראת יו"ר הרשות (20טו) בקרנות הסל, 2018 עד היום.
שלב 1: חיפוש טקסט חופשי ברשימת הדוחות של מאיה (reports/etfs), כל הטפסים. שלב 2: הורדת כל דוח שנמצא
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
QUERIES = ["הפסקת הצעת", "חידוש הצעת", "הפסקת פדיון", "הפסקת היצירה", "הפסקת יצירה"]
FUND_RE = re.compile(r"(?:מספר\s*קרן|מס['׳]?\s*קרן)\s*:?\s*(\d{6,8})")


def search(session, text, first_year=2018):
    out, seen, raw_sample = [], set(), None
    for y in range(date.today().year, first_year - 1, -1):
        frm = f"{y - 1}-12-31T22:00:00.000Z"
        to = (date.today().isoformat() if y == date.today().year else f"{y}-12-31") + "T21:00:00.000Z"
        for page in range(1, 40):
            body = {"pageNumber": page, "fromDate": frm, "toDate": to, "noMeetings": False, "isPriority": False,
                    "isSingle": False, "isIntendToTaseMember": False, "by": "company", "formId": "",
                    "freeText": text, "limit": 20, "offset": (page - 1) * 20}
            r = cl._req(session, "POST", cl.LIST_URL, f"{text} {y} p{page}", timeout=30, headers=cl.HEADERS,
                        data=json.dumps(body))
            if r.status_code != 200:
                print(f"  {text} {y} p{page}: HTTP {r.status_code} {r.text[:200]}")
                break
            data = r.json() or []
            if data and raw_sample is None:
                raw_sample = data[0]
            new = 0
            for rep in data:
                if rep.get("id") in seen:
                    continue
                seen.add(rep.get("id"))
                new += 1
                out.append(rep)
            if not data or new == 0 or len(data) < 20:
                break
            time.sleep(0.2)
    return out, raw_sample


def main():
    s = requests.Session()
    found, samples = {}, {}
    for q in QUERIES:
        reps, raw = search(s, q)
        samples[q] = raw
        print(f"query '{q}': {len(reps)} reports")
        for rep in reps:
            found.setdefault(rep.get("id"), {"rep": rep, "queries": []})["queries"].append(q)
    print(f"unique reports: {len(found)}")
    details = []
    for i, (rid, v) in enumerate(sorted(found.items(), key=lambda kv: str(kv[0]), reverse=True)):
        rec = {"id": rid, "queries": v["queries"], "list": v["rep"]}
        if i < 400:
            try:
                html = cl.fetch_report_html(s, rid)
                text = cl._clean(re.sub(r"<script.*?</script>", " ", html, flags=re.S | re.I))
                rec["funds"] = sorted(set(int(x) for x in FUND_RE.findall(text)))
                k = max(text.find("הפסקת"), 0)
                rec["text"] = text[max(0, k - 600):k + 1500]
                rec["has_stop"] = "הפסקת הצעת" in text
                rec["has_renew"] = "חידוש הצעת" in text
                rec["chair"] = "יושב ראש הרשות" in text
            except Exception as e:  # noqa: BLE001
                rec["error"] = repr(e)[:200]
            time.sleep(0.2)
        details.append(rec)
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    json.dump({"generated": date.today().isoformat(), "queries": QUERIES, "samples": samples, "reports": details},
              open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    forms = {}
    for d in details:
        k = str(d["list"].get("formId") or d["list"].get("form") or d["list"].get("formName") or "?")
        forms[k] = forms.get(k, 0) + 1
    print("forms:", json.dumps(forms, ensure_ascii=False))
    print("with funds:", sum(1 for d in details if d.get("funds")), "| stop:", sum(1 for d in details if d.get("has_stop")),
          "| renew:", sum(1 for d in details if d.get("has_renew")), "| chair:", sum(1 for d in details if d.get("chair")))


if __name__ == "__main__":
    main()

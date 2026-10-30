#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
scripts/six_holders/fetch_six.py

הודעות בעלי מניות מהותיים של חברות הנסחרות ב-SIX (שוויץ) - מאגר ההודעות הרשמי של SIX Exchange Regulation
(FinMIA סעיף 120: חובת דיווח בחציית 3/5/10/15/20/25/33⅓/50/66⅔ אחוז). חברות שוויצריות לא מגישות ESEF,
לכן זה המקור שלהן במקום הדוח השנתי.
פלט: data/six_holders/notifications.json.gz (כל ההודעות כפי שהתקבלו) + sample.json (3 ראשונות, לבדיקה).
"""

import gzip
import json
import os
import urllib.request

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
OUT = os.path.join(ROOT, "data", "six_holders")
URL = "https://www.ser-ag.com/sheldon/significant_shareholders/v1/overview.json"
UA = "Mozilla/5.0 (compatible; HARD_WORKER ownership research)"


def get(params):
    url = URL + "?" + "&".join(f"{k}={v}" for k, v in params.items())
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=300) as r:
        return json.load(r)


def main():
    os.makedirs(OUT, exist_ok=True)
    first = get({"pageSize": 50000})
    total, items = first.get("totalCount"), first.get("itemList") or []
    print(f"totalCount={total} first page={len(items)}")
    if total and len(items) < total:
        # עימוד: מנסים את שמות הפרמטרים המקובלים עד שעמוד 2 שונה מעמוד 1
        size = len(items)
        for key in ("pageNumber", "page", "pageIndex", "offset"):
            start = 1 if key != "offset" else size
            nxt = get({"pageSize": size, key: start}).get("itemList") or []
            if nxt and nxt[0] != items[0]:
                print(f"paging by {key}")
                n = start
                while nxt and len(items) < total:
                    items += nxt
                    n += 1 if key != "offset" else size
                    nxt = get({"pageSize": size, key: n}).get("itemList") or []
                break
    print(f"items={len(items)}")
    with gzip.open(os.path.join(OUT, "notifications.json.gz"), "wt", encoding="utf-8") as f:
        json.dump({"totalCount": total, "itemList": items}, f, ensure_ascii=False, separators=(",", ":"))
    json.dump(items[:3], open(os.path.join(OUT, "sample.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)


if __name__ == "__main__":
    main()

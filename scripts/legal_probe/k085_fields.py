#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ק085 (קביעת מספר יחידות מרבי): לכל קרן סל פעילה - הדוח האחרון, השדות המובנים (FieldAlias) והנוסח.
קלט: data/legal_probe/stop_offering_ק087_ק085.json + data/funds_info/creation_limits.csv. פלט: data/legal_probe/k085_latest.json"""
import csv
import json
import os
import re
import sys
import time

import requests

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "funds_info"))
import creation_limits as cl  # noqa: E402

D = os.path.join(HERE, "..", "..", "data")
src = json.load(open(os.path.join(D, "legal_probe", "stop_offering_ק087_ק085.json"), encoding="utf-8"))
active = {r["מספר קרן"] for r in csv.DictReader(open(os.path.join(D, "funds_info", "creation_limits.csv"), encoding="utf-8-sig"))}
latest = {}
for r in src["reports"]:
    if r["form"] != "ק085":
        continue
    for f in r.get("funds") or []:
        f = str(f)
        if f in active and (f not in latest or r["list"]["publishDate"] > latest[f]["list"]["publishDate"]):
            latest[f] = r
ids = sorted({r["id"] for r in latest.values()})
print(f"active ETFs with ק085: {len(latest)}; distinct latest reports: {len(ids)}")
s = requests.Session()
out = {}
for rid in ids:
    try:
        html = cl.fetch_report_html(s, rid)
        fields = [(a, v) for a, v, _ in cl.extract_fields(html)]
        body = cl._clean(re.sub(r"<(script|style).*?</\1>", " ", html, flags=re.S | re.I))
        k = body.find("קביעת")
        out[rid] = {"fields": fields, "text": body[max(0, k - 100):k + 1500]}
    except Exception as e:  # noqa: BLE001
        out[rid] = {"error": repr(e)[:200]}
    time.sleep(1.5)
json.dump({"latest": {f: {"rid": r["id"], "date": r["list"]["publishDate"][:10], "title": r["list"]["title"].strip()}
                      for f, r in latest.items()}, "reports": out},
          open(os.path.join(D, "legal_probe", "k085_latest.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
print("done", sum(1 for v in out.values() if "fields" in v), "ok")

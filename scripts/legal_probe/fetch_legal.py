#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
scripts/legal_probe/fetch_legal.py - בדיקה חד-פעמית (קריאה בלבד): מקורות חוקיים על יצירה ופדיון בקרנות סל
ובקרן הטכנולוגיה העילית (אי.בי.אי. TECH FUND, 1142538), כולל הוראות המעבר מ-2018.
מוריד טקסטים (ויקיטקסט raw, PDF -> טקסט) ומידע מאיה על הקרן -> data/legal_probe/ (בענף הפיתוח בלבד).
"""
import io
import json
import os
import urllib.parse

import requests
from pypdf import PdfReader

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "data", "legal_probe")
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0 Safari/537.36"}
WS = "https://he.wikisource.org/w/index.php?action=raw&title="
DOCS = {
    "regs_index_funds_2018": WS + urllib.parse.quote("תקנות השקעות משותפות בנאמנות (קרנות מחקות)"),
    "regs_hitech_fund_2016": WS + urllib.parse.quote("תקנות השקעות משותפות בנאמנות (קרן טכנולוגיה עילית)"),
    "law_joint_investment_1994": WS + urllib.parse.quote("חוק השקעות משותפות בנאמנות"),
    "isa_hitech_regs_pdf": "https://www.new.isa.gov.il/images/Fittings/isa/asset_library_pic/al_legislation_asset_library/legislation_pdf_pages-62eba7b928ea2/hashkaot_tec.pdf",
    "knesset_2018_isa_version_pdf": "https://fs.knesset.gov.il/20/Committees/20_cs_bg_492697.pdf",
    "ayalon_startup_hitech_prospectus_pdf": "https://mayafiles.tase.co.il/rpdf/1137001-1138000/P1137870-01.pdf",
    "amendment28_2017_pdf": "https://fs.knesset.gov.il/20/law/20_lsr_390376.pdf",
    "amendment28_ws": WS + urllib.parse.quote("חוק השקעות משותפות בנאמנות (תיקון מס׳ 28)"),
    "amendment28_ws2": WS + urllib.parse.quote("חוק השקעות משותפות בנאמנות (תיקון מס' 28)"),
    "regs_reports_20yd_ws": WS + urllib.parse.quote("תקנות השקעות משותפות בנאמנות (דוחות)"),
    "ibi_hitech_page": "https://www.ibi.co.il/hitechoptions/invest-hitech-funds/",
    "ibi_mgr_report_1566254": "https://mayafiles.tase.co.il/RHtm/1566001-1567000/H1566254.htm",
}
MAYA = "https://maya.tase.co.il/api/v1"
MH = dict(UA, **{"accept": "application/json, text/plain, */*", "accept-language": "he-IL",
                 "content-type": "application/json", "referer": "https://maya.tase.co.il/he/funds/1142538"})


def save(name, content, ext):
    with open(os.path.join(OUT, f"{name}.{ext}"), "wb" if isinstance(content, bytes) else "w") as f:
        f.write(content)


def main():
    os.makedirs(OUT, exist_ok=True)
    log = {}
    only = [x for x in os.environ.get("ONLY", "").split(",") if x]
    for name, url in DOCS.items():
        if only and name not in only:
            continue
        try:
            r = requests.get(url, headers=UA, timeout=180)
            log[name] = [url, r.status_code, len(r.content)]
            if r.status_code != 200:
                continue
            if url.lower().endswith(".pdf") or r.content[:4] == b"%PDF":
                text = "\n".join((p.extract_text() or "") for p in PdfReader(io.BytesIO(r.content)).pages)
                save(name, text, "txt")
            else:
                save(name, r.content, "txt" if "action=raw" in url else "html")
        except Exception as e:  # noqa: BLE001
            log[name] = [url, repr(e)[:200], r.headers.get("Content-Type") if "r" in dir() else None]
            try:
                save(name + "_raw", r.content[:300000], "bin")
            except Exception:  # noqa: BLE001
                pass
    json.dump(log, open(os.path.join(OUT, "_log2.json" if only else "_log.json"), "w"), ensure_ascii=False, indent=1)
    if only:
        print(json.dumps(log, ensure_ascii=False, indent=1))
        return
    # מאיה: רשומת הקרן ב-funds/all + נקודות קצה אפשריות לפרטי הקרן ולדוחותיה
    s = requests.Session()
    try:
        r = s.post(MAYA + "/funds/all", headers=dict(MH, referer="https://maya.tase.co.il/he/funds/all"),
                   data=json.dumps({"pageSize": 5000, "pageNumber": 1}), timeout=120)
        allf = r.json()
        rec = [f for f in allf if str(f.get("fundId")) == "1142538"]
        save("maya_funds_all_1142538", json.dumps(rec, ensure_ascii=False, indent=1), "json")
        types = {}
        for f in allf:
            types[f.get("fundType")] = types.get(f.get("fundType"), 0) + 1
        log["maya_funds_all"] = [r.status_code, len(allf), types]
    except Exception as e:  # noqa: BLE001
        log["maya_funds_all"] = repr(e)[:200]
    for path in ("/funds/1142538", "/funds/1142538/details", "/funds/1142538/reports", "/funds/details/1142538"):
        try:
            r = s.get(MAYA + path, headers=MH, timeout=60)
            log["maya" + path] = [r.status_code, len(r.content)]
            if r.status_code == 200:
                save("maya" + path.replace("/", "_"), r.content, "json")
        except Exception as e:  # noqa: BLE001
            log["maya" + path] = repr(e)[:200]
    for kind in ("funds", "mutualfunds", "etfs"):
        body = {"pageNumber": 1, "fromDate": "2016-01-01T00:00:00.000Z", "toDate": "2026-12-31T21:00:00.000Z",
                "noMeetings": False, "isPriority": False, "isSingle": False, "isIntendToTaseMember": False,
                "by": "company", "formId": "", "freeText": "TECH FUND", "limit": 100, "offset": 0}
        try:
            r = s.post(MAYA + f"/reports/{kind}", headers=MH, data=json.dumps(body), timeout=60)
            log[f"maya_reports_{kind}"] = [r.status_code, len(r.content)]
            if r.status_code == 200:
                save(f"maya_reports_{kind}_techfund", r.content, "json")
        except Exception as e:  # noqa: BLE001
            log[f"maya_reports_{kind}"] = repr(e)[:200]
    json.dump(log, open(os.path.join(OUT, "_log.json"), "w"), ensure_ascii=False, indent=1)
    print(json.dumps(log, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()

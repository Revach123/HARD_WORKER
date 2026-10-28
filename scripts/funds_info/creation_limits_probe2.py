# -*- coding: utf-8 -*-
"""
creation_limits_probe2.py — איתור התשקיף העיקרי של הראל ומגדל (שבדוח השנתי שלהן
אין שורת "מגבלת שווי הזמנה") וחיפוש ניסוח מגבלת יצירה בתוכו.
פלט: data/funds_info/_debug/probe/prospectus_*.txt + run.log
"""
import json, os, re, sys, time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import creation_limits as cl
import creation_limits_probe as pr
import requests

TARGETS = ("הראל", "מגדל")
KW = re.compile(r"מזער|מירבי|מרבי|מגבלה לשווי|מגבלת שווי|מספר יחידות|ק079|ק79")


def list_free(s, text, frm, to):
    out, seen = [], set()
    for page in range(1, 40):
        body = {"pageNumber": page, "fromDate": frm, "toDate": to, "noMeetings": False,
                "isPriority": False, "isSingle": False, "isIntendToTaseMember": False,
                "by": "company", "formId": "", "freeText": text, "limit": 20,
                "offset": (page - 1) * 20}
        r = cl._req(s, "POST", cl.LIST_URL, f"free {text} p{page}", timeout=30,
                    headers=cl.HEADERS, data=json.dumps(body))
        if r.status_code != 200:
            print(f"  HTTP {r.status_code} {r.text[:200]}")
            break
        data = r.json() or []
        new = [x for x in data if x.get("id") not in seen]
        for x in new:
            seen.add(x["id"]); out.append(x)
        if not new:
            break
        time.sleep(0.2)
    return out


URLS = [
    "rpdf/1643001-1644000/P1643524-00.pdf",   # תשקיף עיקרי 30.1.2025 (חשד: מגדל)
    "rpdf/1588001-1589000/P1588270-00.pdf",   # תשקיף עיקרי 2.5.2024
    "rpdf/1594001-1595000/P1594987-00.pdf",   # תשקיף עיקרי 2.6.2024
    "rpdf/1349001-1350000/P1349007-00.pdf",   # תשקיף עיקרי 1.2.2021
]
MGR = re.compile(r"(הראל|מגדל|קסם|מיטב|תכלית|אי\.?בי\.?אי|איביאי|מור|ילין|אנליסט)\s*קרנות\s*נאמנות")


def main():
    os.makedirs(pr.OUT, exist_ok=True)
    s = requests.Session()
    for u in URLS:
        r = cl._req(s, "GET", cl.FILES_BASE + u, u, timeout=180, headers=cl.GET_HEADERS)
        print(f"[probe2] {u}: HTTP {r.status_code} {len(r.content)} bytes")
        if r.status_code != 200:
            continue
        import annual_terms as at
        text = at.pdf_to_text(r.content)
        mgr = MGR.search(text[:5000])
        print(f"[probe2] {u}: מנהל={mgr.group(0) if mgr else '?'} chars={len(text)}")
        with open(os.path.join(pr.OUT, "prospectus_" + u.split("/")[-1] + ".txt"), "w", encoding="utf-8") as f:
            f.write(text)
        hits = [ln.strip() for ln in text.splitlines() if KW.search(ln)
                and "המרביים" not in ln and "המרבית" not in ln and "ומרבית" not in ln]
        print(f"[probe2] {u}: {len(hits)} שורות רלוונטיות")
        for ln in hits[:25]:
            print("    " + ln[:200])
        time.sleep(1)


if __name__ == "__main__":
    main()

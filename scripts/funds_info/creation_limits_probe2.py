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


def main():
    os.makedirs(pr.OUT, exist_ok=True)
    s = requests.Session()
    reps = list_free(s, "תשקיף", "2025-06-30T21:00:00.000Z", cl.date.today().isoformat() + "T21:00:00.000Z")
    print(f"[probe2] {len(reps)} דוחות עם 'תשקיף'")
    picked = {}
    for rep in sorted(reps, key=lambda r: r["id"], reverse=True):
        cos = [(c.get("name") or "").strip() for c in (rep.get("companies") or [])]
        title = rep.get("title") or ""
        print(f"  {rep['id']} {cos} {title[:90]}")
        for t in TARGETS:
            if t not in picked and any(t in c for c in cos) and "תשקיף" in title and "קרן חדשה" not in title:
                picked[t] = rep
    for t, rep in picked.items():
        m = pr.meta(s, rep["id"])
        url, text = pr.pdf_text(s, m)
        print(f"[probe2] {t} {rep['id']} {url} chars={len(text or '')}")
        if not text or text.startswith("HTTP"):
            continue
        with open(os.path.join(pr.OUT, f"prospectus_{t}_{rep['id']}.txt"), "w", encoding="utf-8") as f:
            f.write(text)
        hits = [ln.strip() for ln in text.splitlines() if KW.search(ln)
                and "המרביים" not in ln and "המרבית" not in ln]
        print(f"[probe2] {t}: {len(hits)} שורות רלוונטיות")
        for ln in hits[:40]:
            print("    " + ln[:200])


if __name__ == "__main__":
    main()

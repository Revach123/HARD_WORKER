# -*- coding: utf-8 -*-
"""fund_probe.py — בדיקת קרן בודדת במאיה: רשומת funds/all המלאה + דיווחים אחרונים שמזכירים
את מספר הקרן או את שמה (כותרות + טקסט ה-htm של כל דיווח).
שימוש: python fund_probe.py FUND_ID [DAYS] -> data/funds_info/_debug/fund_<id>/"""
import json, os, re, sys, time
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import creation_limits as cl
import requests


def search(s, text, days):
    frm = (date.today() - timedelta(days=days)).isoformat() + "T21:00:00.000Z"
    to = date.today().isoformat() + "T21:00:00.000Z"
    out, seen = [], set()
    for page in range(1, 10):
        body = {"pageNumber": page, "fromDate": frm, "toDate": to, "noMeetings": False,
                "isPriority": False, "isSingle": False, "isIntendToTaseMember": False,
                "by": "company", "formId": "", "freeText": text, "limit": 20, "offset": (page - 1) * 20}
        r = cl._req(s, "POST", cl.LIST_URL, f"search {text} p{page}", timeout=30,
                    headers=cl.HEADERS, data=json.dumps(body))
        print(f"[probe] search '{text}' p{page}: HTTP {r.status_code}")
        if r.status_code != 200:
            break
        data = [x for x in (r.json() or []) if x.get("id") not in seen]
        for x in data:
            seen.add(x["id"]); out.append(x)
        if not data:
            break
        time.sleep(1)
    return out


def main(fid, days):
    out = os.path.join(cl.DEBUG_DIR, f"fund_{fid}")
    os.makedirs(out, exist_ok=True)
    s = requests.Session()
    funds = cl.fetch_all_funds(s)
    rec = funds.get(fid)
    json.dump(rec, open(os.path.join(out, "funds_all.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"[probe] funds/all: {json.dumps(rec, ensure_ascii=False)[:1500]}")
    # קרנות קסם ממונפות/הפוכות אחרות על ת"א 35 — להשוואת מחירים
    for f in funds.values():
        n = f.get("name") or ""
        if f.get("managerName", "").startswith("קסם") and ("35" in n) and ("פי" in n or "חסר" in n or "ממ" in n):
            print(f"[probe]  peer {f['fundId']} {n}: רכישה {f.get('purchasePrice')} פדיון {f.get('redemptionPrice')} {f.get('ratesAsOf')}")
    hits = search(s, str(fid), days)
    name = (rec or {}).get("name") or ""
    if name:
        hits += [h for h in search(s, name, days) if h["id"] not in {x["id"] for x in hits}]
    hits.sort(key=lambda x: x["id"], reverse=True)
    for h in hits[:15]:
        line = f"{h['id']} {h.get('publishDate', '')[:10]} {h.get('formId', '')} {h.get('title', '')}"
        print(f"[probe] report {line}")
        try:
            html = cl.fetch_report_html(s, h["id"])
            txt = cl._clean(html)
            open(os.path.join(out, f"report_{h['id']}.txt"), "w", encoding="utf-8").write(line + "\n" + txt)
            for m in re.finditer(r".{0,160}(עמל|יציר|פירוק|מיזוג|השעי|הפסק|מחיק|הודע).{0,200}", txt):
                print(f"[probe]     … {m.group(0)[:330]}")
                break
        except Exception as e:
            print(f"[probe]     htm: {e}")
        time.sleep(1)


if __name__ == "__main__":
    main(int(sys.argv[1]), int(sys.argv[2]) if len(sys.argv) > 2 else 120)

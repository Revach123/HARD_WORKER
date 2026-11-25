# -*- coding: utf-8 -*-
"""
nonq_check.py - בלי Gemini: האם השמות שחולצו ממצגות / הודעות (דוחות שינויים שאינם רבעוניים) הם מידע אמיתי או רעש.

מדגם דוחות כאלה שבחילוץ שלהם יש שמות שלא מופיעים בדוח השנתי או הרבעוני של אותה חברה ("שמות חדשים"). לכל שם חדש
בודקים בטקסט הדוח עצמו (pdftotext): האם השם מופיע, והאם באותו עמוד יש סימן החזקה סמוך (אחוז, "חברה בת", "מוחזקת",
"subsidiary", "held"...). מספרים בלבד (ריפו ציבורי).

הרצה:  python nonq_check.py [N]
"""
import io
import json
import random
import re
import sys
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor

from pypdf import PdfReader

import maya_reports_client as mrc
import report_selector as rs_
import run_small_batch as rsb

OWN = re.compile(r"\d\s*%|%\s*\d|חבר(?:ה|ות)\s+(?:ה)?בת|מוחזק|החזק|אחזק|בבעלות|שותפות|subsidiar|held|owned|holding|"
                 r"interest|stake|joint\s+venture|רכיש|רכשה|מכיר|מיזוג", re.I)
LEGAL = re.compile(r"בע[\"״]?מ|\bltd\b|\binc\b|\bllc\b|\bl\.?p\b|\bgmbh\b|\bb\.v\b|שותפות", re.I)


def key(n):
    return re.sub(r"[\s\"'״׳`.,()\-–]|בע\"?מ|בעמ", "", str(n or "").lower())


def cat(t):
    if rs_.is_periodic_change(t):
        return "quarterly"
    return "presentation" if "מצגת" in t else "other"


def main():
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 30
    d = json.load(open(".maya_cache/change_merged.json"))
    rs = d["reports"]
    rs = list(rs.values()) if isinstance(rs, dict) else rs
    byid = {str(r["id"]): r for r in rs}
    known = defaultdict(set)
    nonq = {}
    for line in open("private_subsidiaries.jsonl", encoding="utf-8"):
        try:
            r = json.loads(line)
        except ValueError:
            continue
        rid, hp = str(r.get("report_id")), str(r["parent_hp"])
        c = cat(byid[rid].get("title") or "") if rid in byid else "annual"
        names = {e.get("company") for e in r.get("change_events") or []} | {s.get("name") for s in r.get("subsidiaries") or []}
        names = {x for x in names if x}
        if c in ("quarterly", "annual"):
            known[hp] |= {key(x) for x in names}
        else:
            nonq.setdefault((rid, hp), [c, set()])[1].update(names)
    cands = []
    for (rid, hp), (c, names) in nonq.items():
        new = {x for x in names if key(x) and key(x) not in known[hp]}
        if new:
            cands.append((rid, hp, c, sorted(new)))
    random.seed(7)
    sample = random.sample(cands, min(n, len(cands)))
    print(f"NONQ דוחות לא-רבעוניים עם שמות חדשים: {len(cands)}; מדגם {len(sample)}")

    def one(item):
        rid, hp, c, new = item
        m = byid[rid]
        try:
            url = "https://mayafiles.tase.co.il/" + next(a["url"] for a in m["attachments"]
                                                         if a.get("fileType", "").startswith("pdf"))
            pdf = mrc.download_report_file(url)
            n_pages = len(PdfReader(io.BytesIO(pdf)).pages)
            texts = rsb._page_texts(pdf, n_pages) or []
        except Exception:
            return None
        pages = []
        for t in texts:
            t2 = t + "\n" + "\n".join(line[::-1] for line in t.splitlines())
            pages.append((key(t2), t2))
        res = Counter()
        for name in new:
            k = key(name)
            res["names"] += 1
            if LEGAL.search(name):
                res["legal_form"] += 1
            hit = [t2 for kt, t2 in pages if k and k in kt]
            if not hit:
                res["not_in_text"] += 1
                continue
            res["in_text"] += 1
            if any(OWN.search(t2) for t2 in hit):
                res["with_ownership_context"] += 1
        return c, res

    with ThreadPoolExecutor(8) as pool:
        out = [x for x in pool.map(one, sample) if x]
    tot = defaultdict(Counter)
    for c, res in out:
        tot[c].update(res)
        tot[c]["reports"] += 1
    for c, res in tot.items():
        print(f"NONQ {c}: דוחות {res['reports']}, שמות חדשים {res['names']}, עם צורת התאגדות {res['legal_form']}, "
              f"מופיעים בטקסט {res['in_text']}, עם סימן החזקה באותו עמוד {res['with_ownership_context']}, "
              f"לא נמצאו בטקסט {res['not_in_text']}")


if __name__ == "__main__":
    sys.exit(main())

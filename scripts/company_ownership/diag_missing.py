# -*- coding: utf-8 -*-
"""
diag_missing.py - בלי Gemini: למה חסרות חברות בת? לכל פריט בסט האמת שחילוץ מסוים פספס - מה הסיבה הסבירה:
  page_not_sent  - העמוד שלו לא נשלח בכלל (סינון העמודים)
  image_only     - העמוד נשלח, אבל השם לא בטקסט העמוד (תרשים / תמונה) - המודל צריך לקרוא תמונה
  omitted_dense  - העמוד נשלח והשם בטקסט, בעמוד צפוף (10+ פריטים מסט האמת) - המודל "דילג" בטבלה ארוכה
  omitted        - העמוד נשלח והשם בטקסט, עמוד לא צפוף
וגם: בין שתי ריצות זהות (control מול media-low) - הפריטים שנמצאו רק באחת: באילו עמודים (צפיפות).
מספרים בלבד (ריפו ציבורי).

הרצה:  python diag_missing.py
"""
import io
import json
import re
import sys
from collections import Counter, defaultdict

from pypdf import PdfReader

import maya_reports_client as mrc
import run_small_batch as rsb
import score_truth as st

B = "model_test/subs_gemini-3.6-flash_low_chunk_filter_charts-gemini-4-hydrogen_v2"
RUNS = {"control": B + "_control.jsonl", "media": B + "_media-low.jsonl",
        "H-low": "model_test/subs_gemini-4-hydrogen_low_chunk_formal_v2_media-low_compact.jsonl"}


def found_keys(path, rid):
    out = set()
    for line in open(path, encoding="utf-8"):
        r = json.loads(line)
        if str(r["report_id"]) == rid:
            out |= {st.key(s.get("name")) for s in r.get("subsidiaries") or []}
    return out


def main():
    truth = st.load_truth()
    plan = json.load(open("selection_plan.json", encoding="utf-8"))
    by_rid = {}
    for cid, e in plan.items():
        s = e.get("snapshot") or {}
        if s.get("report_id"):
            by_rid[str(s["report_id"])] = s
    causes = {k: Counter() for k in RUNS}
    cand = Counter()
    diff_density = Counter()
    page_stats = Counter()
    for rep in truth:
        rid = str(rep["report_id"])
        snap = by_rid.get(rid)
        if not snap:
            print(f"DIAG אין דוח בתוכנית ל-{rid}")
            continue
        files = []
        for url in [snap["pdf_url"]] + list(snap.get("extra_pdfs") or []):
            try:
                pdf = mrc.download_report_file(url)
                n = len(PdfReader(io.BytesIO(pdf)).pages)
                texts = rsb._page_texts(pdf, n) or [""] * n
                rel = set(rsb._relevant_pages(pdf, n) or range(n))
                formal = set(rsb._formal_pages(pdf, n) or [])
                files.append((texts, rel, formal))
            except Exception as e:
                print(f"DIAG {rid}: קובץ נכשל ({type(e).__name__})")
        items = [it for it in rep["items"] if it["label"] in ("entity", "asset")]
        # רשימת מועמדים חינמית מכל עמודי הדוח: שורות עם צורת התאגדות, או שורות עם אחוז ושם
        lines = set()
        for texts, rel, formal in files:
            for t in texts:
                for line in t.splitlines():
                    for ln in (line, line[::-1]):
                        if rsb.ENTITY.search(ln) or (re.search(r"\d\s*%|%\s*\d", ln) and len(re.findall(r"[A-Za-z\u05d0-\u05ea]", ln)) >= 4):
                            lines.add(st.key(line))
                            break
        cand["candidates"] += len(lines)
        for it in items:
            names = {st.key(n) for n in [it["name"]] + list(it.get("alt_names") or [])}
            cand["items"] += 1
            if any(k and any(k in ln for ln in lines) for k in names):
                cand["covered"] += 1
        dens = Counter(it.get("page") for it in items)
        got = {k: found_keys(p, rid) for k, p in RUNS.items()}
        for it in items:
            names = {st.key(n) for n in [it["name"]] + list(it.get("alt_names") or [])}
            p = it.get("page")
            p0 = (int(p) - 1) if str(p).isdigit() else None
            # העמוד בכל קובץ שבו יש לו טקסט שמכיל את השם (דוח עם כמה קבצים - page_reference לא מציין קובץ)
            in_text = sent_rel = sent_formal = False
            for texts, rel, formal in files:
                if p0 is None or p0 >= len(texts):
                    continue
                t = texts[p0]
                t2 = st.key(t + "\n" + "\n".join(line[::-1] for line in t.splitlines()))
                if any(k and k in t2 for k in names):
                    in_text = True
                sent_rel |= p0 in rel
                sent_formal |= p0 in formal
            page_stats["items"] += 1
            page_stats["name_in_page_text"] += in_text
            page_stats["page_in_relevant_filter"] += sent_rel
            page_stats["page_in_formal_filter"] += sent_formal
            for run, keys in got.items():
                if names & keys:
                    causes[run]["found"] += 1
                    continue
                sent = sent_formal if run == "H-low" else sent_rel
                if p0 is None:
                    causes[run]["no_page_ref"] += 1
                elif not sent:
                    causes[run]["page_not_sent"] += 1
                elif not in_text:
                    causes[run]["image_only"] += 1
                elif dens[p] >= 10:
                    causes[run]["omitted_dense"] += 1
                else:
                    causes[run]["omitted"] += 1
            a, b = bool(names & got["control"]), bool(names & got["media"])
            if a != b:
                diff_density["dense(10+)" if dens[p] >= 10 else "sparse"] += 1
            else:
                diff_density["same"] += 1
    print(f"DIAG רשימת מועמדים (בלי AI, כל העמודים): {cand['candidates']} שורות ב-10 הדוחות; "
          f"מכסה {cand['covered']}/{cand['items']} מפריטי סט האמת")
    print("DIAG פריטים: " + " ".join(f"{k}={v}" for k, v in page_stats.items()))
    for run, c in causes.items():
        print(f"DIAG {run}: " + " ".join(f"{k}={c[k]}" for k in
                                         ("found", "page_not_sent", "image_only", "omitted_dense", "omitted", "no_page_ref")))
    print("DIAG שתי ריצות זהות (control מול media): " + " ".join(f"{k}={v}" for k, v in diff_density.items()))


if __name__ == "__main__":
    sys.exit(main())

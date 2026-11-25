# -*- coding: utf-8 -*-
"""
estimate_changes.py - בדיקת חיסכון בדוחות השינויים, בלי Gemini.
מדגם דוחות רבעוניים שכבר חולצו (יש להם change_events עם page_reference): כמה עמודים בוחר _change_pages לעומת
_relevant_pages / כל הדוח, וכמה מהאירועים שכבר חולצו נמצאים בעמודים שנבחרו (כיסוי).
"""
import io, json, random, re, sys
from concurrent.futures import ThreadPoolExecutor
from pypdf import PdfReader
import maya_reports_client as mrc
import run_small_batch as rsb

d = json.load(open(".maya_cache/change_merged.json"))
rs = d["reports"]; rs = list(rs.values()) if isinstance(rs, dict) else rs
byid = {str(r["id"]): r for r in rs}
isq = lambda t: bool(re.search(r"דוח\s+רבעון|חצי\s+שנתי", t or "")) and not re.search(r"מועד\s+פרסום", t or "")
recs = {}
for line in open("private_subsidiaries.jsonl", encoding="utf-8"):
    try:
        r = json.loads(line)
    except ValueError:
        continue
    m = byid.get(str(r.get("report_id")))
    evs = [e for e in r.get("change_events") or [] if str(e.get("page_reference")).isdigit()]
    if m and isq(m.get("title")) and evs:
        recs[str(r["report_id"])] = (m, evs)
random.seed(4)
sample = random.sample(list(recs.items()), min(int(sys.argv[1]) if len(sys.argv) > 1 else 60, len(recs)))

def one(item):
    rid, (m, evs) = item
    try:
        url = "https://mayafiles.tase.co.il/" + next(a["url"] for a in m["attachments"] if a.get("fileType", "").startswith("pdf"))
        pdf = mrc.download_report_file(url)
        n = len(PdfReader(io.BytesIO(pdf)).pages)
        ch = set(rsb._change_pages(pdf, n) or [])
        rel = set(rsb._relevant_pages(pdf, n) or range(n))
        pages = [int(e["page_reference"]) - 1 for e in evs]
        return n, len(ch), len(rel), sum(1 for p in pages if p in ch), sum(1 for p in pages if p in rel), len(pages)
    except Exception as e:
        return None

with ThreadPoolExecutor(8) as pool:
    res = [x for x in pool.map(one, sample) if x]
n, ch, rel, cch, crel, ev = (sum(x[i] for x in res) for i in range(6))
print(f"CHG {len(res)} דוחות רבעוניים, {n} עמודים: סינון שינויים {ch} עמ' ({100*ch//max(1,n)}%), סינון רגיל {rel} ({100*rel//max(1,n)}%)")
print(f"CHG כיסוי אירועים שכבר חולצו ({ev}): סינון שינויים {cch} ({100*cch//max(1,ev)}%), סינון רגיל {crel} ({100*crel//max(1,ev)}%)")

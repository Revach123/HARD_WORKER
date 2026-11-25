# -*- coding: utf-8 -*-
"""
change_test.py - בדיקה (Gemini) של סינון העמודים לדוחות השינויים, בלי לגעת בנתוני הייצור.

מדגם דוחות רבעוניים שכבר חולצו (כמו estimate_changes.py). כל דוח מחולץ פעמיים באותן הגדרות (מודל, חשיבה, הנחיה,
חלונות של 20 עמודים): פעם כל העמודים, ופעם רק עמודי אירועי השינוי (GEMINI_PAGE_FILTER_CHANGES=changes).
משווים את אירועי השינוי שנמצאו (חברה + סוג אירוע): כמה מאירועי "כל העמודים" נמצאו גם בסינון, וכמה טוקנים נחסכו.
הפלט: model_test/changes_{all,filtered}.jsonl ושורות CHGTEST (מספרים בלבד).

הרצה:  python change_test.py [N]
"""
import json
import os
import random
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor

os.makedirs("model_test", exist_ok=True)

import extract_subsidiaries as ex          # noqa: E402
import run_small_batch as rsb              # noqa: E402

_orig_save = ex.save_extraction_json
PHASE = {"out": None}
ex.save_extraction_json = lambda **kw: _orig_save(**{**kw, "out_path": PHASE["out"]})
# התקדמות חלונות - רק בזיכרון (לא נוגעים במאגר הייצור, ולא ממשיכים התקדמות של ריצה אחרת)
_PROG = {}
rsb.CHUNK_PROGRESS_GET = lambda rid: _PROG.get((PHASE["out"], rid))
rsb.CHUNK_PROGRESS_PUT = lambda rid, cid, kind, chunks: _PROG.__setitem__((PHASE["out"], rid), chunks)


def _key(name):
    return re.sub(r"[\s\"'״׳.,()\-]|בע\"?מ|בעמ", "", str(name or ""))


def events(path):
    out = {}
    for line in open(path, encoding="utf-8") if os.path.exists(path) else []:
        try:
            r = json.loads(line)
        except ValueError:
            continue
        out[str(r["report_id"])] = {(_key(e.get("company")), e.get("event_type")) for e in r.get("change_events") or []}
    return out


def usage(path):
    u = {"calls": 0, "in": 0, "out": 0, "think": 0, "cached": 0}
    for line in open(path) if os.path.exists(path) else []:
        kv = dict(x.split("=", 1) for x in line.split()[1:] if "=" in x)
        u["calls"] += 1
        for k in ("in", "out", "think", "cached"):
            u[k] += int(kv.get(k, 0))
    return u


def main():
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 15
    d = json.load(open(".maya_cache/change_merged.json"))
    rs = d["reports"]
    rs = list(rs.values()) if isinstance(rs, dict) else rs
    byid = {str(r["id"]): r for r in rs}
    recs = {}
    for line in open("private_subsidiaries.jsonl", encoding="utf-8"):
        try:
            r = json.loads(line)
        except ValueError:
            continue
        m = byid.get(str(r.get("report_id")))
        if m and r.get("change_events") and __import__("report_selector").is_periodic_change(m.get("title")):
            recs[str(r["report_id"])] = (str(r["parent_hp"]), m, r)
    random.seed(11)
    sample = random.sample(sorted(recs), min(n, len(recs)))
    print(f"מדגם {len(sample)} דוחות רבעוניים (מתוך {len(recs)} שכבר חולצו עם אירועי שינוי)")

    def one(rid):
        cid, m, _ = recs[rid]
        url = "https://mayafiles.tase.co.il/" + next(a["url"] for a in m["attachments"]
                                                     if a.get("fileType", "").startswith("pdf"))
        try:
            rsb.process_change_report(cid, (m.get("companies") or [{}])[0].get("name"),
                                      {"report_id": rid, "publish_date": m.get("publishDate"), "pdf_url": url})
        except ex.GeminiQuotaExceededError:
            print("  מכסה", flush=True)
        except Exception as e:
            print(f"  {rid}: שגיאה {type(e).__name__}: {e}", flush=True)

    t0 = time.time()
    tag = "_".join(x for x in (os.environ.get("CHG_TAG", ""), os.environ.get("CHG_TAG_EXTRA", "")) if x)  # קבצים נפרדים לכל הגדרה
    sfx = f"_{tag}" if tag else ""
    phases = [p for p in (os.environ.get("CHG_PHASES") or "all,filtered").split(",") if p]
    for phase, pf in (("all", ""), ("filtered", "changes")):
        if phase not in phases:
            continue
        PHASE["out"] = f"model_test/changes_{phase}{sfx}.jsonl"
        os.environ["GEMINI_PAGE_FILTER_CHANGES"] = pf
        os.environ["GEMINI_PAGE_FILTER"] = ""
        os.environ["GEMINI_USAGE_LOG"] = f"model_test/usage_changes_{phase}{sfx}.log"
        with ThreadPoolExecutor(6) as pool:
            list(pool.map(one, sample))
        print(f"שלב {phase}: {int(time.time() - t0)}s", flush=True)

    if tag:          # הגדרה אחרת על כל העמודים: מול הריצה הקודמת (v2 + חשיבה נמוכה) ומול הייצור
        a, f = events(f"model_test/changes_all{sfx}.jsonl"), events("model_test/changes_all.jsonl")
        print(f"CHGTEST השוואה: {tag} (כל העמודים) מול v2-low (כל העמודים) - 'בסינון' להלן = v2-low")
    else:
        a, f = events("model_test/changes_all.jsonl"), events("model_test/changes_filtered.jsonl")
    prod = {rid: {(_key(e.get("company")), e.get("event_type")) for e in recs[rid][2].get("change_events") or []}
            for rid in sample}
    both = [rid for rid in sample if rid in a and rid in f]
    ea = sum(len(a[r]) for r in both)
    ef = sum(len(f[r]) for r in both)
    common = sum(len(a[r] & f[r]) for r in both)
    names_a = sum(len({k[0] for k in a[r]}) for r in both)
    names_common = sum(len({k[0] for k in a[r]} & {k[0] for k in f[r]}) for r in both)
    pa = sum(len(prod[r]) for r in both)
    print(f"CHGTEST דוחות שהושלמו בשני השלבים: {len(both)}/{len(sample)}")
    print(f"CHGTEST אירועים (חברה+סוג): כל העמודים {ea}, בסינון {ef}, משותפים {common} "
          f"({100 * common // max(1, ea)}% מכל העמודים); בייצור (הגרסה הקיימת) {pa}")
    print(f"CHGTEST חברות עם אירוע: כל העמודים {names_a}, נמצאו גם בסינון {names_common} "
          f"({100 * names_common // max(1, names_a)}%)")
    print(f"CHGTEST מהייצור: נמצאו בכל העמודים {sum(len(prod[r] & a[r]) for r in both)}/{pa}, "
          f"בסינון {sum(len(prod[r] & f[r]) for r in both)}/{pa}")
    # האירועים שהסינון פספס - סוג האירוע בלבד (לא שמות - ריפו ציבורי)
    miss = {}
    for r in both:
        for k in a[r] - f[r]:
            miss[k[1]] = miss.get(k[1], 0) + 1
    print("CHGTEST סוגי האירועים שהסינון פספס: " + " ".join(f"{k}={v}" for k, v in sorted(miss.items(), key=lambda x: -x[1])))
    if os.environ.get("GEMINI_RECHECK") == "1":
        st = rsb.RECHECK_STATS
        print(f"CHGTEST בדיקת שלמות: חלונות {st['windows']}, נשלחו שוב {st['flagged']} ({st['calls']} קריאות), "
              f"פריטים לפני {st['before']} / בשליחה החוזרת {st['after']}")
    for phase in phases:
        u = usage(f"model_test/usage_changes_{phase}{sfx}.log")
        print(f"CHGTEST טוקנים {phase}: calls={u['calls']} in={u['in']} out={u['out']} think={u['think']} "
              f"cached={u.get('cached', 0)}")


if __name__ == "__main__":
    sys.exit(main())

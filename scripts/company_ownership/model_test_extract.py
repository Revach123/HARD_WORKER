# -*- coding: utf-8 -*-
"""
model_test_extract.py - ניסוי השוואת מודלים לחילוץ חברות הבת (בלי לגעת בנתוני הייצור).

מריץ את אותו חילוץ בדיוק (run_small_batch.process_company - דוח הסיכום של כל חברה) על חברות נבחרות, עם המודל
ורמת החשיבה שב-env (GEMINI_MODEL_OVERRIDE, GEMINI_THINKING), וכותב את התוצאה לקובץ נפרד:
  model_test/subs_<model>_<thinking>.jsonl
private_subsidiaries.jsonl, processed_reports_*.json ו-D1 לא משתנים. בסוף: לכל חברה - מספר חברות הבת מול החילוץ
הטוב ביותר שכבר קיים לאותו דוח, וסיכום טוקנים (GEMINI_USAGE).

הרצה:  python model_test_extract.py --cids 1172,1630 [--workers 6]
"""
import argparse
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor

MODEL = os.environ.get("GEMINI_MODEL_OVERRIDE", "gemini-3.6-flash")
THINK = os.environ.get("GEMINI_THINKING", "high")
os.makedirs("model_test", exist_ok=True)
TAG = f"{MODEL}_{THINK}" + ("_chunk" if os.environ.get("GEMINI_CHUNK") == "1" else "")
OUT = f"model_test/subs_{TAG}.jsonl"
os.environ.setdefault("GEMINI_USAGE_LOG", f"model_test/usage_{TAG}.log")

import extract_subsidiaries as ex          # noqa: E402
import run_small_batch as rsb              # noqa: E402

_orig_save = ex.save_extraction_json
ex.save_extraction_json = lambda **kw: _orig_save(**{**kw, "out_path": OUT})


def existing_best():
    """לכל (חברה, דוח) - מספר חברות הבת המרבי בחילוץ הקיים (הייצור), ושמותיהן."""
    best = {}
    for line in open("private_subsidiaries.jsonl", encoding="utf-8"):
        try:
            r = json.loads(line)
        except ValueError:
            continue
        k = (str(r.get("parent_hp")), str(r.get("report_id")))
        names = {s.get("name") for s in r.get("subsidiaries") or [] if s.get("name")}
        if len(names) > len(best.get(k, set())):
            best[k] = names
    return best


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cids", required=True)
    ap.add_argument("--workers", type=int, default=6)
    a = ap.parse_args()
    plan = json.load(open("selection_plan.json", encoding="utf-8"))
    cids = [c for c in a.cids.split(",") if c in plan]
    done = set()
    if os.path.exists(OUT):
        for line in open(OUT, encoding="utf-8"):
            try:
                done.add(str(json.loads(line)["parent_hp"]))
            except (ValueError, KeyError):
                pass
    todo = [c for c in cids if c not in done]
    print(f"מודל {MODEL}, חשיבה {THINK}: {len(todo)} חברות ({len(done)} כבר בקובץ)")
    t0 = time.time()

    def run(cid):
        try:
            ok = rsb.process_company(cid, plan[cid])
            print(f"  {plan[cid].get('company_name')} ({cid}): {'הושלם' if ok is True else ok}", flush=True)
        except ex.GeminiQuotaExceededError:
            print(f"  {plan[cid].get('company_name')}: מכסה", flush=True)
        except Exception as e:
            print(f"  {plan[cid].get('company_name')}: שגיאה {type(e).__name__}: {e}", flush=True)
    with ThreadPoolExecutor(max_workers=a.workers) as pool:
        list(pool.map(run, todo))

    best = existing_best()
    print(f"\n=== {MODEL} / {THINK} ({int(time.time() - t0)}s) ===")
    for line in open(OUT, encoding="utf-8") if os.path.exists(OUT) else []:
        r = json.loads(line)
        cid, rid = str(r["parent_hp"]), str(r["report_id"])
        names = {s.get("name") for s in r.get("subsidiaries") or [] if s.get("name")}
        prev = best.get((cid, rid), set())
        print(f"RESULT cid={cid} {plan.get(cid, {}).get('company_name')}: {len(names)} חברות בת "
              f"(קיים: {len(prev)}; חדשות שלא היו: {len(names - prev)}; מהקיים שחסרות: {len(prev - names)})")
    u = {"calls": 0, "in": 0, "out": 0, "think": 0}
    log = os.environ["GEMINI_USAGE_LOG"]
    if os.path.exists(log):
        for line in open(log):
            kv = dict(x.split("=", 1) for x in line.split()[1:] if "=" in x)
            u["calls"] += 1
            for k in ("in", "out", "think"):
                u[k] += int(kv.get(k, 0))
    print(f"USAGE model={MODEL} thinking={THINK} calls={u['calls']} in={u['in']} out={u['out']} think={u['think']}")


if __name__ == "__main__":
    sys.exit(main())

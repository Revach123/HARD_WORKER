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
TAG = f"{MODEL}_{THINK}" + ("_chunk" if os.environ.get("GEMINI_CHUNK") == "1" else "") + \
    ({"1": "_filter", "charts": "_charts"}.get(os.environ.get("GEMINI_PAGE_FILTER", ""), ""))
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


def probe(a):
    """לכל קובץ בדוח של כל חברה: לכל עמוד בטווח - אורך הטקסט ומניין סיומות של ישויות (LLC, LP, Inc...), וסה"כ שמות שונים
    שמסתיימים בסיומת כזו. ובהשוואה: כמה חברות בת מצא כל מודל בבדיקות הקודמות בעמודים האלה (page_reference)."""
    import collections
    import glob
    import io
    import re
    import maya_reports_client as mrc
    from pypdf import PdfReader
    lo, hi = (int(x) for x in a.probe.split("-"))
    SUF = {"LLC": r"\bL\.?L\.?C\b", "LP": r"\bL\.?P\b", "Inc": r"\bInc\b", "Ltd/Limited": r"\bLtd\b|\bLimited\b",
           "Corp": r"\bCorp(?:oration)?\b", "בע״מ": r'בע"?מ|בע״מ|מ"עב|מ״עב', "שותפות": r"שותפות|תופתוש"}
    NAME = re.compile(r"([A-Z0-9][\w&.,'\- ]{2,60}?\s(?:LLC|L\.L\.C\.|LP|L\.P\.|Inc\.?|Ltd\.?|Limited|Corp\.?))\b")
    plan = json.load(open("selection_plan.json", encoding="utf-8"))
    for cid in a.cids.split(","):
        snap = (plan.get(cid) or {}).get("snapshot") or {}
        for fi, url in enumerate([snap.get("pdf_url")] + (snap.get("extra_pdfs") or [])):
            if not url:
                continue
            pdf = mrc.download_report_file(url)
            n = len(PdfReader(io.BytesIO(pdf)).pages)
            texts = rsb._page_texts(pdf, n)
            print(f"PROBE {plan[cid].get('company_name')} ({cid}) קובץ {fi} {url.split('/')[-1]}: {n} עמודים, pdftotext={'כן' if texts else 'לא'}")
            if not texts:
                continue
            tot, names = collections.Counter(), set()
            for pg in range(lo, min(hi, n) + 1):
                t = texts[pg - 1]
                c = {k: len(re.findall(v, t)) for k, v in SUF.items()}
                tot.update(c)
                nm = {" ".join(m.split()) for m in NAME.findall(t)}
                names |= nm
                print(f"PROBE   עמ' {pg}: {len(t.strip())} תווים | " + " ".join(f"{k}={v}" for k, v in c.items() if v)
                      + f" | שמות ישויות שונים: {len(nm)}")
            print(f"PROBE   סה\"כ עמ' {lo}-{hi}: " + " ".join(f"{k}={v}" for k, v in tot.items() if v)
                  + f" | שמות ישויות שונים: {len(names)} | דוגמה: {sorted(names)[:6]}")
        for f in sorted(glob.glob("model_test/subs_*.jsonl")):
            for line in open(f, encoding="utf-8"):
                r = json.loads(line)
                if str(r.get("parent_hp")) == cid:
                    k = sum(1 for x in r.get("subsidiaries") or [] if str(x.get("page_reference")).isdigit()
                            and lo <= int(x["page_reference"]) <= hi)
                    print(f"PROBE   {f.split('/')[-1][5:-6]}: {k} חברות בת בעמודים {lo}-{hi}")


def dry(a):
    """סינון העמודים בלי Gemini: לכל דוח - כמה עמודים נבחרו, וכמה מחברות הבת בחילוץ הקיים (page_reference בקובץ הראשי)
    נמצאות בעמודים שנבחרו. רק קריאת PDF - אין עלות."""
    import io
    import maya_reports_client as mrc
    from pypdf import PdfReader
    plan = json.load(open("selection_plan.json", encoding="utf-8"))
    best = {}
    for line in open("private_subsidiaries.jsonl", encoding="utf-8"):
        try:
            r = json.loads(line)
        except ValueError:
            continue
        k = (str(r.get("parent_hp")), str(r.get("report_id")))
        if len(r.get("subsidiaries") or []) > len(best.get(k, [])):
            best[k] = r.get("subsidiaries") or []
    tot_sel = tot_pages = tot_cov = tot_refs = 0
    for cid in a.cids.split(","):
        snap = (plan.get(cid) or {}).get("snapshot") or {}
        if not snap:
            continue
        pdf = mrc.download_report_file(snap["pdf_url"])
        n = len(PdfReader(io.BytesIO(pdf)).pages)
        sel = (rsb._chart_pages(pdf, n) if os.environ.get("GEMINI_PAGE_FILTER") == "charts" else rsb._relevant_pages(pdf, n)) or list(range(n))
        refs = [s.get("page_reference") for s in best.get((cid, snap["report_id"]), [])]
        refs = [int(p) for p in refs if str(p).isdigit() and 1 <= int(p) <= n]
        cov = sum(1 for p in refs if p - 1 in set(sel))
        tot_sel += len(sel); tot_pages += n; tot_cov += cov; tot_refs += len(refs)
        print(f"DRY {plan[cid].get('company_name')} ({cid}): {len(sel)}/{n} עמודים נבחרו; "
              f"חברות בת קיימות בעמודים שנבחרו: {cov}/{len(refs)}")
        texts = rsb._page_texts(pdf, n)
        if texts is None:
            print(f"   (pdftotext לא החזיר טקסט תואם - {n} עמודים)")
            continue
        import collections
        missed = collections.Counter(p for p in refs if p - 1 not in set(sel))
        for p, c in missed.most_common(6):
            t = texts[p - 1]
            head = " ".join(t.split())[:110]
            print(f"   MISS p{p} ({c} חברות): len={len(t.strip())} ent={len(rsb.ENTITY.findall(t))} pct={t.count('%')} | {head}")
    print(f"DRY TOTAL: {tot_sel}/{tot_pages} עמודים ({100 * tot_sel // max(1, tot_pages)}%); "
          f"כיסוי חברות הבת הקיימות: {tot_cov}/{tot_refs} ({100 * tot_cov // max(1, tot_refs)}%)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cids", required=True)
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--probe", default="", help="בלי Gemini: טקסט (pdftotext) של עמודים נבחרים ומניין שמות ישויות, למשל 10-16")
    ap.add_argument("--dry", action="store_true", help="בלי Gemini: רק כמה עמודים נבחרים, וכמה מעמודי חברות הבת הקיימות מכוסים")
    a = ap.parse_args()
    if a.probe:
        return probe(a)
    if a.dry:
        return dry(a)
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

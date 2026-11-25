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
    ({"1": "_filter", "charts": "_charts", "formal": "_formal"}.get(os.environ.get("GEMINI_PAGE_FILTER", ""), "")) + \
    (f"_charts-{os.environ['GEMINI_CHART_MODEL']}" if os.environ.get("GEMINI_CHART_MODEL") else "") + \
    ("_" + os.environ["EXTRACTION_PROMPT"] if os.environ.get("EXTRACTION_PROMPT", "v1") != "v1" else "") + \
    (f"_p{os.environ['GEMINI_CHUNK_PAGES']}" if os.environ.get("GEMINI_CHUNK_PAGES") else "") + \
    (f"_media-{os.environ['GEMINI_MEDIA_RES']}" if os.environ.get("GEMINI_MEDIA_RES") else "") + \
    ("_compact" if os.environ.get("EXTRACTION_COMPACT") == "1" else "") + \
    (f"_recheck-{os.environ.get('RECHECK_MODEL') or 'same'}-{os.environ.get('RECHECK_THINKING') or 'high'}"
     if os.environ.get("GEMINI_RECHECK") == "1" else "") + \
    (f"_{os.environ['TEST_TAG']}" if os.environ.get("TEST_TAG") else "")
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


def ocr_page(pdf_bytes, page):
    """OCR לעמוד אחד: pdftoppm (300dpi) -> tesseract (אנגלית+עברית, psm 11 - טקסט מפוזר כמו בתרשים). None בלי tesseract."""
    import shutil
    import subprocess
    import tempfile
    if not (shutil.which("tesseract") and shutil.which("pdftoppm")):
        return None
    with tempfile.TemporaryDirectory() as d:
        src = os.path.join(d, "r.pdf")
        open(src, "wb").write(pdf_bytes)
        subprocess.run(["pdftoppm", "-f", str(page), "-l", str(page), "-r", "300", "-png", src, os.path.join(d, "p")],
                       capture_output=True, timeout=120)
        imgs = sorted(f for f in os.listdir(d) if f.endswith(".png"))
        if not imgs:
            return None
        r = subprocess.run(["tesseract", os.path.join(d, imgs[0]), "-", "-l", "eng+heb", "--psm", "11"],
                           capture_output=True, timeout=300)
        return r.stdout.decode("utf-8", "replace")


def why(a):
    """לכל עמוד שצוין: האם נבחר בסינון העמודים, ואיזה סימן תפס (כותרת / ריכוז ישויות ואחוזים) - ותחילת הטקסט."""
    import io
    import re
    import maya_reports_client as mrc
    from pypdf import PdfReader
    plan = json.load(open("selection_plan.json", encoding="utf-8"))
    for spec in a.why.split(";"):
        cid, fidx, pages = spec.replace("why=", "").split(":")
        snap = plan[cid]["snapshot"]
        url = ([snap["pdf_url"]] + (snap.get("extra_pdfs") or []))[int(fidx)]
        pdf = mrc.download_report_file(url)
        n = len(PdfReader(io.BytesIO(pdf)).pages)
        texts = rsb._page_texts(pdf, n)
        sel = set(rsb._relevant_pages(pdf, n) or [])
        print(f"WHY {plan[cid]['company_name']} {url.split('/')[-1]} ({n} עמ', נבחרו {len(sel)})")
        for p in [int(x) for x in pages.split(",")]:
            t = texts[p - 1] if texts else ""
            t2 = t + "\n" + "\n".join(line[::-1] for line in t.splitlines())
            k = rsb.PAGE_KEYS.search(t2)
            ent = len(rsb.ENTITY.findall(t2))
            hs = rsb.HOLD_STMT.search(t2)
            print(f"WHY  עמ' {p}: {'נבחר' if p - 1 in sel else 'לא נבחר'} | len={len(t.strip())} ent={ent} pct={t.count('%')} "
                  f"hold={hs.group(0) if hs else '-'} "
                  f"key={k.group(0) if k else '-'} | {' '.join(t.split())[:160]}")


def ocr_test(a):
    """אותם עמודים (למשל תרשים מבנה ההחזקות) נשלחים ל-Gemini פעמיים: רק ה-PDF, ו-PDF + טקסט OCR של כל עמוד כרמז.
    מדפיס מספר חברות בת, כמה עם אחוז, חפיפה עם תוצאת Hydrogen באותם עמודים, וטוקנים לכל ניסיון."""
    import glob
    import io
    import re
    import maya_reports_client as mrc
    from pypdf import PdfReader
    cid, fidx, rng = a.ocr_test.split(":")
    lo, hi = (int(x) for x in rng.split("-"))
    plan = json.load(open("selection_plan.json", encoding="utf-8"))
    snap = plan[cid]["snapshot"]
    url = ([snap["pdf_url"]] + (snap.get("extra_pdfs") or []))[int(fidx)]
    pdf = mrc.download_report_file(url)
    reader = PdfReader(io.BytesIO(pdf))
    pages = list(range(lo - 1, min(hi, len(reader.pages))))
    part = rsb._slice_pages(reader, pages)
    ocr = "\n\n".join(f"--- עמוד {p + 1} ---\n{(ocr_page(pdf, p + 1) or '').strip()}" for p in pages)
    norm = lambda x: re.sub(r"[^a-z0-9\u0590-\u05FF]", "", (x or "").lower())
    hyd = set()
    for f in glob.glob("model_test/subs_gemini-4-hydrogen*.jsonl"):
        for line in open(f, encoding="utf-8"):
            r = json.loads(line)
            if str(r.get("parent_hp")) == cid:
                hyd |= {norm(x["name"]) for x in r.get("subsidiaries") or []
                        if str(x.get("page_reference")).isdigit() and lo <= int(x["page_reference"]) <= hi}
    log = os.environ["GEMINI_USAGE_LOG"]
    print(f"OCRTEST {plan[cid].get('company_name')} {url.split('/')[-1]} עמ' {lo}-{hi}: מודל {MODEL}/{THINK}; "
          f"OCR {len(ocr)} תווים; Hydrogen מצא {len(hyd)}")
    for label, hint in (("בלי OCR", f"{url.split('/')[-1]} עמ' {lo}-{hi}"),
                        ("עם OCR", f"{url.split('/')[-1]} עמ' {lo}-{hi}\n\nטקסט OCR של העמודים המצורפים (עזר לקריאת השמות "
                                   f"בתרשים; ייתכנו שגיאות זיהוי - השמות והאחוזים הקובעים הם מה שבתמונה):\n{ocr}")):
        before = sum(1 for _ in open(log)) if os.path.exists(log) else 0
        try:
            res = rsb._call_gemini_with_quota_retry(part, hint) or {}
        except ex.GeminiQuotaExceededError as e:
            print(f"OCRTEST {label}: מכסה/קרדיט - {e}")
            return
        subs = res.get("subsidiaries") or []
        names = {norm(x.get("name")) for x in subs if x.get("name")}
        with_pct = sum(1 for x in subs if x.get("ownership_pct") is not None)
        u = {"in": 0, "out": 0, "think": 0}
        if os.path.exists(log):
            for line in list(open(log))[before:]:
                kv = dict(x.split("=", 1) for x in line.split()[1:] if "=" in x)
                for k in u:
                    u[k] += int(kv.get(k, 0))
        print(f"OCRTEST {label}: {len(names)} חברות בת ({with_pct} עם אחוז); משותפות עם Hydrogen: {len(names & hyd)}; "
              f"חדשות מול Hydrogen: {len(names - hyd)} | in={u['in']} out={u['out']} think={u['think']}")


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
            ocr_n = 0
            for pg in range(lo, min(hi, n) + 1):
                t = texts[pg - 1]
                src = ""
                if len(t.strip()) < 300:            # כמעט בלי טקסט - כנראה תמונה / תרשים: OCR
                    o = ocr_page(pdf, pg)
                    if o is not None:
                        t, src = o, " (OCR)"
                        ocr_n += 1
                        if os.environ.get("PROBE_DUMP") == "1":
                            for ln in [x for x in t.splitlines() if x.strip()][:60]:
                                print(f"OCRTXT p{pg} | {ln.strip()[:110]}")
                c = {k: len(re.findall(v, t)) for k, v in SUF.items()}
                tot.update(c)
                nm = {" ".join(m.split()) for m in NAME.findall(t)}
                names |= nm
                print(f"PROBE   עמ' {pg}{src}: {len(t.strip())} תווים | " + " ".join(f"{k}={v}" for k, v in c.items() if v)
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
    if os.environ.get("GEMINI_PAGE_FILTER") == "charts":       # כל קבצי הדוח: אילו עמודים זוהו כתרשים
        for cid in a.cids.split(","):
            snap = (plan.get(cid) or {}).get("snapshot") or {}
            for fi, url in enumerate([snap.get("pdf_url")] + (snap.get("extra_pdfs") or [])):
                if not url:
                    continue
                pdf = mrc.download_report_file(url)
                n = len(PdfReader(io.BytesIO(pdf)).pages)
                det = []
                t0 = time.time()
                sel = rsb._chart_pages(pdf, n, det) or []
                print(f"DRY {plan[cid].get('company_name')} ({cid}) קובץ {fi} {url.split('/')[-1]}: {n} עמ', "
                      f"תרשימים: {len(sel)} עמ' {[p for p, _, _ in det][:25]} ({int(time.time() - t0)}s)")
                for pg, pct, kw in det[:12]:
                    print(f"DRY    עמ' {pg}: אחוזים={pct}{' · מילת מפתח' if kw else ''}")
        return
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
    ap.add_argument("--why", default="", help="cid:file:p1,p2,... - לכל עמוד: נבחר בסינון? ולמה (בלי Gemini)")
    ap.add_argument("--ocr-test", default="", help="cid:file:from-to - אותם עמודים ל-Gemini פעם בלי ופעם עם טקסט OCR כרמז")
    ap.add_argument("--probe", default="", help="בלי Gemini: טקסט (pdftotext) של עמודים נבחרים ומניין שמות ישויות, למשל 10-16")
    ap.add_argument("--dry", action="store_true", help="בלי Gemini: רק כמה עמודים נבחרים, וכמה מעמודי חברות הבת הקיימות מכוסים")
    a = ap.parse_args()
    if a.why:
        return why(a)
    if a.ocr_test:
        return ocr_test(a)
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
    u = {"calls": 0, "in": 0, "out": 0, "think": 0, "cached": 0}
    log = os.environ["GEMINI_USAGE_LOG"]
    if os.path.exists(log):
        for line in open(log):
            kv = dict(x.split("=", 1) for x in line.split()[1:] if "=" in x)
            u["calls"] += 1
            for k in ("in", "out", "think", "cached"):
                u[k] += int(kv.get(k, 0))
    print(f"USAGE model={MODEL} thinking={THINK} calls={u['calls']} in={u['in']} out={u['out']} think={u['think']} "
          f"cached={u['cached']}")
    print(f"COST ${ex._SPENT[0]:.2f} (תקרה ${ex._BUDGET or 0})" + (" - נעצר בתקרה" if ex.budget_exceeded() else ""))
    if os.environ.get("GEMINI_RECHECK") == "1":
        print(f"RECHECK חלונות {rsb.RECHECK_STATS['windows']}, נשלחו שוב {rsb.RECHECK_STATS['flagged']} "
              f"({rsb.RECHECK_STATS['calls']} קריאות), פריטים לפני {rsb.RECHECK_STATS['before']} / בשליחה החוזרת "
              f"{rsb.RECHECK_STATS['after']}")
    if os.path.exists(OUT):
        import score_truth
        s = score_truth.score(OUT)
        if s["reports"]:
            print(f"TRUTH {TAG}: דוחות {s['reports']}, דיוק {s['precision']}% (שגויים {s['wrong']}), "
                  f"כיסוי {s['recall']}% ({s['found']}/{s['of']}), כיסוי ישויות {s['entity_recall']}%")


if __name__ == "__main__":
    sys.exit(main())

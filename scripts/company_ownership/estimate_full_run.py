# -*- coding: utf-8 -*-
"""
estimate_full_run.py - הערכת עלות של חילוץ מלא מחדש של כל הדוחות, בלי Gemini.

לכל דוח סיכום (שנתי) בתוכנית: מוריד את כל קבצי ה-PDF, סופר עמודים, מריץ את סינון העמודים (pdftotext, כמו בייצור)
וסופר עמודים נבחרים וחלונות. על מדגם (כל N-י) - גם זיהוי עמודי תרשים שהם תמונה (OCR). דוחות שינויים - מדגם.
העלות מחושבת לפי צריכת הטוקנים שנמדדה בבדיקות (24-25/11):
  3.6-flash, חשיבה נמוכה, עמודים מסוננים: ~775 טוקני קלט לעמוד, ~2,500 טוקני פלט+חשיבה לחלון
  Hydrogen על עמודי תרשים: ~1,200 טוקני קלט לעמוד, ~11,000 טוקני פלט+חשיבה לחלון
  שליחה בלי סינון (השוואה): כל העמודים, בחלונות 20 עם חפיפה 3
הרצה:  python estimate_full_run.py [--sample-every 10] [--changes-sample 150] [--workers 8]
"""
import argparse
import io
import json
import math
import random
import sys
import time
from concurrent.futures import ThreadPoolExecutor, ProcessPoolExecutor

from pypdf import PdfReader

import maya_reports_client as mrc
import run_small_batch as rsb

T = {"flash_in_page": 775, "flash_out_win": 2500, "hyd_in_page": 1200, "hyd_out_win": 11000}
PRICE = {"flash_now": (0.75, 3.75), "flash_2027": (1.50, 7.50), "hyd": (1.50, 7.50), "lite": (0.30, 2.50)}


def pages_of(url):
    try:
        pdf = mrc.download_report_file(url)
        n = len(PdfReader(io.BytesIO(pdf)).pages)
        sel = rsb._relevant_pages(pdf, n)
        return {"ok": True, "n": n, "sel": len(sel) if sel is not None else n, "pdf": pdf}
    except Exception as e:
        return {"ok": False, "err": type(e).__name__}


def charts_of(pdf):
    try:
        n = len(PdfReader(io.BytesIO(pdf)).pages)
        c = rsb._chart_pages(pdf, n) or []
        return len(c)
    except Exception:
        return 0


def report_urls(r):
    return [r["pdf_url"]] + (r.get("extra_pdfs") or [])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sample-every", type=int, default=10, help="OCR לזיהוי תרשימים - כל N-י דוח שנתי")
    ap.add_argument("--changes-sample", type=int, default=150)
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--limit", type=int, default=0)
    a = ap.parse_args()
    plan = json.load(open("selection_plan.json", encoding="utf-8"))
    snaps = [(cid, e["snapshot"]) for cid, e in plan.items() if e.get("snapshot")]
    if a.limit:
        snaps = snaps[:a.limit]
    changes = [(cid, c) for cid, e in plan.items() for c in e.get("changes") or [] if c.get("pdf_url")]
    random.seed(1)
    ch_sample = random.sample(changes, min(a.changes_sample, len(changes)))
    t0 = time.time()

    def do_report(item, want_charts):
        cid, r = item
        files = [pages_of(u) for u in report_urls(r)]
        ok = [f for f in files if f["ok"]]
        out = {"cid": cid, "files": len(files), "failed": len(files) - len(ok), "n": sum(f["n"] for f in ok),
               "sel": sum(f["sel"] for f in ok),
               "win_sel": sum(math.ceil(f["sel"] / rsb.CHUNK_PAGES) for f in ok),
               "win_all": sum(len(rsb._iter_page_windows(f["n"])) for f in ok),
               "pages_all": sum(sum(e - s for s, e in rsb._iter_page_windows(f["n"])) for f in ok)}
        if want_charts:
            with ProcessPoolExecutor(max_workers=2) as pp:
                cs = list(pp.map(charts_of, [f["pdf"] for f in ok]))
            out["charts"] = sum(cs)
            out["chart_win"] = sum(math.ceil(c / rsb.CHUNK_PAGES) for c in cs if c)
        return out

    with ThreadPoolExecutor(max_workers=a.workers) as pool:
        snap_res = list(pool.map(lambda x: do_report(x[1], x[0] % a.sample_every == 0), enumerate(snaps)))
        print(f"דוחות שנתיים: {len(snap_res)} ({int(time.time() - t0)}s)", flush=True)
        ch_res = list(pool.map(lambda x: do_report(x, False), ch_sample))
    print(f"מדגם דוחות שינויים: {len(ch_res)} ({int(time.time() - t0)}s)")

    def summarize(res, label, scale=1.0):
        n = sum(r["n"] for r in res) * scale
        sel = sum(r["sel"] for r in res) * scale
        ws = sum(r["win_sel"] for r in res) * scale
        wa = sum(r["win_all"] for r in res) * scale
        pa = sum(r["pages_all"] for r in res) * scale
        fail = sum(r["failed"] for r in res)
        samp = [r for r in res if "charts" in r]
        cp = (sum(r["charts"] for r in samp) / len(samp) * len(res) * scale) if samp else 0
        cw = (sum(r["chart_win"] for r in samp) / len(samp) * len(res) * scale) if samp else 0
        rep_with_charts = (sum(1 for r in samp if r["charts"]) / len(samp)) if samp else 0
        print(f"\nESTIMATE {label}: {len(res) * scale:.0f} דוחות, {n:,.0f} עמודים; אחרי סינון {sel:,.0f} ({100 * sel / max(1, n):.0f}%), "
              f"{ws:,.0f} חלונות; בלי סינון {pa:,.0f} עמודים ב-{wa:,.0f} חלונות; קבצים שנכשלו בהורדה: {fail}")
        if samp:
            print(f"ESTIMATE {label}: תרשימים (מדגם {len(samp)} דוחות): {100 * rep_with_charts:.0f}% מהדוחות, "
                  f"~{cp:,.0f} עמודי תרשים, ~{cw:,.0f} חלונות")
        tin, tout = sel * T["flash_in_page"], ws * T["flash_out_win"]
        hin, hout = cp * T["hyd_in_page"], cw * T["hyd_out_win"]
        ain, aout = pa * T["flash_in_page"], wa * T["flash_out_win"]
        cost = lambda i, o, p: i / 1e6 * p[0] + o / 1e6 * p[1]
        for nm, pf in (("מחיר מבצע (עד 31/12/2026)", "flash_now"), ("מ-1/1/2027", "flash_2027")):
            filt = cost(tin, tout, PRICE[pf]) + cost(hin, hout, PRICE["hyd"])
            full = cost(ain, aout, PRICE[pf])
            print(f"ESTIMATE {label} [{nm}]: עם סינון + Hydrogen לתרשימים: ${filt:,.2f} "
                  f"(3.6: ${cost(tin, tout, PRICE[pf]):,.2f}, Hydrogen: ${cost(hin, hout, PRICE['hyd']):,.2f}) | "
                  f"בלי סינון, כל העמודים: ${full:,.2f}")
        print(f"ESTIMATE {label}: טוקנים - 3.6 קלט {tin:,.0f} פלט {tout:,.0f}; Hydrogen קלט {hin:,.0f} פלט {hout:,.0f}")

    summarize(snap_res, "דוחות שנתיים (סיכום)")
    summarize(ch_res, "דוחות שינויים", scale=len(changes) / max(1, len(ch_res)))


if __name__ == "__main__":
    sys.exit(main())

# -*- coding: utf-8 -*-
"""
batch_test.py - בדיקה טכנית של מצב Batch ב-Gemini (חצי מחיר, תוצאה עד 24 שעות), בלי לגעת בנתוני הייצור.

לוקח 3 חלונות של 20 עמודים מדוח שנתי (חברה אחת מהתוכנית), שולח את אותה בקשה בדיוק (אותה הנחיה והגדרות כמו
call_gemini_extraction) פעם רגיל ופעם ב-Batch, ומדפיס: זמן עד תוצאה, טוקנים, ומספר החברות שחולצו בכל דרך.
מספרים בלבד.

הרצה:  python batch_test.py [cid]
"""
import io
import json
import os
import sys
import time

import requests
from pypdf import PdfReader

import extract_subsidiaries as ex
import maya_reports_client as mrc
import run_small_batch as rsb

BASE = "https://generativelanguage.googleapis.com/v1beta"


def capture_payload(pdf_bytes, model):
    """ההנחיה והגדרות הבקשה בדיוק כמו בייצור: מריצים את call_gemini_extraction עם requests.post מיורט."""
    box = {}

    class _Stop(Exception):
        pass

    orig = ex.requests.post

    def fake(url, json=None, **kw):
        box["payload"] = json
        raise _Stop()
    ex.requests.post = fake
    try:
        ex.call_gemini_extraction(pdf_bytes, filename_hint="window.pdf", max_retries=1, model=model)
    except _Stop:
        pass
    except Exception:
        pass
    finally:
        ex.requests.post = orig
    return box.get("payload")


def n_subs(text):
    try:
        d = json.loads(text)
    except Exception:
        return -1
    if isinstance(d, list):
        d = d[0] if d and isinstance(d[0], dict) else {}
    return len(d.get("subsidiaries") or []) + len(d.get("change_events") or [])


def main():
    cid = sys.argv[1] if len(sys.argv) > 1 else "746"
    model = os.environ.get("GEMINI_MODEL_OVERRIDE", "gemini-3.6-flash")
    key = os.environ["GEMINI_API_KEY"]
    hdr = {"x-goog-api-key": key, "Content-Type": "application/json"}
    plan = json.load(open("selection_plan.json", encoding="utf-8"))
    snap = plan[cid]["snapshot"]
    pdf = mrc.download_report_file(snap["pdf_url"])
    reader = PdfReader(io.BytesIO(pdf))
    n = len(reader.pages)
    wins = [(s, min(s + 20, n)) for s in range(0, min(n, 60), 20)]
    payloads = [capture_payload(rsb._slice_pdf(reader, s, e), model) for s, e in wins]
    print(f"BATCH דוח {snap['report_id']}: {n} עמ', {len(payloads)} חלונות, מודל {model}")

    # רגיל
    t0 = time.time()
    std = []
    for p in payloads:
        r = requests.post(f"{BASE}/models/{model}:generateContent", headers=hdr, json=p, timeout=300)
        if r.status_code != 200:
            print(f"BATCH רגיל: HTTP {r.status_code} {r.text[:150]}")
            return 1
        d = r.json()
        u = d.get("usageMetadata") or {}
        std.append((n_subs(d["candidates"][0]["content"]["parts"][0]["text"]), u))
    print(f"BATCH רגיל: {int(time.time() - t0)}s, פריטים {[x[0] for x in std]}, "
          f"in={sum(x[1].get('promptTokenCount', 0) for x in std)} out={sum(x[1].get('candidatesTokenCount', 0) for x in std)} "
          f"think={sum(x[1].get('thoughtsTokenCount', 0) for x in std)}")

    # Batch (בקשות מוטמעות)
    body = {"batch": {"display_name": f"batch-test-{cid}", "input_config": {"requests": {"requests": [
        {"request": p, "metadata": {"key": f"w{i}"}} for i, p in enumerate(payloads)]}}}}
    t0 = time.time()
    r = requests.post(f"{BASE}/models/{model}:batchGenerateContent", headers=hdr, json=body, timeout=300)
    if r.status_code != 200:
        print(f"BATCH יצירה: HTTP {r.status_code} {r.text[:300]}")
        return 1
    name = r.json().get("name")
    print(f"BATCH נוצר: {name}")
    state = None
    while time.time() - t0 < float(os.environ.get("BATCH_WAIT_MIN") or 100) * 60:
        time.sleep(30)
        g = requests.get(f"{BASE}/{name}", headers=hdr, timeout=60)
        if g.status_code != 200:
            print(f"BATCH מצב: HTTP {g.status_code}")
            continue
        j = g.json()
        state = (j.get("metadata") or {}).get("state") or j.get("state")
        if j.get("done") or state in ("BATCH_STATE_SUCCEEDED", "BATCH_STATE_FAILED", "BATCH_STATE_CANCELLED",
                                      "BATCH_STATE_EXPIRED", "JOB_STATE_SUCCEEDED", "JOB_STATE_FAILED"):
            break
    print(f"BATCH מצב סופי: {state} אחרי {int((time.time() - t0) / 60)} דקות")
    if state not in ("BATCH_STATE_SUCCEEDED", "JOB_STATE_SUCCEEDED") and not j.get("done"):
        return 0
    resp = j.get("response") or (j.get("metadata") or {}).get("output") or {}
    items = (resp.get("inlinedResponses") or {}).get("inlinedResponses") or []
    res = []
    for it in items:
        d = it.get("response") or {}
        u = d.get("usageMetadata") or {}
        try:
            k = n_subs(d["candidates"][0]["content"]["parts"][0]["text"])
        except Exception:
            k = -1
        res.append((k, u))
    print(f"BATCH תוצאות: {len(res)}, פריטים {[x[0] for x in res]}, "
          f"in={sum(x[1].get('promptTokenCount', 0) for x in res)} out={sum(x[1].get('candidatesTokenCount', 0) for x in res)} "
          f"think={sum(x[1].get('thoughtsTokenCount', 0) for x in res)}; מפתחות התגובה: {sorted(resp.keys())}")


if __name__ == "__main__":
    sys.exit(main())

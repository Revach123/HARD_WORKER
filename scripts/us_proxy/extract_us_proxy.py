#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
extract_us_proxy.py

חילוץ בעלות מ-DEF 14A אמריקאי באמצעות Gemini, לחברות שהפרסר הדטרמיניסטי (ריפו Revach,
scripts/ownership_us/proxy_parse.py) לא הצליח לאמת, או שיש להן מבנה מורכב (Up-C / דו-מניתי).

קלט:  queue.json.gz  {cik: {"n": שם, "u": url, "t": טקסט סעיף הבעלות (כולל תוכן הטבלאות), "r": סיבה, "k": דירוג}}
      (נוצר ע"י Revach: data/ownership_us/proxy_sections.json.gz + מטא; ראה export_queue ב-Revach).
פלט:  results_us_proxy_<model>.jsonl    - שורה לכל חברה
      processed_us_proxy_<model>.json   - מעקב idempotent: cik -> {status, ts}

עקרון "בלי ניחושים": Gemini מחזיר ערכים כמחרוזות *כפי שכתובות* (shares_text / percent_text / evidence).
אחר כך כל ערך נבדק שהוא מופיע מילה-במילה בטקסט המקור (grounding). ערך שלא מופיע => מאופס ומסומן;
מחזיק שהציטוט שלו לא מופיע בטקסט => נפסל. אין חישוב/השלמה של שום מספר.

מכסה: משתמש ב-_key_pool/GeminiQuotaExceededError של extract_subsidiaries (אותם מפתחות, אותה עצירה
אוטומטית ב-RPD). עוצר בעדינות, שומר, וההרצה הבאה ממשיכה מאותה נקודה.

הרצה:  GEMINI_MODEL_OVERRIDE=gemini-3.5-flash-lite python extract_us_proxy.py --queue queue.json.gz [--limit N]
"""

import argparse
import glob
import gzip
import json
import os
import re
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

import requests

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "company_ownership"))
import extract_subsidiaries as ex  # noqa: E402  (_key_pool, GEMINI_BASE_URL, GEMINI_MODEL, GeminiQuotaExceededError)

CHECKPOINT_EVERY = 10
SCHEMA_VERSION = 2

SYSTEM_PROMPT = """\
You extract beneficial-ownership information from the text of a US proxy statement (Schedule 14A / DEF 14A).
The text may contain flattened tables (cells separated by " | ") and narrative paragraphs/footnotes.

STRICT RULES
1. Use ONLY information explicitly present in the provided text. Never compute, estimate, convert, round,
   sum, or infer any number. If a value is not written in the text, use null.
2. Copy every number EXACTLY as written (same digits, commas, decimals, % sign). Put it in the *_text fields
   as a string. Do not normalise.
3. Include every person/entity that appears in the beneficial ownership table(s), and every holder of more
   than 5% that is mentioned in narrative text or footnotes. One object per (holder, share class) pair.
4. For each holder return a verbatim evidence quote (max 160 characters, copied exactly from the text) that
   contains the figures you extracted for that holder.
5a. "classes": one object per share class mentioned in the text (and, in an Up-C structure, one for the holding-company
   units/interests, e.g. "membership interests in the operating LLC") with ONLY figures the text states for that class.
5. "structure": describe the ownership/control structure ONLY if the text states it (e.g. dual-class stock,
   Up-C / membership interests / Class B voting, controlled company, voting agreements). Quote exact figures
   as strings. If nothing is stated, use null.
6. Do not output anything except JSON.

OUTPUT JSON SCHEMA
{
  "as_of_date": "YYYY-MM-DD or null (date the ownership table is stated as of)",
  "shares_outstanding": [ {"class": "as written or null", "text": "exact figure as written"} ],
  "classes": [
    {
      "class": "share class as written (e.g. Class A common stock, Class B common stock)",
      "shares_outstanding_text": "exact figure as written or null",
      "votes_per_share_text": "exact text about votes per share as written (e.g. '1 vote per share', '3,126,844 votes') or null",
      "voting_power_percent_text": "exact % of total voting power attributable to this class, as written, or null",
      "economic_interest_percent_text": "exact % of economic interest attributable to this class, as written, or null",
      "evidence": "verbatim quote (max 200 chars) supporting the figures above"
    }
  ],
  "holders": [
    {
      "name": "name as written, without footnote markers or street address",
      "role": "director_or_officer | five_percent_holder | group_total | other",
      "class": "share class as written, or null",
      "shares_text": "exact string or null",
      "percent_text": "exact string or null (percent of that class)",
      "voting_percent_text": "exact string or null",
      "economic_percent_text": "exact string or null",
      "evidence": "verbatim quote from the text"
    }
  ],
  "structure": null | {
    "type": "dual_class | up_c | controlled_company | other",
    "summary": "max 400 chars, only what the text states",
    "key_figures": [ {"label": "as written", "text": "exact figure as written"} ]
  },
  "no_data_reason": "string if the text has no beneficial ownership information, else null"
}
"""


# ---------------------------------------------------------------- grounding

_QUOTES = str.maketrans({"‘": "'", "’": "'", "“": '"', "”": '"', "–": "-", "—": "-",
                         " ": " ", "​": ""})


def norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").translate(_QUOTES)).strip().lower()


def grounded(value, source_norm: str) -> bool:
    return value is None or (isinstance(value, str) and norm(value) in source_norm)


_FOOT_TAIL = re.compile(r"\s*\(\s*\d{1,2}\s*\)\s*$")


def clean_figure(v):
    """מסיר הפניית שוליים צמודה ("30.2(2)" -> "30.2"); ערך שהוא רק הפניה ("(3)") או בלי ספרות/כוכבית => None."""
    if not isinstance(v, str):
        return None
    t = _FOOT_TAIL.sub("", v).strip()
    if t == "*" or re.search(r"\d", t):
        return t
    return None


def ground_result(res: dict, source: str) -> dict:
    """מאפס ערכים שלא מופיעים מילה-במילה במקור; פוסל מחזיקים בלי ציטוט תקף. מחזיר dict מנוקה + סטטיסטיקה."""
    src = norm(source)
    out_holders, dropped, nulled = [], 0, 0
    for h in (res.get("holders") or []):
        if not isinstance(h, dict) or not h.get("name"):
            dropped += 1
            continue
        ev = h.get("evidence")
        if not (isinstance(ev, str) and norm(ev) and norm(ev) in src):
            dropped += 1  # בלי ציטוט מאומת - לא סומכים על השורה
            continue
        if norm(h["name"]) not in src:
            # שם עם סימוני שוליים/כתובת הוסרו - נבדוק לפחות את המילה הראשונה והאחרונה
            toks = norm(h["name"]).split()
            if not toks or toks[0] not in src or toks[-1] not in src:
                dropped += 1
                continue
        clean = {k: h.get(k) for k in ("name", "role", "class", "evidence")}
        for k in ("shares_text", "percent_text", "voting_percent_text", "economic_percent_text"):
            v = h.get(k)
            if v is not None and not grounded(v, src):
                nulled += 1
                v = None
            if k != "shares_text" and v is not None:
                v = clean_figure(v)  # "(3)" = הפניית שוליים, לא אחוז
            clean[k] = v
        out_holders.append(clean)
    so = []
    for x in (res.get("shares_outstanding") or []):
        if isinstance(x, dict) and grounded(x.get("text"), src):
            so.append({"class": x.get("class"), "text": x.get("text")})
    st = res.get("structure")
    if isinstance(st, dict):
        kf = [k for k in (st.get("key_figures") or []) if isinstance(k, dict) and grounded(k.get("text"), src)]
        st = {"type": st.get("type"), "summary": (st.get("summary") or "")[:400], "key_figures": kf}
    else:
        st = None
    cls = []
    for c in (res.get("classes") or []):
        if not isinstance(c, dict) or not c.get("class"):
            continue
        ev = c.get("evidence")
        if not (isinstance(ev, str) and norm(ev) and norm(ev) in src):
            continue  # בלי ציטוט מאומת - לא סומכים
        row = {"class": c.get("class"), "evidence": ev}
        for k in ("shares_outstanding_text", "votes_per_share_text", "voting_power_percent_text",
                  "economic_interest_percent_text"):
            v = c.get(k)
            row[k] = v if (v is not None and grounded(v, src)) else None
        cls.append(row)
    asof = res.get("as_of_date")
    if not (isinstance(asof, str) and re.match(r"^\d{4}-\d{2}-\d{2}$", asof)):
        asof = None
    return {"as_of_date": asof, "shares_outstanding": so, "classes": cls, "holders": out_holders, "structure": st,
            "no_data_reason": res.get("no_data_reason"),
            "grounding": {"holders_in": len(res.get("holders") or []), "holders_kept": len(out_holders),
                          "holders_dropped": dropped, "fields_nulled": nulled}}


# ---------------------------------------------------------------- Gemini

def call_gemini_text(text: str, name: str, max_retries: int = 5) -> dict:
    payload = {
        "system_instruction": {"parts": [{"text": SYSTEM_PROMPT}]},
        "contents": [{"role": "user", "parts": [{"text":
            f"Company: {name}\n\nPROXY STATEMENT TEXT (ownership section):\n\n{text}"}]}],
        "generationConfig": {"responseMimeType": "application/json", "temperature": 0,
                             "thinkingConfig": {"thinkingLevel": "high"}},
    }
    pool = ex._key_pool
    last_error = None
    for attempt in range(max_retries):
        if pool.all_daily_exhausted():
            raise ex.GeminiQuotaExceededError("כל המפתחות הגיעו למכסה יומית (RPD)")
        key = pool.get_next_key()
        if key is None:
            wait = pool.seconds_until_next_available()
            if wait == float("inf"):
                raise ex.GeminiQuotaExceededError("כל המפתחות הגיעו למכסה יומית (RPD)")
            if wait > 0:
                time.sleep(wait)
            key = pool.get_next_key()
            if key is None:
                raise ex.GeminiQuotaExceededError("כל המפתחות עדיין בקירור")
        pool.limiter_for(key).acquire()
        url = f"{ex.GEMINI_BASE_URL}/{ex.GEMINI_MODEL}:generateContent?key={key}"
        try:
            resp = requests.post(url, json=payload, timeout=180)
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
            last_error = e
            time.sleep(5 * (2 ** attempt))
            continue
        if resp.status_code == 429 and "exceeded your current quota" in resp.text:
            is_daily = False
            try:
                for d in resp.json().get("error", {}).get("details", []):
                    if d.get("@type", "").endswith("QuotaFailure"):
                        is_daily = is_daily or any("PerDay" in v.get("quotaId", "") for v in d.get("violations", []))
            except Exception:  # noqa: BLE001
                pass
            pool.mark_exhausted(key, daily=is_daily)
            last_error = requests.exceptions.HTTPError(f"429: {resp.text[:200]}")
            continue
        if resp.status_code in (429, 503):
            last_error = requests.exceptions.HTTPError(f"{resp.status_code}: {resp.text[:200]}")
            time.sleep(5 * (2 ** attempt))
            continue
        resp.raise_for_status()
        data = resp.json()
        try:
            txt = data["candidates"][0]["content"]["parts"][0]["text"]
        except (KeyError, IndexError) as e:
            raise RuntimeError(f"תגובת Gemini לא בפורמט צפוי: {json.dumps(data)[:300]}") from e
        txt = re.sub(r"^```json\s*|\s*```$", "", txt.strip())
        return json.loads(txt)
    if last_error is not None and "exceeded your current quota" in str(last_error):
        raise ex.GeminiQuotaExceededError(str(last_error))
    raise last_error or RuntimeError("Gemini failed")


# ---------------------------------------------------------------- persistence / git

def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def commit_progress(paths, reason):
    """commit+push עם rebase retry (מה-thread הראשי בלבד). לא נכשל את הריצה אם push נדחה."""
    try:
        subprocess.run(["git", "config", "user.name", "github-actions[bot]"], check=True)
        subprocess.run(["git", "config", "user.email", "github-actions[bot]@users.noreply.github.com"], check=True)
        subprocess.run(["git", "add", *paths], check=True)
        if subprocess.run(["git", "diff", "--staged", "--quiet"]).returncode == 0:
            return True
        subprocess.run(["git", "commit", "-m", f"us-proxy progress {reason} [skip ci]"], check=True)
        for attempt in range(5):
            if subprocess.run(["git", "push"]).returncode == 0:
                return True
            subprocess.run(["git", "rebase", "--abort"], stderr=subprocess.DEVNULL)
            subprocess.run(["git", "fetch", "origin", "main"], check=True)
            if subprocess.run(["git", "rebase", "origin/main"]).returncode != 0:
                subprocess.run(["git", "rebase", "--abort"], stderr=subprocess.DEVNULL)
            time.sleep(3 * (attempt + 1))
        print("    checkpoint push נכשל - ההתקדמות על הדיסק המקומי בלבד")
        return False
    except subprocess.CalledProcessError as e:
        print(f"    שגיאת git: {e}")
        return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--queue", default=os.path.join(HERE, "queue.json.gz"))
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--workers", type=int, default=2)
    ap.add_argument("--no-git", action="store_true", help="בלי commit (בדיקות מקומיות)")
    args = ap.parse_args()

    tag = ex.GEMINI_MODEL.replace(".", "_")
    results_path = os.path.join(HERE, f"results_us_proxy_{tag}.jsonl")
    processed_path = os.path.join(HERE, f"processed_us_proxy_{tag}.json")

    with gzip.open(args.queue, "rt", encoding="utf-8") as f:
        queue = json.load(f)
    processed = load_json(processed_path, {})
    order = sorted(queue, key=lambda c: queue[c].get("k", 10**9))  # הגדולות (דירוג נמוך) קודם
    # חברות שכבר עובדו (באותה סכמה) ע"י מודל אחר לא נשלחות שוב - לא מבזבזים מכסה
    done_elsewhere = set()
    for pf in glob.glob(os.path.join(HERE, "processed_us_proxy_*.json")):
        if os.path.abspath(pf) == os.path.abspath(processed_path):
            continue
        for c, v in load_json(pf, {}).items():
            if v.get("status") in ("ok", "empty") and v.get("schema") == SCHEMA_VERSION:
                done_elsewhere.add(c)
    todo = [c for c in order if c not in done_elsewhere
            and (processed.get(c, {}).get("status") not in ("ok", "empty")
                 or processed[c].get("schema") != SCHEMA_VERSION)]
    if args.limit:
        todo = todo[:args.limit]
    print(f"model={ex.GEMINI_MODEL} queue={len(queue)} todo={len(todo)}")

    lock = threading.Lock()
    stop = threading.Event()
    done = ok = empty = err = 0

    def work(cik):
        if stop.is_set():
            return cik, None, "skipped"
        item = queue[cik]
        try:
            raw = call_gemini_text(item["t"], item.get("n") or cik)
        except ex.GeminiQuotaExceededError as e:
            stop.set()
            return cik, str(e), "quota"
        except Exception as e:  # noqa: BLE001
            return cik, f"{type(e).__name__}: {str(e)[:200]}", "error"
        res = ground_result(raw, item["t"])
        return cik, res, "ok" if res["holders"] else "empty"

    with ThreadPoolExecutor(args.workers) as pool:
        futs = [pool.submit(work, c) for c in todo]
        for fut in as_completed(futs):
            cik, res, status = fut.result()
            if status == "skipped":
                continue
            if status == "quota":
                print(f"  מכסה נגמרה ({res}) - עוצר בעדינות")
                continue
            with lock:
                now = datetime.now(timezone.utc).isoformat(timespec="seconds")
                if status in ("ok", "empty"):
                    rec = {"cik": cik, "model": ex.GEMINI_MODEL, "ts": now, "schema": SCHEMA_VERSION,
                           "url": queue[cik].get("u"), "reason": queue[cik].get("r"), **res}
                    with open(results_path, "a", encoding="utf-8") as f:
                        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
                    processed[cik] = {"status": status, "ts": now, "schema": SCHEMA_VERSION}
                    ok += status == "ok"
                    empty += status == "empty"
                else:
                    processed[cik] = {"status": "error", "ts": now, "schema": SCHEMA_VERSION, "err": res}
                    err += 1
                done += 1
                tmp = processed_path + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(processed, f, ensure_ascii=False, indent=1, sort_keys=True)
                os.replace(tmp, processed_path)
                if done % CHECKPOINT_EVERY == 0:
                    print(f"  {done}/{len(todo)} ok={ok} empty={empty} err={err}")
                    if not args.no_git:
                        commit_progress([results_path, processed_path], f"{done}/{len(todo)}")
    if not args.no_git:
        commit_progress([results_path, processed_path], "final")
    print(f"סיום: done={done} ok={ok} empty={empty} err={err} (מתוך {len(todo)})")


if __name__ == "__main__":
    main()

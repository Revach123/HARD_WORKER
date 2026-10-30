#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
extract_eu_reports.py

חילוץ בעלות מדוחות שנתיים אירופאיים (ESEF / UKSEF) באמצעות Gemini - המקבילה של scripts/us_proxy.

קלט:  queue.json.gz  {lei: {"n": שם, "u": url, "t": טקסט סעיפי הבעלות, "r": סיבה, "k": דירוג, "cc", "isin"}}
      (נוצר ע"י Revach: scripts/ownership_eu/fetch_reports.py -> data/ownership_eu/llm_queue.json.gz).
פלט:  results_eu_reports_<model>.jsonl    - שורה לכל חברה
      processed_eu_reports_<model>.json   - מעקב idempotent: lei -> {status, ts}

אותו עקרון "בלי ניחושים" ואותה בדיקת grounding כמו ב-us_proxy: כל ערך חייב להופיע מילה-במילה בטקסט
המקור; מחזיק שהציטוט שלו לא מופיע - נפסל. אותם מפתחות ואותה עצירה אוטומטית במכסה (extract_subsidiaries).

הרצה:  GEMINI_MODEL_OVERRIDE=gemini-3.5-flash-lite python extract_eu_reports.py --queue queue.json.gz [--limit N]
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
sys.path.insert(0, os.path.join(HERE, "..", "us_proxy"))
import extract_subsidiaries as ex  # noqa: E402  (_key_pool, GEMINI_BASE_URL, GEMINI_MODEL, GeminiQuotaExceededError)
from extract_us_proxy import clean_figure, load_json  # noqa: E402
from extract_us_proxy import norm as _norm  # noqa: E402

CHECKPOINT_EVERY = 10
SCHEMA_VERSION = 4  # 2: grounding סובלני לתבליטים + rejected; 3: control; 4: ציטוט טבלה (תאים בשורות נפרדות)
CELL_SPAN = 400     # ציטוט של שורת טבלה: כל התאים, לפי הסדר, בטווח הזה בטקסט
_BULLETS = re.compile(r"[\u2022\u00b7\u25aa\u25cf\u25e6\u25a0\u2023\u2043]")


def norm(s: str) -> str:
    """כמו ב-us_proxy, ובנוסף בלי תבליטים: דוחות מפרטים מחזיקים כרשימה ("• BlackRock 11.07%;") וציטוט
    שמדלג על התבליט (או חוצה שתי שורות רשימה) הוא עדיין ציטוט מדויק של הטקסט. "|" (מפריד תאים) = רווח:
    בטבלה מ-PDF כל תא בשורה משלו, והמודל מצטט "Name | 226,433,021 | 17.98%"."""
    return _norm(_BULLETS.sub(" ", (s or "").replace("|", " ")))


def evidence_ok(ev, source_norm: str) -> bool:
    """ציטוט מדויק; או ציטוט של שורת טבלה (תאים מופרדים ב-"|" או בשורה חדשה) שבו כל תא מופיע מילה-במילה,
    לפי הסדר, בטווח CELL_SPAN - כשהמודל דילג על עמודה ("BlackRock, | 231,975,400(2) | 5.69%")."""
    if not isinstance(ev, str) or not norm(ev):
        return False
    if norm(ev) in source_norm:
        return True
    cells = [norm(c) for c in re.split(r"[|\n]", ev) if norm(c)]
    if len(cells) < 2:
        return False
    start = 0
    while True:
        i = source_norm.find(cells[0], start)
        if i < 0:
            return False
        pos, ok = i + len(cells[0]), True
        for c in cells[1:]:
            j = source_norm.find(c, pos)
            if j < 0 or j - i > CELL_SPAN:
                ok = False
                break
            pos = j + len(c)
        if ok:
            return True
        start = i + 1


def grounded(value, source_norm: str) -> bool:
    return value is None or (isinstance(value, str) and norm(value) in source_norm)

SYSTEM_PROMPT = """\
You extract shareholder information from excerpts of a European listed company's annual report
(e.g. "Substantial shareholdings" / "Major shareholders" notified under the Disclosure Guidance and Transparency
Rules (DTR 5) or the EU Transparency Directive, "Directors' interests", "Share capital").
The text may be in any European language. It may contain flattened tables (cells separated by " | ", or each cell on its own line) and
excerpts separated by "[...]". When quoting a table row whose cells are on separate lines, join the cells
with " | " in their original order.

STRICT RULES
1. Use ONLY information explicitly present in the provided text. Never compute, estimate, convert, round,
   sum, or infer any number. If a value is not written in the text, use null.
2. Copy every number EXACTLY as written (same digits, separators, decimals, % sign) into the *_text fields.
3. "holders": every shareholder listed as a substantial / major / significant shareholder (notified holdings),
   and every director or officer whose shareholding is listed. One object per (holder, share class).
   If a table has several date columns, use ONLY the most recent one and say which in "as_of_text".
   Do not list shareholders that appear only as examples, in share-plan descriptions, or in subsidiaries' tables.
4. For each holder return a verbatim evidence quote (max 160 characters, copied exactly from the text) that
   contains the figures you extracted for that holder.
5. "shares_outstanding": the total issued shares / total voting rights figure(s) as written, per class.
6. "classes": one object per share class mentioned, with only figures the text states for that class.
7. "structure": describe the ownership/control structure ONLY if the text states it (e.g. controlling
   shareholder, relationship agreement, dual-class / multiple voting rights, shareholder agreement).
8. "control": who controls the company IN PRACTICE, ONLY if the text says so explicitly: a controlling shareholder
   (e.g. "controlling shareholder", "relationship agreement", holds a majority of voting rights), a parent company
   that consolidates it, special rights to appoint directors, or a shareholder agreement that confers control.
   Name the controller exactly as written and give a verbatim evidence quote. If the text states that no person
   controls the company, set "controller" to null and "mechanism" to "none_stated". If nothing is said, use null.
9. Output JSON only.

OUTPUT JSON SCHEMA
{
  "as_of_date": "YYYY-MM-DD or null (date the substantial-shareholdings list is stated as of)",
  "shares_outstanding": [ {"class": "as written or null", "text": "exact figure as written"} ],
  "classes": [
    {
      "class": "share class as written",
      "shares_outstanding_text": "exact figure or null",
      "votes_per_share_text": "exact text about votes per share or null",
      "voting_power_percent_text": "exact % of total voting rights attributable to this class or null",
      "evidence": "verbatim quote (max 200 chars)"
    }
  ],
  "holders": [
    {
      "name": "name as written, without footnote markers",
      "role": "substantial_holder | director_or_officer | group_total | other",
      "class": "share class as written, or null",
      "shares_text": "exact string or null",
      "percent_text": "exact % of issued share capital / voting rights as written, or null",
      "voting_percent_text": "exact string or null (only if stated separately from percent_text)",
      "as_of_text": "date or column heading the figures refer to, as written, or null",
      "evidence": "verbatim quote from the text"
    }
  ],
  "structure": null | {
    "type": "controlling_shareholder | dual_class | shareholder_agreement | other",
    "summary": "max 400 chars, only what the text states",
    "key_figures": [ {"label": "as written", "text": "exact figure as written"} ]
  },
  "control": null | {
    "controller": "name as written, or null",
    "mechanism": "majority_voting | relationship_agreement | parent_company | special_rights | shareholder_agreement | none_stated | other",
    "percent_text": "exact % of voting rights / capital held by the controller as written, or null",
    "evidence": "verbatim quote (max 240 chars) stating the control"
  },
  "no_data_reason": "string if the text has no shareholder information, else null"
}
"""


def ground_result(res: dict, source: str) -> dict:
    """מאפס ערכים שלא מופיעים מילה-במילה במקור; פוסל מחזיקים בלי ציטוט תקף (כמו us_proxy)."""
    src = norm(source)
    out_holders, dropped, nulled, rejected = [], 0, 0, []
    for h in (res.get("holders") or []):
        if not isinstance(h, dict) or not h.get("name"):
            dropped += 1
            continue
        ev = h.get("evidence")
        if not evidence_ok(ev, src):
            dropped += 1
            rejected.append({"name": h.get("name"), "evidence": (ev or "")[:200], "why": "evidence"})
            continue
        if norm(h["name"]) not in src:
            toks = norm(h["name"]).split()
            if not toks or toks[0] not in src or toks[-1] not in src:
                dropped += 1
                rejected.append({"name": h.get("name"), "evidence": (ev or "")[:200], "why": "name"})
                continue
        clean = {k: h.get(k) for k in ("name", "role", "class", "evidence")}
        for k in ("shares_text", "percent_text", "voting_percent_text", "as_of_text"):
            v = h.get(k)
            if v is not None and not grounded(v, src):
                nulled += 1
                v = None
            if k in ("percent_text", "voting_percent_text") and v is not None:
                v = clean_figure(v)
            clean[k] = v
        out_holders.append(clean)
    so = [{"class": x.get("class"), "text": x.get("text")} for x in (res.get("shares_outstanding") or [])
          if isinstance(x, dict) and x.get("text") and grounded(x.get("text"), src)]
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
        if not evidence_ok(ev, src):
            continue
        row = {"class": c.get("class"), "evidence": ev}
        for k in ("shares_outstanding_text", "votes_per_share_text", "voting_power_percent_text"):
            v = c.get(k)
            row[k] = v if (v is not None and grounded(v, src)) else None
        cls.append(row)
    ctl = res.get("control")
    if isinstance(ctl, dict):
        ev = ctl.get("evidence")
        name = ctl.get("controller")
        if not evidence_ok(ev, src):
            ctl = None  # בלי ציטוט מאומת - לא סומכים על קביעת שליטה
        else:
            if name is not None and not (isinstance(name, str) and (norm(name) in src or all(
                    t in src for t in norm(name).split()[:1] + norm(name).split()[-1:]))):
                name = None
            pt = ctl.get("percent_text")
            pt = clean_figure(pt) if (pt is not None and grounded(pt, src)) else None
            ctl = {"controller": name, "mechanism": ctl.get("mechanism"), "percent_text": pt, "evidence": ev[:300]}
    else:
        ctl = None
    asof = res.get("as_of_date")
    if not (isinstance(asof, str) and re.match(r"^\d{4}-\d{2}-\d{2}$", asof)):
        asof = None
    return {"as_of_date": asof, "shares_outstanding": so, "classes": cls, "holders": out_holders, "structure": st,
            "control": ctl,
            "no_data_reason": res.get("no_data_reason"),
            "grounding": {"holders_in": len(res.get("holders") or []), "holders_kept": len(out_holders),
                          "holders_dropped": dropped, "fields_nulled": nulled},
            "rejected": rejected[:20]}  # לבקרה בלבד - לא מוצג באתר


def call_gemini_text(text: str, name: str, max_retries: int = 5) -> dict:
    payload = {
        "system_instruction": {"parts": [{"text": SYSTEM_PROMPT}]},
        "contents": [{"role": "user", "parts": [{"text":
            f"Company: {name}\n\nANNUAL REPORT EXCERPTS (shareholder sections):\n\n{text}"}]}],
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


def commit_progress(paths, reason):
    """commit+push עם rebase retry (מה-thread הראשי בלבד). לא נכשל את הריצה אם push נדחה."""
    branch = os.environ.get("GITHUB_REF_NAME") or "main"
    try:
        subprocess.run(["git", "config", "user.name", "github-actions[bot]"], check=True)
        subprocess.run(["git", "config", "user.email", "github-actions[bot]@users.noreply.github.com"], check=True)
        subprocess.run(["git", "add", *paths], check=True)
        if subprocess.run(["git", "diff", "--staged", "--quiet"]).returncode == 0:
            return True
        subprocess.run(["git", "commit", "-m", f"eu-reports progress {reason} [skip ci]"], check=True)
        for attempt in range(5):
            if subprocess.run(["git", "push", "origin", f"HEAD:{branch}"]).returncode == 0:
                return True
            subprocess.run(["git", "rebase", "--abort"], stderr=subprocess.DEVNULL)
            subprocess.run(["git", "fetch", "origin", branch], check=True)
            if subprocess.run(["git", "rebase", f"origin/{branch}"]).returncode != 0:
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
    ap.add_argument("--leis", default="", help="רק LEI אלה (מופרד בפסיקים)")
    ap.add_argument("--no-git", action="store_true", help="בלי commit (בדיקות מקומיות)")
    args = ap.parse_args()

    tag = ex.GEMINI_MODEL.replace(".", "_")
    results_path = os.path.join(HERE, f"results_eu_reports_{tag}.jsonl")
    processed_path = os.path.join(HERE, f"processed_eu_reports_{tag}.json")

    with gzip.open(args.queue, "rt", encoding="utf-8") as f:
        queue = json.load(f)
    processed = load_json(processed_path, {})
    order = sorted(queue, key=lambda c: queue[c].get("k", 10**9))
    # דוח שכבר עובד (אותו url, אותה סכמה) ע"י מודל כלשהו לא נשלח שוב - חוסך מכסה
    # סכמה 3: בדיקת הציטוט פסלה שורות טבלה תקינות (תא בכל שורה). מעבדים מחדש רק דוחות שבהם נפסל מחזיק
    # כלשהו; השאר תקפים גם בסכמה 4 - לא מבזבזים מכסה
    dropped_v3 = set()
    for rf in glob.glob(os.path.join(HERE, "results_eu_reports_*.jsonl")):
        if rf.endswith("_parser.jsonl"):
            continue
        for line in open(rf, encoding="utf-8"):
            if line.strip():
                r = json.loads(line)
                if r.get("schema", 0) < 4 and (r.get("grounding") or {}).get("holders_dropped"):
                    dropped_v3.add((r["lei"], r.get("url")))

    def current(c, v):
        sch = v.get("schema") or 0
        return (v.get("status") in ("ok", "empty") and v.get("u") == queue.get(c, {}).get("u")
                and (sch == SCHEMA_VERSION or (sch == 3 and (c, v.get("u")) not in dropped_v3)))

    done_elsewhere = set()
    for pf in glob.glob(os.path.join(HERE, "processed_eu_reports_*.json")):
        if os.path.abspath(pf) == os.path.abspath(processed_path):
            continue
        for c, v in load_json(pf, {}).items():
            if current(c, v):
                done_elsewhere.add(c)

    def need(c):
        return not current(c, processed.get(c, {}))  # כולל דוח שנתי חדש (url אחר) => מעבדים מחדש

    todo = [c for c in order if c not in done_elsewhere and need(c)]
    if args.leis:
        want = {x.strip() for x in args.leis.split(",") if x.strip()}
        todo = [c for c in order if c in want]
    if args.limit:
        todo = todo[:args.limit]
    print(f"model={ex.GEMINI_MODEL} queue={len(queue)} todo={len(todo)}")

    lock = threading.Lock()
    stop = threading.Event()
    done = ok = empty = err = 0

    def work(lei):
        if stop.is_set():
            return lei, None, "skipped"
        item = queue[lei]
        try:
            raw = call_gemini_text(item["t"], item.get("n") or lei)
        except ex.GeminiQuotaExceededError as e:
            stop.set()
            return lei, str(e), "quota"
        except Exception as e:  # noqa: BLE001
            return lei, f"{type(e).__name__}: {str(e)[:200]}", "error"
        res = ground_result(raw, item["t"])
        return lei, res, "ok" if res["holders"] else "empty"

    with ThreadPoolExecutor(args.workers) as pool:
        futs = [pool.submit(work, c) for c in todo]
        for fut in as_completed(futs):
            lei, res, status = fut.result()
            if status == "skipped":
                continue
            if status == "quota":
                print(f"  מכסה נגמרה ({res}) - עוצר בעדינות")
                continue
            with lock:
                now = datetime.now(timezone.utc).isoformat(timespec="seconds")
                item = queue[lei]
                if status in ("ok", "empty"):
                    rec = {"lei": lei, "model": ex.GEMINI_MODEL, "ts": now, "schema": SCHEMA_VERSION,
                           "url": item.get("u"), "reason": item.get("r"), "n": item.get("n"), "cc": item.get("cc"),
                           "isin": item.get("isin"), **res}
                    with open(results_path, "a", encoding="utf-8") as f:
                        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
                    processed[lei] = {"status": status, "ts": now, "schema": SCHEMA_VERSION, "u": item.get("u")}
                    ok += status == "ok"
                    empty += status == "empty"
                else:
                    processed[lei] = {"status": "error", "ts": now, "schema": SCHEMA_VERSION, "u": item.get("u"),
                                      "err": res}
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

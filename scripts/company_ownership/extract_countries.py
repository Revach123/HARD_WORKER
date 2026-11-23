# -*- coding: utf-8 -*-
"""
extract_countries.py - מדינת ההתאגדות של חברות בת שלא נמצאו ברשם הישראלי (חילוץ ממוקד, Gemini).

הקלט: country_targets.json (נבנה מ-Revach: data/holdings/extracted_subsidiaries.json) - חברות אם עם ח.פ ישראלי,
ולכל אחת: הדוח שממנו חולצו חברות הבת, קבצי ה-PDF, העמודים שבהם הופיעו (page_reference מהחילוץ המקורי) ושמות
חברות הבת שלא זוהו.

לא חילוץ מלא מחדש: לכל דוח שולחים ל-Gemini רק את העמודים הרלוונטיים - העמודים מהחילוץ המקורי (±1) ועמודים
שבטקסט שלהם מופיע שם של אחת מחברות הבת - ומבקשים את המדינה של כל חברה ברשימה, רק כפי שמופיעה במסמך.
הפלט בקובץ נפרד (subsidiary_countries.jsonl, שורה לכל חברת אם) - private_subsidiaries.jsonl לא משתנה.

נעצר בעדינות כשהמכסה נגמרת; ריצה הבאה ממשיכה מהחברות שעוד לא טופלו.
הרצה:  python extract_countries.py [--limit N] [--hp 520000000,...]
"""
import argparse
import io
import json
import os
import re
import sys
import time
from datetime import datetime, timezone

from pypdf import PdfReader, PdfWriter

import extract_subsidiaries as ex
import maya_reports_client as mrc
import run_small_batch as rsb

TARGETS = "country_targets.json"
OUT = "subsidiary_countries.jsonl"
MAX_PAGES_PER_CALL = 30
PAGE_PAD = 1

COUNTRY_PROMPT = """\
אתה מנוע חילוץ מידע מדוחות של חברות ישראליות. מצורפים עמודים מתוך דוח (לרוב טבלת "חברות מוחזקות" / תקנה 11,
"List of Subsidiaries", או באור על חברות מוחזקות).

המשימה: לכל אחת מהחברות ברשימה שתינתן, מצא במסמך את מדינת ההתאגדות / הרישום שלה.

כללים מחייבים:
1. החזר אך ורק JSON תקני, בלי טקסט נוסף ובלי Markdown.
2. רק מדינה שמופיעה במסמך במפורש לגבי החברה הזו: עמודה כמו "מקום התאגדות", "מדינת התאגדות", "ארץ התאגדות",
   "Jurisdiction", "Country of incorporation", "Place of incorporation", או משפט מפורש בטקסט ("חברה שהתאגדה
   בהולנד", "a Delaware corporation"). מדינה בארה"ב (Delaware, New York) - החזר "ארה\"ב" ו-country_code "US",
   והמדינה בתוך ארה"ב ב-state.
3. אסור להסיק מדינה מהשם, מהמטבע, מצורת ההתאגדות (Ltd, GmbH, B.V.) או ממיקום הנכסים/הפעילות. אם המדינה לא
   כתובה במפורש לגבי החברה - country ו-country_code הם null.
4. name - השם בדיוק כפי שהופיע ברשימה שקיבלת (לא כפי שבמסמך), כדי שאפשר יהיה לשייך.
5. evidence - ציטוט קצר מהמסמך (עד 15 מילים) שממנו נלקחה המדינה; page - מספר העמוד בקובץ המצורף.

סכימת פלט:
{
  "subsidiaries": [
    {"name": "...", "country": "המדינה כפי שבמסמך" | null, "country_code": "ISO 3166-1 alpha-2" | null,
     "state": "מדינה/מחוז בתוך המדינה, אם צוין" | null, "evidence": "..." | null, "page": number | null}
  ]
}
"""


def norm(s):
    return re.sub(r"\s+", " ", re.sub(r"[^\w֐-׿]+", " ", str(s or "").lower())).strip()


def find_pages(reader, names, hint_pages, is_main):
    """עמודים לשליחה: העמודים מהחילוץ המקורי (±PAGE_PAD, רק בקובץ הראשי) ועמודים שבהם מופיע שם חברת בת."""
    n = len(reader.pages)
    pages = set()
    if is_main:
        for p in hint_pages:
            for q in range(p - 1 - PAGE_PAD, p + PAGE_PAD):
                if 0 <= q < n:
                    pages.add(q)
    keys = [k for k in (norm(x) for x in names) if len(k) >= 5]
    if keys:
        for i in range(n):
            try:
                t = norm(reader.pages[i].extract_text() or "")
            except Exception:
                continue
            if any(k in t or k[::-1] in t for k in keys):     # עברית לפעמים מחולצת הפוכה
                pages.add(i)
    return sorted(pages)


def crop(reader, pages):
    w = PdfWriter()
    for i in pages:
        w.add_page(reader.pages[i])
    buf = io.BytesIO()
    w.write(buf)
    return buf.getvalue()


def ask(pdf_bytes, names, hint):
    ex.EXTRACTION_SYSTEM_PROMPT = COUNTRY_PROMPT    # call_gemini_extraction קורא את ההנחיה בזמן הקריאה
    listing = "\n".join(f"- {n}" for n in names)
    old = ex.call_gemini_extraction

    def call(pdf, filename_hint, max_retries=2):
        # אותה קריאה (מאגר מפתחות, RPM, זיהוי מכסה יומית) - עם רשימת החברות בהודעת המשתמש
        payload_hint = f"{filename_hint}\n\nהחברות לבדיקה (החזר שורה לכל אחת):\n{listing}"
        return old(pdf, filename_hint=payload_hint, max_retries=max_retries)
    ex.call_gemini_extraction = call
    try:
        return rsb._call_gemini_with_quota_retry(pdf_bytes, hint)
    finally:
        ex.call_gemini_extraction = old


def done_hps():
    s = set()
    if os.path.exists(OUT):
        for line in open(OUT, encoding="utf-8"):
            try:
                s.add(json.loads(line)["hp"])
            except (ValueError, KeyError):
                pass
    return s


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--hp", default="", help="רק חברות האם האלה (ח.פ, מופרדים בפסיק)")
    a = ap.parse_args()
    targets = json.load(open(TARGETS, encoding="utf-8"))["targets"]
    if a.hp:
        targets = [t for t in targets if t["hp"] in set(a.hp.split(","))]
    done = done_hps()
    todo = [t for t in targets if t["hp"] not in done]
    # קודם חברות עם יותר חברות בת לא מזוהות - שם התועלת הגדולה
    todo.sort(key=lambda t: -len(t["names"]))
    if a.limit:
        todo = todo[:a.limit]
    print(f"חברות אם: {len(targets)}, כבר טופלו: {len(done & {t['hp'] for t in targets})}, בתור: {len(todo)} "
          f"(מודל {ex.GEMINI_MODEL})")
    n_ok = n_country = 0
    for k, t in enumerate(todo, 1):
        items, pages_sent, errors = {}, 0, []
        for fi, url in enumerate(t["pdfs"]):
            try:
                pdf = mrc.download_report_file(url)
                reader = PdfReader(io.BytesIO(pdf))
            except Exception as e:
                errors.append(f"{url.split('/')[-1]}: {type(e).__name__}")
                continue
            pages = find_pages(reader, t["names"], t["pages"], fi == 0)
            for s in range(0, len(pages), MAX_PAGES_PER_CALL):
                part = pages[s:s + MAX_PAGES_PER_CALL]
                try:
                    res = ask(crop(reader, part), t["names"], f"{url}#p{part[0] + 1}-{part[-1] + 1}")
                except ex.GeminiQuotaExceededError:
                    print(f"מכסה נגמרה אחרי {k - 1} חברות - ממשיכים בריצה הבאה.")
                    print(f"סיכום: {n_ok} חברות אם, {n_country} חברות בת עם מדינה")
                    return 0
                pages_sent += len(part)
                for x in (res or {}).get("subsidiaries") or []:
                    nm = x.get("name")
                    if nm in t["names"] and (x.get("country") or nm not in items):
                        if x.get("page") and isinstance(x["page"], int) and 0 < x["page"] <= len(part):
                            x["page"] = part[x["page"] - 1] + 1      # עמוד בקובץ המקורי
                        x["file"] = fi
                        items[nm] = {kk: v for kk, v in x.items() if v not in (None, "")}
                time.sleep(1)
        rec = {"hp": t["hp"], "cid": t["cid"], "report_id": t["report_id"], "model": ex.GEMINI_MODEL,
               "at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "pages_sent": pages_sent,
               "items": list(items.values())}
        if errors:
            rec["errors"] = errors
        if not pages_sent and errors:
            print(f"[{k}/{len(todo)}] {t['name']}: הורדה נכשלה - ינוסה שוב בריצה הבאה")
            continue
        with open(OUT, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        c = sum(1 for x in items.values() if x.get("country"))
        n_ok += 1
        n_country += c
        print(f"[{k}/{len(todo)}] {t['name']} ({t['hp']}): {pages_sent} עמ', מדינה ל-{c}/{len(t['names'])}")
    print(f"סיכום: {n_ok} חברות אם, {n_country} חברות בת עם מדינה")
    return 0


if __name__ == "__main__":
    sys.exit(main())

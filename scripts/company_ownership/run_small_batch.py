"""
run_small_batch.py

בדיקה מקצה לקצה על מספר קטן של חברות: קורא selection_plan.json,
בוחר N חברות עם דוח סיכום, מוריד את ה-PDF בפועל מ-Maya (לא קובץ מקומי),
שולח ל-Gemini, שומר ל-private_subsidiaries.jsonl.

לא נוגע בדוחות שינויים (change) בשלב הזה - רק snapshot, כדי לשמור את
הבדיקה הראשונה פשוטה.

שלושה תיקונים (ראה diagnose_multipart.py לפרטי האבחון):

1. עיבוד כל קבצי ה-PDF: הקוד הישן עצר על ה-PDF הראשון שהחזיר תוצאה
   כלשהי, ומעולם לא ניסה extra_pdfs - גם כשהם הכילו מידע משמעותי
   (בדיסקונט: 11 חברות בגרסה הישנה מול 58 בפועל בכל הקבצים ביחד).
   עכשיו מעבדים את כולם וממזגים (dedup לפי שם).

2. בדיקה ממוקדת בתוך-קובץ: קובץ בודד יכול "לדלל" את תשומת הלב של
   Gemini אם הוא ענק (מאות עמודים) - נמצאה טבלה פורמלית (תקנה 11 וכו')
   עם page_reference אבל רק מעט מדי רשומות משויכות לאותו עמוד בדיוק.
   כשזה קורה, גוזרים (pypdf) רק את טווח העמודים סביב הטבלה ומריצים
   עליו בדיקה נפרדת - נצפה באלרוב נדל"ן: 19 חברות במסמך המלא מול 68
   בקטע ממוקד של 100 עמודים סביב הטבלה (פי 3.6).

3. ויתור מהיר על מכסה: כש-run_full_batch.py מזהה שהמכסה היומית נגמרה
   (QUOTA_EXCEEDED, threading.Event משותף), משימות שכבר בתור מוותרות
   מיד במקום להמשיך לנסות קירורים מלאים (עד כמה דקות לכל ניסיון) על
   מפתחות שכבר ידוע שאין בהם כלום - זה מה שגרם לריצה אחת (445 "דקות"
   בטעות בחישוב, בפועל ~5 שעות) להימשך שעות אחרי שהמכסה כבר נגמרה.

הרצה:
    py run_small_batch.py --plan selection_plan.json --n 5
    py run_small_batch.py --plan selection_plan.json --company-ids 694,2514
"""

import argparse
import os
import re
import io
import json
import threading
import time
from collections import Counter

from pypdf import PdfReader, PdfWriter

import extract_subsidiaries as ex
import maya_reports_client as mrc

# משותף עם run_full_batch.py - run_full_batch מחליף את זה באותו
# threading.Event שהוא עצמו בודק בלולאת as_completed שלו (לא יוצר
# חדש), כדי ששני הצדדים יראו את אותו דגל. כשלא רצים דרך run_full_batch
# (למשל run_small_batch.py ישירות, או test_fixed_pipeline.py) זה נשאר
# Event רגיל שאף אחד לא מפעיל - התנהגות זהה לקודם.
QUOTA_EXCEEDED = threading.Event()

FORMAL_TABLE_MARKERS = ["תקנה 11", "List of Subsidiaries", "Organizational Structure"]
FOCUSED_RECHECK_THRESHOLD = 3
CROP_PAGES_BEFORE = 40
CROP_PAGES_AFTER = 60

# ── חיתוך עיוור (רק למודלים ב-CHUNK_MODELS, כרגע 3.5-flash-lite) ────────
# למה עיוור ולא סביב-טבלה: ההחזקות לא תמיד בטבלה פורמלית - לפעמים בפרוזה
# מפוזרת. חיתוך שיטתי של כל הדוח לחלונות חופפים מכסה 100% מהעמודים בלי
# להסתמך על זיהוי טבלה. 3.6 נשאר מסלול מלא (call יקר) - לא נכנס לכאן.
CHUNK_MODELS = {"gemini-3.5-flash-lite"}
CHUNK_PAGES = 20            # גודל חלון (עמודים). קבוע לכוונון - לא לרדת מ-15
CHUNK_OVERLAP = 3          # בלי סיבה. חפיפה שלא לחתוך טבלה בגבול חלון.
MAX_WINDOW_ATTEMPTS = 3     # חלון שנכשל (לא-מכסה) כך-וכך פעמים מסומן ככשל
                           # לצמיתות עם תוצאה ריקה, כדי שהדוח יוכל להשלים
                           # ולא ייתקע ב-partial לנצח (אזהרה רועשת נרשמת).

# ערך-סימון שמחזיר _process_report כשנתחים חסרים (לא הצלחה ולא כישלון) -
# run_full_batch.py מזהה אותו ולא קורא ל-mark (ר' שם).
PARTIAL = "partial"

# מוזרקים מ-run_full_batch.py (כמו QUOTA_EXCEEDED) - גישה למאגר הדוחות כדי
# לשמור התקדמות-נתחים בין ריצות. כשרצים run_small_batch.py לבד (בדיקה) אלה
# נשארים no-op: אין resumability וה-jsonl נכתב רק אם הריצה הושלמה - התנהגות
# סבירה להרצה ידנית.
def CHUNK_PROGRESS_GET(report_id):          # מוחלף ע"י run_full_batch
    return None
def CHUNK_PROGRESS_PUT(report_id, company_id, kind, chunks):  # מוחלף
    pass


def _is_formal_table(sub: dict) -> bool:
    for field in (sub.get("section_title"), sub.get("table_title")):
        if field and any(m in field for m in FORMAL_TABLE_MARKERS):
            return True
    return False


def _find_focused_recheck_candidate(subsidiaries: list[dict]) -> int | None:
    """מחפש רשומה שמסומנת כטבלה פורמלית עם page_reference - ומחזיר את
    העמוד אם רק FOCUSED_RECHECK_THRESHOLD רשומות או פחות משויכות
    לעמוד הזה בדיוק. None אם אין חשד."""
    formal = [s for s in subsidiaries if _is_formal_table(s) and s.get("page_reference")]
    if not formal:
        return None
    page_counts = Counter(s["page_reference"] for s in formal)
    page, count = min(page_counts.items(), key=lambda x: x[1])
    return page if count <= FOCUSED_RECHECK_THRESHOLD else None


def _crop_pdf_pages(pdf_bytes: bytes, center_page: int) -> bytes | None:
    """גוזר טווח עמודים סביב center_page (1-indexed, כפי שמדווח ע"י
    Gemini ב-page_reference). None אם center_page לא סביר או שקריאת
    ה-PDF נכשלה."""
    try:
        reader = PdfReader(io.BytesIO(pdf_bytes))
    except Exception as e:
        print(f"    שגיאת קריאת PDF לגזירה: {e}")
        return None

    n_pages = len(reader.pages)
    idx = center_page - 1
    if idx < 0 or idx >= n_pages:
        print(f"    center_page={center_page} מחוץ לטווח ({n_pages} עמודים בפועל) - מדלג.")
        return None

    start = max(0, idx - CROP_PAGES_BEFORE)
    end = min(n_pages, idx + CROP_PAGES_AFTER)

    writer = PdfWriter()
    for i in range(start, end):
        writer.add_page(reader.pages[i])

    buf = io.BytesIO()
    writer.write(buf)
    print(f"    נגזר טווח עמודים {start+1}-{end} (מתוך {n_pages}) סביב עמוד {center_page}.")
    return buf.getvalue()


def normalize_list_response(raw: list) -> dict:
    """Gemini החזיר רשימה במקום אובייקט. שני מקרים:
    - התשובה המלאה עטופה ברשימה: [{"subsidiaries": [...], ...}] - לוקחים את האובייקט(ים) ומאחדים את הרשימות שבהם.
      (באג שתוקן 25/11: בעבר זה נשמר כ"חברת בת" אחת - כל חברות הבת שבחלון אבדו.)
    - רשימת חברות בת עצמה: [{"name": ...}, ...]."""
    wrapped = [x for x in raw if isinstance(x, dict) and ("subsidiaries" in x or "change_events" in x)]
    if wrapped:
        out = {k: v for k, v in wrapped[0].items() if k not in ("subsidiaries", "change_events")}
        out["subsidiaries"] = [s for x in wrapped for s in (x.get("subsidiaries") or [])]
        out["change_events"] = [e for x in wrapped for e in (x.get("change_events") or [])]
        print(f"  Gemini עטף את התשובה ברשימה - {len(out['subsidiaries'])} חברות בת חולצו ממנה.")
        return out
    print(f"  אזהרה: Gemini החזיר מערך גולמי ({len(raw)} פריטים) במקום {{'subsidiaries': [...]}} - מנרמל.")
    return {"subsidiaries": [x for x in raw if isinstance(x, dict)], "change_events": []}


def _call_gemini_with_quota_retry(pdf_bytes: bytes, filename_hint: str,
                                   quota_retries: int = 4, model: str | None = None) -> dict | None:
    """עוטף call_gemini_extraction עם retry-with-cooldown מקומי על
    מכסה. הבאג שנצפה בפועל: עם max_retries=2 (ברירת המחדל) ו-N מפתחות
    במאגר, מספיק שמפתח *אחד* נכנס לקירור זמני (65s, לא מכסה אמיתית)
    כדי שהפונקציה תזרוק GeminiQuotaExceededError בלי לתת הזדמנות
    הוגנת לשאר המפתחות. כאן: max_retries=6 בקריאה עצמה (סבב מלא) +
    שכבת retry חיצונית שממתינה קירור מלא בין ניסיונות - אבל בודקת
    QUOTA_EXCEEDED בתחילת כל ניסיון ומוותרת מיד אם threads אחרים כבר
    זיהו שזו מכסה אמיתית (לא מבזבזת עוד קירורים על מפתחות ריקים)."""
    last_quota_error = None
    for attempt in range(quota_retries):
        if QUOTA_EXCEEDED.is_set() or ex.all_keys_daily_exhausted():
            raise ex.GeminiQuotaExceededError(
                "מכסה יומית אמיתית (RPD) כבר זוהתה - מוותר מיד בלי קירור נוסף."
            )
        try:
            raw = ex.call_gemini_extraction(pdf_bytes, filename_hint=filename_hint, max_retries=6, model=model)
            if isinstance(raw, list):
                raw = normalize_list_response(raw)
            return raw
        except ex.GeminiQuotaExceededError as e:
            last_quota_error = e
            if ex.all_keys_daily_exhausted():
                # לא עוד קירור - זו כבר מכסה יומית ידועה, לא תיפתר תוך 65s
                raise
            print(f"    כל המפתחות בקירור (ניסיון {attempt + 1}/{quota_retries}) - "
                  f"ממתין {ex.GeminiKeyPool.COOLDOWN_SECONDS}s לפני ניסיון נוסף...")
            time.sleep(ex.GeminiKeyPool.COOLDOWN_SECONDS)
        except Exception as e:
            print(f"  שגיאת חילוץ: {e}")
            return None
    raise ex.GeminiQuotaExceededError(
        f"מכסה נגמרה גם אחרי {quota_retries} ניסיונות: {last_quota_error}"
    )


PROSPECTUS_KEYS = ("תרשים אחזקות", "תרשים החזקות", "מבנה האחזקות", "מבנה ההחזקות", "תרשים מבנה",
                   "חברות מוחזקות", "תאגידים מוחזקים", "חברות בנות", "חברות הנכס", "שותפויות הנכס")


def _prospectus_pages(pdf_bytes: bytes) -> bytes | None:
    """תשקיף הוא מאות עמודים. בוחרים את העמודים שמזכירים מבנה החזקות / חברות בנות, ועוד שני עמודים אחרי כל אחד
    (התרשים עצמו הוא לרוב תמונה בעמוד שאחרי הכותרת) - בלי תקרה, כדי לא לפספס. None = לא נמצא - שולחים את הכל."""
    try:
        reader = PdfReader(io.BytesIO(pdf_bytes))
        n = len(reader.pages)
        score = []
        for i, pg in enumerate(reader.pages):
            t = pg.extract_text() or ""
            k = sum(t.count(x) for x in PROSPECTUS_KEYS)
            if k:
                score.append((k, i))
        if not score:
            return None
        pick = set()
        for _, i in score:
            for j in (i, i + 1, i + 2):
                if j < n:
                    pick.add(j)
        w = PdfWriter()
        for j in sorted(pick):
            w.add_page(reader.pages[j])
        out = io.BytesIO(); w.write(out)
        print(f"  תשקיף: {n} עמודים -> {len(pick)} עמודים רלוונטיים")
        return out.getvalue()
    except Exception as e:
        print(f"  תשקיף: בחירת עמודים נכשלה ({e}) - נשלח המסמך המלא")
        return None


def _try_extract(pdf_url: str) -> dict | None:
    """מוריד ומחלץ מ-URL בודד. מחזיר None בכישלון (במקום לזרוק), כדי
    שהקורא יוכל לנסות PDF הבא באותו דוח בלי להפיל את כל התהליך.

    אחרי חילוץ המסמך המלא - אם נמצא חשד לטבלה פורמלית שנחתכה, גוזר
    טווח עמודים סביבה ומריץ בדיקה ממוקדת נפרדת, וממזג (dedup לפי name)
    כל חברה חדשה שנמצאה לתוך התוצאה. לא מחליף - רק מוסיף."""
    is_prosp = pdf_url.endswith("#prospectus")
    if is_prosp:
        pdf_url = pdf_url[:-len("#prospectus")]
    try:
        print(f"  מוריד {pdf_url} ...")
        pdf_bytes = mrc.download_report_file(pdf_url)
    except Exception as e:
        print(f"  שגיאת הורדה: {e}")
        return None
    if is_prosp:
        sliced = _prospectus_pages(pdf_bytes)
        if sliced:
            pdf_bytes = sliced

    print(f"  ({len(pdf_bytes):,} bytes) שולח ל-Gemini...")
    result = _call_gemini_with_quota_retry(pdf_bytes, pdf_url)
    if result is None:
        return None

    candidate_page = _find_focused_recheck_candidate(result.get("subsidiaries", []))
    if candidate_page and not QUOTA_EXCEEDED.is_set():
        print(f"    חשד: טבלה פורמלית בעמוד {candidate_page} עם "
              f"{FOCUSED_RECHECK_THRESHOLD} רשומות או פחות - מריץ בדיקה ממוקדת...")
        cropped = _crop_pdf_pages(pdf_bytes, candidate_page)
        if cropped:
            try:
                focused_result = _call_gemini_with_quota_retry(
                    cropped, f"{pdf_url}#focused_page_{candidate_page}"
                )
            except ex.GeminiQuotaExceededError:
                focused_result = None  # לא מפילים את כל התוצאה בגלל הבדיקה הנוספת
            if focused_result:
                existing_names = {s.get("name") for s in result.get("subsidiaries", [])}
                new_subs = [
                    s for s in focused_result.get("subsidiaries", [])
                    if s.get("name") and s.get("name") not in existing_names
                ]
                if new_subs:
                    print(f"    בדיקה ממוקדת הוסיפה {len(new_subs)} חברות בת נוספות.")
                    result.setdefault("subsidiaries", []).extend(new_subs)
            time.sleep(2)

    return result


LEGAL_TOKENS = re.compile(
    r'בע"?מ|בע״מ|בעמ|ש\.?מ\.?|שותפות\s+מוגבלת|שותפות\s+כללית|שותפות\s+רשומה|\(?\s*בפירוק\s*\)?|'
    r'\b(?:ltd|limited|llc|l\.l\.c|inc|incorporated|corp|corporation|co|l\.?p|plc|b\.?v|n\.?v|gmbh|ag|s\.?a|s\.?r\.?l|'
    r's\.?r\.?o|sp\.?\s*z\.?\s*o\.?\s*o|kft|ab|sia|pte|pty|the)\b\.?', re.I)


def _name_key(name: str) -> str:
    """מפתח להשוואת שמות: בלי צורת התאגדות, רווחים ופיסוק (אותו שם בכתיב אחר -> אותו מפתח)."""
    s = LEGAL_TOKENS.sub(" ", str(name or "").lower())
    return re.sub(r"[^0-9a-z\u0590-\u05FF]", "", s)


def _dedup_names(subs: list[dict]) -> list[dict]:
    """איחוד פריטים שהם אותה ישות בכתיב אחר (מפתח _name_key זהה, או שם של אחד מופיע ב-alt_names של אחר).
    נשמר הפריט המלא יותר (עם אחוז, שם ארוך יותר); השמות האחרים נוספים ל-alt_names."""
    out, by_key = [], {}
    def score(x):
        return (x.get("ownership_pct") is not None, not x.get("name_is_generic"), len(str(x.get("name") or "")))
    for s in subs:
        keys = {_name_key(s.get("name"))} | {_name_key(a) for a in s.get("alt_names") or []}
        keys.discard("")
        hit = next((by_key[k] for k in keys if k in by_key), None)
        if hit is None:
            s = dict(s)
            out.append(s)
            for k in keys:
                by_key[k] = s
            continue
        keep, other = (hit, s) if score(hit) >= score(s) else (s, hit)
        alts = list(dict.fromkeys([*(keep.get("alt_names") or []), other.get("name"), *(other.get("alt_names") or [])]))
        alts = [a for a in alts if a and a != keep.get("name")]
        if keep is s:                                   # הפריט החדש עדיף - מחליף את הקיים באותו מקום
            hit.clear()
            hit.update(s)
        target = hit
        target["alt_names"] = alts
        for k in keys | {_name_key(a) for a in alts}:
            if k:
                by_key[k] = target
    return out


def _merge_extraction_results(results: list[dict]) -> dict:
    """ממזג את תוצאות כל קבצי ה-PDF של אותו דוח לרשומה אחת:
    - subsidiaries: איחוד עם dedup לפי name (הרשומה הראשונה שנתקלים
      בה זוכה - סדר העיבוד הוא ראשי קודם).
    - change_events: איחוד עם dedup לפי (company, event_date,
      event_description).
    - מטא-דאטה ברמת המסמך (report_format/as_of_date/document_type):
      הערך הראשון שאינו None, לפי סדר עיבוד (ראשי עדיף)."""
    merged_subs: dict[str, dict] = {}
    merged_events: dict[tuple, dict] = {}
    meta = {"report_format": None, "as_of_date": None, "document_type": None}

    for result in results:
        for key in meta:
            if meta[key] is None and result.get(key):
                meta[key] = result[key]
        for s in result.get("subsidiaries", []):
            name = s.get("name")
            if name and name not in merged_subs:
                merged_subs[name] = s
        for ev in result.get("change_events", []):
            dedup_key = (ev.get("company"), ev.get("event_date"), ev.get("event_description"))
            if dedup_key not in merged_events:
                merged_events[dedup_key] = ev

    subs = list(merged_subs.values())
    if os.environ.get("EXTRACTION_PROMPT", "v1") == "v2":
        subs = _dedup_names(subs)
    return {
        **meta,
        "subsidiaries": subs,
        "change_events": list(merged_events.values()),
    }


def _iter_page_windows(n_pages: int) -> list[tuple[int, int]]:
    """חלונות (start,end) 0-indexed, end בלעדי, בגודל CHUNK_PAGES עם חפיפה
    CHUNK_OVERLAP. מכסים 100% מהעמודים. דוח <= CHUNK_PAGES = חלון יחיד על
    כל הקובץ (אין מה לחתוך)."""
    if n_pages <= 0:
        return []
    if n_pages <= CHUNK_PAGES:
        return [(0, n_pages)]
    step = CHUNK_PAGES - CHUNK_OVERLAP
    windows, start = [], 0
    while start < n_pages:
        end = min(start + CHUNK_PAGES, n_pages)
        windows.append((start, end))
        if end >= n_pages:
            break
        start += step
    return windows


# ---- סינון עמודים (GEMINI_PAGE_FILTER=1, ניסוי): רק עמודים שכנראה מופיעות בהם חברות בת נשלחים ל-Gemini ----
# כותרות חזקות - עמוד עם טבלה / תרשים של חברות הבת (לא כל אזכור של "חברות בנות" בבאורים)
PAGE_KEYS = re.compile(
    r"תקנה\s*11|חברות\s+(?:בנות\s+ו)?(?:מוחזקות|כלולות)\s+(?:עיקריות|מהותיות)|רשימת\s+(?:ה)?חברות|"
    r"(?:תרשים|מבנה)\s+(?:ה)?(?:[אה]חזקות|החזקות|אחזקות|הקבוצה|התאגיד)|תרשים\s+מבנה|"
    r"השקע(?:ה|ות)\s+בחברות\s+(?:מוחזקות|כלולות|בנות)|השקעות\s+עיקריות|פרטים\s+אודות\s+השקעות|נתוני\s+(?:ה)?פרויקטים|זכיין|"
    r"List\s+of\s+(?:Significant\s+)?Subsidiaries|(?:Principal|Significant)\s+subsidiaries|Group\s+Entities|"
    r"Investments?\s+in\s+(?:subsidiaries|investees|associates)|Organizational\s+Structure|Holding\s+structure|Group\s+structure", re.I)
ENTITY = re.compile(r'בע"?מ|בע״מ|שותפות|\bLtd\b|\bLimited\b|\bLLC\b|\bInc\b|\bB\.?V\b|\bGmbH\b|\bS\.?A\b|\bL\.?P\b|'
                    r'\bS\.?R\.?L\b|\bsp\.? z|\bPLC\b|\bCorp', re.I)
# משפט החזקה בטקסט חופשי (דוח הדירקטוריון / תיאור עסקי התאגיד): "הבנק מחזיק 100% מ...", "מחזיקה ב-51%", "holds 70%"
HOLD_STMT = re.compile(r"מחזיק(?:ה|ים|ות)?\s+(?:ב[-־]?\s*)?(?:כ[-־]?\s*)?(?:\d[\d.,]*\s*%|%\s*\d[\d.,]*)|"
                       r"\d[\d.,]*\s*%\s+(?:מ)?(?:הון|זכויות|מניות|אחזקות|החזקות)|"
                       r"(?:holds?|owns?|holding)\s+(?:approximately\s+|about\s+)?\d[\d.,]*\s*%", re.I)
PAGE_PAD = 1


def _page_texts(pdf_bytes: bytes, n_pages: int):
    """טקסט כל עמוד (pdftotext - מהיר, ב-C). None אם אין pdftotext או שהפלט לא תואם."""
    import shutil
    import subprocess
    import tempfile
    if not shutil.which("pdftotext"):
        return None
    with tempfile.NamedTemporaryFile(suffix=".pdf") as f:
        f.write(pdf_bytes)
        f.flush()
        try:
            out = subprocess.run(["pdftotext", "-q", f.name, "-"], capture_output=True, timeout=300).stdout
        except subprocess.TimeoutExpired:
            return None
    # תווי כיווניות בלתי נראים (pdftotext עוטף בהם רצפי מספרים בעברית: "מחזיק \u202a100%\u202c") - מסירים לפני כל התאמה
    txt = re.sub("[\u200e\u200f\u202a-\u202e\u2066-\u2069]", "", out.decode("utf-8", "replace"))
    pages = txt.split("\f")
    return pages[:n_pages] if len(pages) >= n_pages else None


def _relevant_pages(pdf_bytes: bytes, n_pages: int):
    """עמודים (0-based) שכנראה מופיעות בהם חברות בת: כותרת רלוונטית, או ריכוז של שמות ישויות ואחוזים; ועמוד עם מעט
    טקסט (תרשים / תמונה) ליד עמוד כזה. + PAGE_PAD לכל כיוון. None - אין סינון (כל העמודים)."""
    texts = _page_texts(pdf_bytes, n_pages)
    if not texts:
        return None
    hit = set()
    for i, t in enumerate(texts):
        t2 = t + "\n" + "\n".join(line[::-1] for line in t.splitlines())   # עברית בסדר חזותי (הפוך) בחלק מהקבצים
        ent = len(ENTITY.findall(t2))
        if PAGE_KEYS.search(t2) or ent >= 8 or (ent >= 5 and t.count("%") >= 3) or len(HOLD_STMT.findall(t2)) >= 1:
            hit.add(i)
    low = {i for i, t in enumerate(texts) if len(t.strip()) < 300}
    hit |= {i for i in low if any(abs(i - j) <= 2 for j in hit)}
    sel = sorted({q for i in hit for q in range(i - PAGE_PAD, i + PAGE_PAD + 1) if 0 <= q < n_pages})
    return sel or None


CHART_KEYS = re.compile(
    r"(?:תרשים|מבנה)\s+(?:ה)?(?:[אה]חזקות|החזקות|אחזקות|הקבוצה|התאגיד)|תרשים\s+מבנה|"
    r"Organizational\s+(?:Structure|chart)|Holding\s+structure|Group\s+structure|Structure\s+chart", re.I)


CHART_WORDS = re.compile(
    r"חברות\s+בנות|חבר(?:ה|ות)\s+בת|מוחזק(?:ות|ת)|תרשים|מבנה\s+ה?(?:[אה]חזקות|החזקות|אחזקות|קבוצה|תאגיד)|"
    r"Organi[sz]ational\s+(?:Structure|chart)|Holding\s+structure|Group\s+structure|Structure\s+chart|Subsidiar", re.I)
OCR_MAX_PAGES = 120          # תקרה לעמוד-תמונה בקובץ (OCR ~5 שניות לעמוד)


def _ocr_page(pdf_path: str, page: int):
    """OCR לעמוד אחד (1-based): pdftoppm 300dpi -> tesseract אנגלית+עברית, psm 11 (טקסט מפוזר, כמו בתרשים)."""
    import shutil
    import subprocess
    import tempfile
    if not (shutil.which("tesseract") and shutil.which("pdftoppm")):
        return None
    env = {**os.environ, "OMP_THREAD_LIMIT": "1"}     # tesseract בתהליכון אחד - כמה דוחות במקביל לא חונקים את המעבד
    try:
        with tempfile.TemporaryDirectory() as d:
            subprocess.run(["pdftoppm", "-f", str(page), "-l", str(page), "-r", "200", "-png", pdf_path, os.path.join(d, "p")],
                           capture_output=True, timeout=120)
            imgs = sorted(f for f in os.listdir(d) if f.endswith(".png"))
            if not imgs:
                return None
            r = subprocess.run(["tesseract", os.path.join(d, imgs[0]), "-", "-l", "eng+heb", "--psm", "11"],
                               capture_output=True, timeout=180, env=env)
            return r.stdout.decode("utf-8", "replace")
    except (subprocess.TimeoutExpired, OSError):
        return ""            # עמוד שלא הצליח - מדלגים עליו, לא מפילים את הקובץ


def _chart_pages(pdf_bytes: bytes, n_pages: int, detail: list | None = None):
    """עמודי תרשים מבנה ההחזקות (GEMINI_PAGE_FILTER=charts - למודל יקר). עמוד כמעט בלי טקסט (תמונה) עובר OCR, והוא
    תרשים אם יש בו הרבה אחוזים (8+; אחוזי החזקה על החצים), או מילת מפתח ("חברות בת", "מוחזקות", "תרשים",
    "מבנה החזקות", Organizational structure...) בו או בעמוד שלפניו (הכותרת "להלן תרשים..." לרוב בעמוד הקודם, כטקסט).
    עמוד תמונות בלי אחוזים (למשל תמונות של נכסים בשומה) - לא. detail - לבדיקות: [(עמוד, אחוזים, מילת מפתח)]."""
    import tempfile
    texts = _page_texts(pdf_bytes, n_pages)
    if not texts:
        return None
    rev = lambda t: t + "\n" + "\n".join(line[::-1] for line in t.splitlines())
    low = [i for i, t in enumerate(texts) if len(t.strip()) < 300][:OCR_MAX_PAGES]
    if not low:
        return None
    ocr = {}
    with tempfile.NamedTemporaryFile(suffix=".pdf") as f:
        f.write(pdf_bytes)
        f.flush()
        for i in low:
            ocr[i] = _ocr_page(f.name, i + 1) or ""
    sel = []
    for i in low:
        t = ocr[i]
        pct = len(re.findall(r"\d\s?%|%\s?\d", t))
        prev = (ocr.get(i - 1) or texts[i - 1]) if i > 0 else ""
        kw = bool(CHART_WORDS.search(rev(t)) or CHART_WORDS.search(rev(prev)))
        if pct >= 8 or (kw and pct >= 2):
            sel.append(i)
            if detail is not None:
                detail.append((i + 1, pct, kw))
    return sel or None


def _formal_pages(pdf_bytes: bytes, n_pages: int):
    """רק העמודים של הרשימות הרשמיות (לסט האמת): עמוד עם כותרת חזקה (תקנה 11, רשימת חברות מוחזקות, תרשים מבנה,
    Group Entities, List of Subsidiaries...) ועמודי ההמשך של הטבלה (כל עוד יש בהם שמות ישויות או אחוזים, עד 15),
    + עמודי תרשים שהם תמונה (OCR). None - לא נמצא."""
    texts = _page_texts(pdf_bytes, n_pages)
    if not texts:
        return None
    rev = lambda t: t + "\n" + "\n".join(line[::-1] for line in t.splitlines())
    sel = set()
    for i, t in enumerate(texts):
        if PAGE_KEYS.search(rev(t)):
            sel.add(i)
            for j in range(i + 1, min(n_pages, i + 16)):
                tj = rev(texts[j])
                if len(ENTITY.findall(tj)) >= 3 or texts[j].count("%") >= 3:
                    sel.add(j)
                else:
                    break
    try:
        sel |= set(_chart_pages(pdf_bytes, n_pages) or [])
    except Exception:
        pass
    return sorted(sel) or None


# דוחות שינויים (רבעוניים): רק עמודים שמתארים שינוי בהחזקות - אירועים מהותיים, רכישה / מכירה / מיזוג / הקמה, משפט החזקה
EVENT_KEYS = re.compile(
    r"אירועים\s+(?:מהותיים|בתקופת|לאחר)|רכיש(?:ה|ת)|רכשה|מכיר(?:ה|ת)\s+(?:של\s+)?(?:מניות|החזק|אחזק|זכויות)|מימוש|מיזוג|"
    r"השלמת\s+(?:ה)?עסקה|הקמת\s+(?:חברה|שותפות)|התקשרות\s+בהסכם|הסכם\s+(?:לרכישת|למכירת)|דילול|"
    r"acqui(?:re|sition)|disposal|divest|merger|completion\s+of|subsequent\s+events|significant\s+events", re.I)


def _change_pages(pdf_bytes: bytes, n_pages: int):
    """עמודי שינויים בדוח רבעוני: כותרת/מילת אירוע (EVENT_KEYS) יחד עם אחוזים או ישויות, או משפט החזקה (HOLD_STMT),
    או טבלת החברות המוחזקות (PAGE_KEYS). ±PAGE_PAD."""
    texts = _page_texts(pdf_bytes, n_pages)
    if not texts:
        return None
    hit = set()
    for i, t in enumerate(texts):
        t2 = t + "\n" + "\n".join(line[::-1] for line in t.splitlines())
        ev = len(EVENT_KEYS.findall(t2))
        if PAGE_KEYS.search(t2) or HOLD_STMT.search(t2) or (ev and ("%" in t or len(ENTITY.findall(t2)) >= 2)):
            hit.add(i)
    sel = sorted({q for i in hit for q in range(i - PAGE_PAD, i + PAGE_PAD + 1) if 0 <= q < n_pages})
    return sel or None


def _page_windows(pages: list[int]) -> list[list[int]]:
    """רשימת עמודים נבחרים -> חלונות של עד CHUNK_PAGES עמודים (בלי חפיפה - העמודים כבר מורחבים בשוליים)."""
    return [pages[i:i + CHUNK_PAGES] for i in range(0, len(pages), CHUNK_PAGES)]


def _win_pages(w) -> list[int]:
    if isinstance(w, dict):
        return list(w["pages"])
    return list(range(w[0], w[1])) if isinstance(w, tuple) else list(w)


CHART_MODEL = os.environ.get("GEMINI_CHART_MODEL", "").strip()     # למשל gemini-4-hydrogen; ריק - בלי מעבר תרשימים


def _page_filter(source_type: str = "annual_report") -> str:
    """סינון העמודים לדוח: GEMINI_PAGE_FILTER לדוח השנתי; לדוחות השינויים GEMINI_PAGE_FILTER_CHANGES אם הוגדר
    (למשל changes - עמודי אירועי שינוי בלבד), אחרת כמו השנתי."""
    pf = os.environ.get("GEMINI_PAGE_FILTER", "")
    if source_type != "annual_report":
        pf = os.environ.get("GEMINI_PAGE_FILTER_CHANGES") or pf
    return pf


def _scheme(source_type: str = "annual_report") -> str:
    """זהות תוכנית החלונות - התקדמות שנשמרה בתוכנית אחרת (גודל/סינון/מודל תרשימים) לא מתערבבת: מתאפסת."""
    return f"{CHUNK_PAGES}/{CHUNK_OVERLAP}/{_page_filter(source_type)}/{CHART_MODEL}"


def _slice_pages(reader: PdfReader, pages: list[int]) -> bytes:
    writer = PdfWriter()
    for i in pages:
        writer.add_page(reader.pages[i])
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def _map_page_refs(result: dict, pages: list[int]) -> dict:
    """page_reference יחסי לחלון (1 = העמוד הראשון בחלון) -> העמוד במסמך המקורי (1-based), גם בחלון לא רציף."""
    def _fix(rec):
        try:
            p = int(str(rec.get("page_reference")).strip())
        except (ValueError, TypeError):
            return
        if 1 <= p <= len(pages):
            rec["page_reference"] = pages[p - 1] + 1
    for s in result.get("subsidiaries", []):
        _fix(s)
    for ev in result.get("change_events", []):
        _fix(ev)
    return result


def _slice_pdf(reader: PdfReader, start: int, end: int) -> bytes:
    """גוזר עמודים [start,end) (0-indexed) ל-PDF חדש. reader כבר פתוח -
    לא קוראים את הקובץ מחדש לכל חלון."""
    writer = PdfWriter()
    for i in range(start, end):
        writer.add_page(reader.pages[i])
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def _offset_page_refs(result: dict, window_start0: int) -> dict:
    """page_reference שחוזר מ-Gemini הוא יחסי לתת-ה-PDF של החלון (עמוד 1 =
    העמוד הראשון בחלון). ממפה חזרה לקואורדינטת המסמך המקורי:
    עמוד-מקורי-1indexed = window_start0 + עמוד-מדווח. רק כשהערך מספרי -
    אחרת משאירים כמות שהוא (לא ממציאים מספר "בטוח אבל שגוי")."""
    def _fix(rec):
        pr = rec.get("page_reference")
        if pr is None:
            return
        try:
            p = int(str(pr).strip())
        except (ValueError, TypeError):
            return
        rec["page_reference"] = window_start0 + p
    for s in result.get("subsidiaries", []):
        _fix(s)
    for ev in result.get("change_events", []):
        _fix(ev)
    return result


def _kind_of(source_type: str) -> str:
    """ממפה source_type ל-kind של המאגר (כמו task_kind ב-run_full_batch)."""
    return "snapshot" if source_type == "annual_report" else "change"


def _process_report_chunked(
    company_id: str, company_name: str, report_id: str, publish_date: str,
    pdf_url: str, extra_pdfs: list[str], source_type: str,
):
    """מסלול חיתוך עיוור (3.5): חותך את כל קבצי הדוח לחלונות CHUNK_PAGES עם
    CHUNK_OVERLAP חפיפה, שולח כל חלון ל-Gemini וממזג. resumable בין ריצות -
    התקדמות-החלונות נשמרת במאגר הדוחות (design A). שורת jsonl אחת נכתבת רק
    כשכל החלונות הושלמו. מחזיר True (הושלם), PARTIAL (נתחים חסרים - ינוסה
    שוב), או False (כישלון מוחלט - אף קובץ לא נטען)."""
    all_pdf_urls = [pdf_url] + (extra_pdfs or [])

    # שלב 1: הורדת כל הקבצים + מספר עמודים -> תוכנית חלונות מלאה. חייבים את
    # total מראש כדי לדעת מתי "הושלם".
    files = []  # [{"url","reader","windows":[(s,e)...],"n_pages"}]
    for fi, url in enumerate(all_pdf_urls):
        try:
            pdf_bytes = mrc.download_report_file(url)
            if url.endswith("#prospectus"):
                pdf_bytes = _prospectus_pages(pdf_bytes) or pdf_bytes
            reader = PdfReader(io.BytesIO(pdf_bytes))
            n_pages = len(reader.pages)
            pf = _page_filter(source_type)
            sel = (_relevant_pages(pdf_bytes, n_pages) if pf == "1" else _chart_pages(pdf_bytes, n_pages) if pf == "charts"
                   else _formal_pages(pdf_bytes, n_pages) if pf == "formal"
                   else _change_pages(pdf_bytes, n_pages) if pf == "changes" else None)
            if pf == "charts" and sel is None:
                sel = []      # בלי תרשים - אין מה לשלוח למודל היקר
            if sel is not None:
                print(f"  סינון עמודים: {len(sel)}/{n_pages} עמודים נשלחים ({url.split('/')[-1]})")
            # GEMINI_CHART_MODEL: עמודי תרשים מבנה שהם תמונה (OCR) - בנוסף, למודל חזק יותר (חלונות נפרדים באותו דוח)
            try:
                charts = _chart_pages(pdf_bytes, n_pages) if CHART_MODEL and pf != "charts" else None
            except Exception as e:      # זיהוי התרשימים לא מפיל את החילוץ הרגיל של הקובץ
                print(f"  אזהרה: זיהוי תרשימים נכשל ({type(e).__name__}) - ממשיכים בלי")
                charts = None
            if charts:
                print(f"  תרשימי מבנה (תמונה): {len(charts)} עמ' -> {CHART_MODEL} ({url.split('/')[-1]})")
        except Exception as e:
            print(f"  אזהרה: קובץ {fi} ({url}) נכשל בהורדה/קריאה: {e} - מוחרג "
                  f"מתוכנית החלונות (הדוח לא ייחשב שלם עד שהקובץ יצליח).")
            continue
        if sel == []:
            print(f"  אין עמודי תרשים ב-{url.split('/')[-1]} - מדלג")
            continue
        wins = _page_windows(sel) if sel is not None else _iter_page_windows(n_pages)
        wins += [{"pages": w, "model": CHART_MODEL} for w in _page_windows(charts or [])]
        files.append({"url": url, "reader": reader, "n_pages": n_pages, "windows": wins})

    if not files:
        print("  כישלון מוחלט: אף קובץ PDF של הדוח לא נטען.")
        return False

    window_keys = [(fi, wi) for fi, f in enumerate(files)
                   for wi in range(len(f["windows"]))]
    total = len(window_keys)

    # שלב 2: טעינת התקדמות קודמת מהמאגר (resume).
    prog = CHUNK_PROGRESS_GET(report_id) or {}
    if prog.get("done") and prog.get("scheme", f"{CHUNK_PAGES}/{CHUNK_OVERLAP}//") != _scheme(source_type):
        print(f"  התקדמות קודמת בתוכנית חלונות אחרת ({prog.get('scheme', 'ישנה')}) - מתחילים מחדש.")
        prog = {}
    done = dict(prog.get("done", {}))              # {"fi:wi": <window result>}
    win_attempts = dict(prog.get("attempts", {}))  # {"fi:wi": int}

    def _persist():
        CHUNK_PROGRESS_PUT(report_id, str(company_id), _kind_of(source_type), {
            "size": CHUNK_PAGES, "overlap": CHUNK_OVERLAP, "scheme": _scheme(source_type),
            "total": total, "done": done, "attempts": win_attempts,
        })

    if done:
        print(f"  המשך: {len(done)}/{total} חלונות כבר הושלמו בריצה קודמת.")

    # שלב 3: עיבוד חלונות חסרים בלבד.
    for fi, wi in window_keys:
        key = f"{fi}:{wi}"
        if key in done:
            continue
        if QUOTA_EXCEEDED.is_set():
            print(f"  מכסה נגמרה - נשמרו {len(done)}/{total} חלונות, הדוח נשאר "
                  f"חלקי (ימשיך בהרצה הבאה).")
            _persist()
            return PARTIAL
        f = files[fi]
        wpages = _win_pages(f["windows"][wi])
        wmodel = f["windows"][wi].get("model") if isinstance(f["windows"][wi], dict) else None
        start, end = wpages[0], wpages[-1] + 1
        print(f"  חלון {key} (עמ' {start+1}-{end}/{f['n_pages']}, {len(wpages)} עמ', "
              f"{f['url'].split('/')[-1]}) -> {wmodel or 'Gemini'}...")
        win_bytes = _slice_pages(f["reader"], wpages)
        try:
            res = _call_gemini_with_quota_retry(win_bytes, f"{f['url']}#w{start+1}-{end}", model=wmodel)
            if res is None and not QUOTA_EXCEEDED.is_set():      # כישלון שאינו מכסה (JSON פגום וכו') - ניסיון חוזר מיידי אחד
                print(f"    חלון {key}: ניסיון חוזר מיידי")
                res = _call_gemini_with_quota_retry(win_bytes, f"{f['url']}#w{start+1}-{end}", model=wmodel)
        except ex.GeminiQuotaExceededError:
            print(f"  מכסה נגמרה בחלון {key} - נשמרו {len(done)}/{total}, חלקי.")
            _persist()
            return PARTIAL
        if res is None:
            win_attempts[key] = win_attempts.get(key, 0) + 1
            if win_attempts[key] >= MAX_WINDOW_ATTEMPTS:
                print(f"  אזהרה: חלון {key} נכשל {win_attempts[key]} פעמים "
                      f"(לא-מכסה) - מסומן ככשל לצמיתות (תוצאה ריקה) כדי שהדוח "
                      f"יוכל להשלים. עמ' {start+1}-{end} לא כוסו.")
                done[key] = {"subsidiaries": [], "change_events": [],
                             "_window_failed": True}
            _persist()
            continue
        done[key] = _map_page_refs(res, wpages)
        _persist()
        print(f"    חלון {key}: {len(res.get('subsidiaries', []))} חברות.")
        time.sleep(1)

    # שלב 4: כל החלונות הושלמו (או סומנו ככשל לצמיתות) -> מיזוג + שורת jsonl.
    if len(done) < total:
        _persist()  # רשת ביטחון - לא אמור לקרות בלי quota
        return PARTIAL

    failed = [k for k, v in done.items() if v.get("_window_failed")]
    merged = _merge_extraction_results(list(done.values()))
    if failed:
        print(f"  הושלם עם {len(failed)}/{total} חלונות שנכשלו לצמיתות (כיסוי "
              f"חלקי) - {len(merged['subsidiaries'])} חברות אחרי dedup.")
    else:
        print(f"  הושלם: {total} חלונות, {len(merged['subsidiaries'])} חברות "
              f"אחרי dedup.")

    ex.save_extraction_json(
        company_legal_id=str(company_id),
        report_id=report_id,
        source_type=source_type,
        extracted=merged,
        report_publish_date=publish_date,
    )
    return True


def _process_report(
    company_id: str, company_name: str, report_id: str, publish_date: str,
    pdf_url: str, extra_pdfs: list[str], source_type: str,
) -> bool:
    """מעבד דוח בודד (סיכום או שינוי): מעבד את כל קבצי ה-PDF שלו (ראשי +
    extra_pdfs) - את כולם, לא רק עד ההצלחה הראשונה - וממזג את התוצאות.
    משותף בין process_company (snapshot) ל-process_change_report."""

    # 3.5: מסלול חיתוך עיוור (resumable, בלי זיהוי טבלה). 3.6: מסלול מלא למטה.
    if ex.GEMINI_MODEL in CHUNK_MODELS or os.environ.get("GEMINI_CHUNK") == "1":   # GEMINI_CHUNK=1: חלונות לכל מודל
        return _process_report_chunked(
            company_id, company_name, report_id, publish_date,
            pdf_url, extra_pdfs, source_type,
        )

    print(f"  דוח: {report_id} ({source_type}), פורסם {publish_date}")

    all_pdf_urls = [pdf_url] + (extra_pdfs or [])
    successful_results = []

    for i, url in enumerate(all_pdf_urls):
        if QUOTA_EXCEEDED.is_set():
            print(f"  מכסה נגמרה - מוותר על שאר קבצי הדוח הזה.")
            if not successful_results:
                # קריטי: זורקים GeminiQuotaExceededError ולא סתם break+return
                # False. באג אמיתי שנצפה בפועל - החזרת False כאן גורמת
                # ל-run_task (run_full_batch.py) לסמן status="failed" רגיל,
                # בדיוק כמו כישלון אמיתי מול Gemini. זה "שורף" ניסיון-חוזר
                # (מתוך MAX_RETRY_ATTEMPTS=3) על דוח שמעולם לא באמת נוסה -
                # אחרי 3 פעמים כאלה (גם בלי שום ניסיון אמיתי) הדוח נחשב
                # כשל-לצמיתות ולא ינוסה שוב אף פעם. זה מה שגרם ל-2,583
                # "כישלונות" ברצף ברגע שהמכסה נגמרה - אף אחד מהם לא היה
                # כישלון אמיתי. אם כבר יש הצלחה חלקית (PDF קודם באותו דוח
                # כן הצליח) - לא זורקים, ממשיכים עם מה שיש (כמו קודם).
                raise ex.GeminiQuotaExceededError(
                    "מכסה יומית כבר זוהתה כנגמרת - הדוח לא נוסה כלל, ינוסה שוב בהרצה הבאה."
                )
            break
        label = "ראשי" if i == 0 else f"נוסף {i}"
        print(f"  --- מעבד PDF {label} ({i+1}/{len(all_pdf_urls)}) ---")
        candidate = _try_extract(url)
        if candidate is None:
            continue

        n_subs = len(candidate.get("subsidiaries", []))
        n_events = len(candidate.get("change_events", []))
        print(f"  התקבלו {n_subs} חברות, {n_events} אירועי שינוי מהקובץ הזה.")
        successful_results.append(candidate)

        if i < len(all_pdf_urls) - 1:
            time.sleep(2)  # נימוס בסיסי מול Gemini בין קבצים

    if not successful_results:
        print(f"  כישלון מוחלט על כל {len(all_pdf_urls)} קבצי ה-PDF.")
        return False

    result = _merge_extraction_results(successful_results)
    if len(all_pdf_urls) > 1:
        print(f"  מוזגו {len(successful_results)}/{len(all_pdf_urls)} קבצים שהצליחו: "
              f"סה\"כ {len(result['subsidiaries'])} חברות, "
              f"{len(result['change_events'])} אירועים אחרי dedup.")

    # הערה: parent_hp כאן הוא companyId של Maya, לא ח.פ (corporateId) -
    # המיפוי בין השניים עדיין לא מחובר (ראה maya_core.fetch_company_list
    # הקיים). לתקן לפני מעבר ל-D1 בפועל.
    ex.save_extraction_json(
        company_legal_id=str(company_id),
        report_id=report_id,
        source_type=source_type,
        extracted=result,
        report_publish_date=publish_date,
    )
    return True


def process_company(company_id: str, entry: dict) -> bool:
    """מעבד את דוח הסיכום (snapshot) של חברה - הדוח השנתי/20-F האחרון."""
    snap = entry.get("snapshot")
    if not snap:
        print(f"  {entry.get('company_name')} ({company_id}): אין דוח סיכום - מדלג.")
        return False

    print(f"\n=== {entry.get('company_name')} (companyId={company_id}) - סיכום ===")
    return _process_report(
        company_id=company_id,
        company_name=entry.get("company_name"),
        report_id=snap["report_id"],
        publish_date=snap["publish_date"],
        pdf_url=snap["pdf_url"],
        extra_pdfs=snap.get("extra_pdfs", []),
        source_type="annual_report",
    )


def process_change_report(company_id: str, company_name: str, change: dict) -> bool:
    """מעבד דוח שינויים בודד (רבעוני/מיידי) - אחד מתוך entry["changes"].
    כל דוח כזה הוא יחידת עבודה נפרדת עם report_id משלו (idempotency
    ברמת הדוח הבודד, לא ברמת החברה)."""

    print(f"\n=== {company_name} (companyId={company_id}) - עדכון ===")
    return _process_report(
        company_id=company_id,
        company_name=company_name,
        report_id=change["report_id"],
        publish_date=change["publish_date"],
        pdf_url=change["pdf_url"],
        extra_pdfs=change.get("extra_pdfs", []),
        source_type="quarterly_report",
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--plan", default="selection_plan.json")
    parser.add_argument("--n", type=int, default=5, help="כמה חברות לבדוק (מההתחלה)")
    parser.add_argument("--company-ids", default=None,
                         help="רשימת companyId מופרדת בפסיקים, לבחירה ידנית במקום --n")
    parser.add_argument("--sleep", type=float, default=5.0,
                         help="השהיה בשניות בין קריאות ל-Gemini")
    args = parser.parse_args()

    with open(args.plan, encoding="utf-8") as f:
        plan = json.load(f)

    if args.company_ids:
        ids = [cid.strip() for cid in args.company_ids.split(",")]
    else:
        ids = [cid for cid, e in plan.items() if e.get("snapshot")][: args.n]

    print(f"מריץ על {len(ids)} חברות: {ids}\n")

    n_ok, n_fail = 0, 0
    for i, cid in enumerate(ids):
        entry = plan.get(cid)
        if entry is None:
            print(f"companyId {cid} לא נמצא בתוכנית - מדלג.")
            n_fail += 1
            continue

        ok = process_company(cid, entry)
        if ok:
            n_ok += 1
        else:
            n_fail += 1

        if i < len(ids) - 1:
            time.sleep(args.sleep)

    print(f"\n=== סיכום ===")
    print(f"הצליחו: {n_ok}, נכשלו: {n_fail}")
    print("תוצאות ב-private_subsidiaries.jsonl")

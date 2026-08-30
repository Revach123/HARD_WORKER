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


def _call_gemini_with_quota_retry(pdf_bytes: bytes, filename_hint: str,
                                   quota_retries: int = 4) -> dict | None:
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
        if QUOTA_EXCEEDED.is_set():
            raise ex.GeminiQuotaExceededError(
                "מכסה כבר זוהתה כנגמרת ב-thread אחר - מוותר מיד בלי קירור נוסף."
            )
        try:
            raw = ex.call_gemini_extraction(pdf_bytes, filename_hint=filename_hint, max_retries=6)
            if isinstance(raw, list):
                print(f"  אזהרה: Gemini החזיר מערך גולמי ({len(raw)} פריטים) "
                      f"במקום {{'subsidiaries': [...]}} - מנרמל.")
                raw = {"subsidiaries": raw, "change_events": []}
            return raw
        except ex.GeminiQuotaExceededError as e:
            last_quota_error = e
            print(f"    כל המפתחות בקירור (ניסיון {attempt + 1}/{quota_retries}) - "
                  f"ממתין {ex.GeminiKeyPool.COOLDOWN_SECONDS}s לפני ניסיון נוסף...")
            time.sleep(ex.GeminiKeyPool.COOLDOWN_SECONDS)
        except Exception as e:
            print(f"  שגיאת חילוץ: {e}")
            return None
    raise ex.GeminiQuotaExceededError(
        f"מכסה נגמרה גם אחרי {quota_retries} ניסיונות: {last_quota_error}"
    )


def _try_extract(pdf_url: str) -> dict | None:
    """מוריד ומחלץ מ-URL בודד. מחזיר None בכישלון (במקום לזרוק), כדי
    שהקורא יוכל לנסות PDF הבא באותו דוח בלי להפיל את כל התהליך.

    אחרי חילוץ המסמך המלא - אם נמצא חשד לטבלה פורמלית שנחתכה, גוזר
    טווח עמודים סביבה ומריץ בדיקה ממוקדת נפרדת, וממזג (dedup לפי name)
    כל חברה חדשה שנמצאה לתוך התוצאה. לא מחליף - רק מוסיף."""
    try:
        print(f"  מוריד {pdf_url} ...")
        pdf_bytes = mrc.download_report_file(pdf_url)
    except Exception as e:
        print(f"  שגיאת הורדה: {e}")
        return None

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

    return {
        **meta,
        "subsidiaries": list(merged_subs.values()),
        "change_events": list(merged_events.values()),
    }


def _process_report(
    company_id: str, company_name: str, report_id: str, publish_date: str,
    pdf_url: str, extra_pdfs: list[str], source_type: str,
) -> bool:
    """מעבד דוח בודד (סיכום או שינוי): מעבד את כל קבצי ה-PDF שלו (ראשי +
    extra_pdfs) - את כולם, לא רק עד ההצלחה הראשונה - וממזג את התוצאות.
    משותף בין process_company (snapshot) ל-process_change_report."""

    print(f"  דוח: {report_id} ({source_type}), פורסם {publish_date}")

    all_pdf_urls = [pdf_url] + (extra_pdfs or [])
    successful_results = []

    for i, url in enumerate(all_pdf_urls):
        if QUOTA_EXCEEDED.is_set():
            print(f"  מכסה נגמרה - מוותר על שאר קבצי הדוח הזה.")
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

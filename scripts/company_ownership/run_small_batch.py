"""
run_small_batch.py

בדיקה מקצה לקצה על מספר קטן של חברות: קורא selection_plan.json,
בוחר N חברות עם דוח סיכום, מוריד את ה-PDF בפועל מ-Maya (לא קובץ מקומי),
שולח ל-Gemini, שומר ל-private_subsidiaries.jsonl.

לא נוגע בדוחות שינויים (change) בשלב הזה - רק snapshot, כדי לשמור את
הבדיקה הראשונה פשוטה.

תיקון (ראה diagnose_multipart.py): הקוד הישן עצר על ה-PDF הראשון
שהחזיר תוצאה כלשהי, ומעולם לא ניסה extra_pdfs - גם כשהם הכילו מידע
משמעותי (בדיסקונט: 11 חברות בגרסה הישנה מול 58 בפועל בכל הקבצים
ביחד). עכשיו: מעבדים את כל קבצי ה-PDF של כל דוח, וממזגים (dedup לפי
שם חברה). זה מכפיל פחות או יותר את מספר קריאות ה-Gemini לחברות עם
extra_pdfs (כ-147 מתוך 669 בתוכנית) - תמורה מכוונת בעד שלמות הנתונים.

הרצה:
    py run_small_batch.py --plan selection_plan.json --n 5
    py run_small_batch.py --plan selection_plan.json --company-ids 694,2514
"""

import argparse
import json
import time

import extract_subsidiaries as ex
import maya_reports_client as mrc


def _try_extract(pdf_url: str) -> dict | None:
    """מוריד ומחלץ מ-URL בודד. מחזיר None בכישלון (במקום לזרוק), כדי
    שהקורא יוכל לנסות PDF הבא באותו דוח בלי להפיל את כל התהליך.

    quota_retries=4 (עם max_retries=6 בקריאה הפנימית עצמה): הבאג
    שנצפה בפועל בסקריפט האבחון - עם max_retries=2 (ברירת המחדל) ו-3
    מפתחות במאגר, מספיק שמפתח *אחד* נכנס לקירור זמני (65s, לא מכסה
    אמיתית) כדי שהפונקציה תזרוק GeminiQuotaExceededError בלי לתת
    הזדמנות הוגנת לשני המפתחות האחרים. זה כנראה הסביר חלק מהכשלים
    הספורדיים בעבר (למשל workflow #32)."""
    try:
        print(f"  מוריד {pdf_url} ...")
        pdf_bytes = mrc.download_report_file(pdf_url)
    except Exception as e:
        print(f"  שגיאת הורדה: {e}")
        return None

    last_quota_error = None
    quota_retries = 4
    for attempt in range(quota_retries):
        try:
            print(f"  ({len(pdf_bytes):,} bytes) שולח ל-Gemini...")
            result = ex.call_gemini_extraction(pdf_bytes, filename_hint=pdf_url, max_retries=6)
            break
        except ex.GeminiQuotaExceededError as e:
            last_quota_error = e
            print(f"    כל המפתחות בקירור (ניסיון {attempt + 1}/{quota_retries}) - "
                  f"ממתין {ex.GeminiKeyPool.COOLDOWN_SECONDS}s לפני ניסיון נוסף...")
            time.sleep(ex.GeminiKeyPool.COOLDOWN_SECONDS)
        except Exception as e:
            print(f"  שגיאת חילוץ: {e}")
            return None
    else:
        # אחרי כל הניסיונות - זו כנראה באמת מכסה יומית (RPD) אמיתית,
        # לא רק קירור זמני. מעבירים הלאה - לא בולעים כמו כישלון רגיל,
        # כדי שהריצה הכוללת (run_full_batch) תדע לעצור ולא תמשיך
        # לבזבז זמן על חברות נוספות כשאין בכלל מפתחות זמינים.
        raise ex.GeminiQuotaExceededError(
            f"מכסה נגמרה גם אחרי {quota_retries} ניסיונות: {last_quota_error}"
        )

    # לפעמים Gemini מחזיר מערך גולמי [...] במקום {"subsidiaries": [...]} -
    # קרה בפועל, הפיל את כל הריצה (candidate.get על list זורק AttributeError).
    # מנרמלים כאן, במקור, כדי שכל הקוד שקורא ל-_try_extract תמיד יקבל dict.
    if isinstance(result, list):
        print(f"  אזהרה: Gemini החזיר מערך גולמי ({len(result)} פריטים) "
              f"במקום {{'subsidiaries': [...]}} - מנרמל.")
        result = {"subsidiaries": result, "change_events": []}

    return result


def _merge_extraction_results(results: list[dict]) -> dict:
    """ממזג את תוצאות כל קבצי ה-PDF של אותו דוח לרשומה אחת:
    - subsidiaries: איחוד עם dedup לפי name (הרשומה הראשונה שנתקלים
      בה זוכה - סדר העיבוד הוא ראשי קודם, אז אם אותה חברה מופיעה גם
      בראשי וגם בנוסף, גרסת הראשי נשמרת).
    - change_events: איחוד עם dedup לפי (company, event_date,
      event_description) - אין שדה id יציב לאירועי שינוי, זה הכי קרוב
      למפתח טבעי בלי לגרום לכפילויות בין קבצים חופפים.
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
        # רק חברות עם snapshot, לפי סדר הופעה בקובץ
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

"""
diagnose_multipart.py

אבחון (לא תיקון!) - שתי היפותזות נפרדות לגבי מה שהפייפליין הנוכחי
מחמיץ:

1. חיתוך בין-קבצי: run_small_batch._process_report עוצר על ה-PDF
   הראשון שמחזיר תוצאה כלשהי (n_subs>0) ולא ממשיך ל-extra_pdfs, גם אם
   יש בהם מידע רלוונטי נוסף.
2. חיתוך בתוך-קובץ: גם בקובץ הראשי היחיד, כשהוא ענק (מאות עמודים),
   ייתכן ש-Gemini מוצא את הטבלה הפורמלית (תקנה 11 וכו') אבל מחלץ ממנה
   רק חלק - כמו שנצפה בפועל בלאומי (עמוד 400 סומן כ"תקנה 11" אבל רק
   חברה אחת יוחסה אליו, כשלמעשה יש שם הרבה יותר - עמוד 338 ואילך).

מריץ על מדגם קבוע של 20 חברות (בנקים, ריט"ים, אחזקות נדל"ן גדולות) -
הסוג שבו סביר יותר למצוא טבלת תקנה 11 עשירה.

לכל חברה, לכל קובץ (ראשי + extra_pdfs): שולח את המסמך המלא ל-Gemini
(היפותזה 1). אם נמצאת רשומה שמסומנת כטבלה פורמלית (section/table_title
מכיל "תקנה 11" וכו') אבל רק מעט מאוד רשומות משויכות לאותו page_reference
בדיוק - חשד שהטבלה נחתכה - גוזר (pypdf) רק את טווח העמודים סביבה
ומריץ עליו בדיקה ממוקדת נפרדת (היפותזה 2), כדי לראות אם מסמך קטן
וממוקד גורם ל-Gemini לחלץ יותר.

לא כותב ל-private_subsidiaries.jsonl ולא ל-control.json - קובץ תוצר
נפרד (diagnose_results.json), שמור אחרי כל חברה (לא מצטבר בסוף).

הרצה (דורש GEMINI_API_KEY* ב-environment):
    py diagnose_multipart.py
"""

import io
import json
import os
import time
from collections import Counter

from pypdf import PdfReader, PdfWriter

import extract_subsidiaries as ex
import maya_reports_client as mrc

# מדגם קבוע ומתועד - לא רנדומלי, כדי שאפשר יהיה לחזור על הבדיקה ולהשוות.
SAMPLE_COMPANY_IDS = [
    "604",   # לאומי - המקרה שעורר את הבדיקה
    "662",   # פועלים
    "593",   # בינלאומי
    "691",   # דיסקונט
    "695",   # מזרחי טפחות
    "127",   # מבטח שמיר - ביטוח
    "1172",  # אפי נכסים
    "1654",  # סטרוברי
    "1630",  # לייטסטון
    "387",   # אלרוב נדל"ן
    "1628",  # ספנסר אקוויטי
    "182",   # אדגר השקעות
    "251",   # אשטרום נכסים
    "323",   # מליסרון
    "1357",  # ריט 1
    "1349",  # רבוע כחול נדל"ן
    "1773",  # ישראכרט
    "1132",  # חלל תקשורת
    "1327",  # ביג
    "1006",  # גילת טלקום
]

OUT_PATH = "diagnose_results.json"

FORMAL_TABLE_MARKERS = ["תקנה 11", "List of Subsidiaries", "Organizational Structure"]
FOCUSED_RECHECK_THRESHOLD = 3
CROP_PAGES_BEFORE = 40
CROP_PAGES_AFTER = 60


def normalize_result(result) -> dict:
    if isinstance(result, list):
        return {"subsidiaries": result, "change_events": []}
    return result or {"subsidiaries": [], "change_events": []}


def call_with_retry(pdf_bytes: bytes, filename_hint: str, quota_retries: int = 4):
    last_quota_error = None
    for attempt in range(quota_retries):
        try:
            raw = ex.call_gemini_extraction(pdf_bytes, filename_hint=filename_hint, max_retries=6)
            return normalize_result(raw), None
        except ex.GeminiQuotaExceededError as e:
            last_quota_error = e
            print(f"    כל המפתחות בקירור (ניסיון {attempt + 1}/{quota_retries}) - "
                  f"ממתין {ex.GeminiKeyPool.COOLDOWN_SECONDS}s לפני ניסיון נוסף...")
            time.sleep(ex.GeminiKeyPool.COOLDOWN_SECONDS)
        except Exception as e:
            return None, f"חילוץ נכשל: {e}"
    return None, f"מכסה נגמרה גם אחרי {quota_retries} ניסיונות: {last_quota_error}"


def extract_single_file(url: str, label: str, quota_retries: int = 4):
    out = {"url": url, "label": label}
    try:
        pdf_bytes = mrc.download_report_file(url)
    except Exception as e:
        out["error"] = f"הורדה נכשלה: {e}"
        return out, None

    out["pdf_size_kb"] = len(pdf_bytes) // 1024

    result, err = call_with_retry(pdf_bytes, url, quota_retries)
    if err:
        out["error"] = err
        return out, pdf_bytes

    out["subsidiaries"] = result.get("subsidiaries", [])
    out["change_events"] = result.get("change_events", [])
    out["document_type"] = result.get("document_type")
    return out, pdf_bytes


def is_formal_table(sub: dict) -> bool:
    for field in (sub.get("section_title"), sub.get("table_title")):
        if field and any(m in field for m in FORMAL_TABLE_MARKERS):
            return True
    return False


def find_focused_recheck_candidate(subsidiaries):
    formal = [s for s in subsidiaries if is_formal_table(s) and s.get("page_reference")]
    if not formal:
        return None
    page_counts = Counter(s["page_reference"] for s in formal)
    page, count = min(page_counts.items(), key=lambda x: x[1])
    return page if count <= FOCUSED_RECHECK_THRESHOLD else None


def crop_pdf_pages(pdf_bytes: bytes, center_page: int):
    try:
        reader = PdfReader(io.BytesIO(pdf_bytes))
    except Exception as e:
        print(f"    שגיאת קריאת PDF לגזירה: {e}")
        return None

    n_pages = len(reader.pages)
    idx = center_page - 1
    if idx < 0 or idx >= n_pages:
        print(f"    center_page={center_page} מחוץ לטווח (המסמך {n_pages} עמודים בפועל) - מדלג על גזירה.")
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


def diagnose_company(cid: str, entry: dict) -> dict:
    company_name = entry.get("company_name")
    snap = entry.get("snapshot")
    if not snap:
        return {"company_name": company_name, "error": "אין snapshot בתוכנית"}

    all_urls = [(snap["pdf_url"], "ראשי")] + [
        (u, f"נוסף {i+1}") for i, u in enumerate(snap.get("extra_pdfs", []))
    ]

    print(f"\n=== {company_name} (companyId={cid}) - {len(all_urls)} קבצים ===")
    files_results = []
    for url, label in all_urls:
        print(f"  מעבד {label}: {url}")
        file_result, pdf_bytes = extract_single_file(url, label)
        n_subs = len(file_result.get("subsidiaries", []))
        if "error" in file_result:
            print(f"    שגיאה: {file_result['error']}")
        else:
            print(f"    {n_subs} חברות בת נמצאו בקובץ הזה")

        if pdf_bytes is not None and "error" not in file_result:
            candidate_page = find_focused_recheck_candidate(file_result["subsidiaries"])
            if candidate_page:
                print(f"    חשד: טבלה פורמלית בעמוד {candidate_page} עם "
                      f"{FOCUSED_RECHECK_THRESHOLD} רשומות או פחות - מריץ בדיקה ממוקדת...")
                cropped = crop_pdf_pages(pdf_bytes, candidate_page)
                if cropped:
                    focused_result, focused_err = call_with_retry(
                        cropped, f"{url}#focused_page_{candidate_page}"
                    )
                    focused_names = (
                        sorted({s.get("name") for s in focused_result.get("subsidiaries", [])
                                if s.get("name")})
                        if focused_result else []
                    )
                    file_result["focused_recheck"] = {
                        "center_page": candidate_page,
                        "cropped_pages": f"{max(1, candidate_page - CROP_PAGES_BEFORE)}-"
                                         f"{candidate_page + CROP_PAGES_AFTER}",
                        "cropped_pdf_size_kb": len(cropped) // 1024,
                        "error": focused_err,
                        "n_subsidiaries_found": len(focused_names),
                        "subsidiary_names": focused_names,
                    }
                    if focused_names:
                        print(f"    בדיקה ממוקדת: {len(focused_names)} חברות בת "
                              f"(מול {n_subs} במסמך המלא)")
                    time.sleep(2)

        files_results.append(file_result)
        time.sleep(2)

    current_pipeline_names = set()
    for fr in files_results:
        if "error" in fr:
            continue
        names = {s.get("name") for s in fr.get("subsidiaries", []) if s.get("name")}
        if names:
            current_pipeline_names = names
            break

    all_names = set()
    for fr in files_results:
        all_names |= {s.get("name") for s in fr.get("subsidiaries", []) if s.get("name")}

    missed_names = sorted(all_names - current_pipeline_names)

    return {
        "company_name": company_name,
        "n_files": len(all_urls),
        "files": files_results,
        "current_pipeline_would_find": len(current_pipeline_names),
        "total_across_all_files": len(all_names),
        "missed_by_current_pipeline": missed_names,
    }


def load_existing() -> dict:
    if os.path.exists(OUT_PATH):
        with open(OUT_PATH, encoding="utf-8") as f:
            return json.load(f)
    return {}


def save(results: dict) -> None:
    tmp = OUT_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    os.replace(tmp, OUT_PATH)


def main():
    with open("selection_plan.json", encoding="utf-8") as f:
        plan = json.load(f)

    results = load_existing()
    print(f"נטענו {len(results)} תוצאות קיימות (יידלגו).")

    for cid in SAMPLE_COMPANY_IDS:
        if cid in results:
            print(f"companyId {cid} כבר נבדק - מדלג.")
            continue
        entry = plan.get(cid)
        if entry is None:
            print(f"companyId {cid} לא נמצא ב-selection_plan.json - מדלג.")
            continue
        try:
            results[cid] = diagnose_company(cid, entry)
        except ex.GeminiQuotaExceededError:
            print(f"מכסת Gemini נגמרה - עוצר. {len(results)}/{len(SAMPLE_COMPANY_IDS)} הושלמו.")
            save(results)
            return
        save(results)

    print(f"\n=== סיכום ===")
    for cid, r in results.items():
        if "error" in r:
            continue
        n_missed = len(r.get("missed_by_current_pipeline", []))
        flag = " <<<< יש הבדל!" if n_missed else ""
        print(f"{cid} {r['company_name']}: פייפליין נוכחי={r['current_pipeline_would_find']}, "
              f"סה\"כ בפועל={r['total_across_all_files']}, הוחמצו={n_missed}{flag}")

    print(f"\nנשמר ל-{OUT_PATH}")


if __name__ == "__main__":
    main()

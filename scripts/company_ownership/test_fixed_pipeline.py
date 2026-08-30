"""
test_fixed_pipeline.py

בדיקת A/B: מריץ את הקוד המתוקן בפועל (run_small_batch.process_company -
לא שכפול לוגיקה) על אותן 20 החברות מ-diagnose_multipart.py, ומשווה את
מספר החברות שנמצא מול הבדיקה הקודמת (diagnose_results.json, "בפועל -
כל הקבצים").

בטיחות: ex.save_extraction_json מנותב (monkeypatch) לפונקציה שרק
שומרת ל-ab_test_results.json - לא נוגעים ב-private_subsidiaries.jsonl
או control.json בכלל. זו בדיקה, לא ריצה אמיתית.

הרצה (דורש GEMINI_API_KEY* ב-environment):
    py test_fixed_pipeline.py
"""

import json
import os
import time

import extract_subsidiaries as ex
import run_small_batch as rsb

# אותו מדגם בדיוק כמו ב-diagnose_multipart.py - כדי שההשוואה תהיה הוגנת.
# הערה: 604/691/1172/387 כבר נבדקו בהצלחה בסבב הקטן (4 חברות) אחרי
# ביטול responseSchema - הריצה תדלג עליהן (כבר ב-ab_test_results.json)
# ותשלים רק את שאר ה-16.
SAMPLE_COMPANY_IDS = [
    "604",   # לאומי
    "691",   # דיסקונט
    "1172",  # אפי נכסים
    "387",   # אלרוב נדל"ן
]

OUT_PATH = "ab_test_results_36flash.json"
OLD_DIAGNOSE_PATH = "diagnose_results.json"

_captured = {}


def _fake_save_extraction_json(company_legal_id, report_id, source_type,
                                extracted, report_publish_date=None, out_path=None):
    """מחליף את ex.save_extraction_json האמיתי - לא כותב שום דבר
    ל-private_subsidiaries.jsonl, רק לוכד את התוצאה בזיכרון לצורך
    ההשוואה. אם משהו קורא לפונקציה הזו יותר מפעם אחת לאותה חברה
    (לא אמור לקרות - report_id אחד לכל snapshot), רק האחרון נשמר."""
    _captured[company_legal_id] = {
        "report_id": report_id,
        "source_type": source_type,
        "n_subsidiaries": len(extracted.get("subsidiaries", [])),
        "n_change_events": len(extracted.get("change_events", [])),
        "subsidiary_names": sorted({
            s.get("name") for s in extracted.get("subsidiaries", []) if s.get("name")
        }),
    }


# מחליפים את ההפניה בשני המקומות - גם במודול extract_subsidiaries עצמו
# וגם בהפניה שכבר יובאה ל-run_small_batch (זהירות: זה אותו אובייקט
# מודול ב-sys.modules, אבל run_small_batch קורא ל-ex.save_extraction_json
# דרך ה-namespace שלו - מחליפים גם שם ליתר ביטחון).
ex.save_extraction_json = _fake_save_extraction_json
rsb.ex.save_extraction_json = _fake_save_extraction_json


def load_json(path: str) -> dict:
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
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

    old_diagnose = load_json(OLD_DIAGNOSE_PATH)
    results = load_json(OUT_PATH)
    print(f"נטענו {len(results)} תוצאות בדיקה קיימות (יידלגו).")

    for cid in SAMPLE_COMPANY_IDS:
        if cid in results:
            print(f"companyId {cid} כבר נבדק - מדלג.")
            continue
        entry = plan.get(cid)
        if entry is None:
            print(f"companyId {cid} לא נמצא ב-selection_plan.json - מדלג.")
            continue

        _captured.clear()
        try:
            ok = rsb.process_company(cid, entry)
        except ex.GeminiQuotaExceededError:
            print(f"מכסת Gemini נגמרה - עוצר. {len(results)}/{len(SAMPLE_COMPANY_IDS)} הושלמו.")
            save(results)
            return

        results[cid] = _captured.get(cid, {"error": "לא נלכד ex.save_extraction_json - process_company החזיר False?", "process_company_ok": ok})
        save(results)
        time.sleep(3)

    print(f"\n=== השוואה: לפני התיקון (diagnose_results.json) מול אחרי (הפייפליין המתוקן) ===")
    for cid, r in results.items():
        name = plan.get(cid, {}).get("company_name", cid)
        old_r = old_diagnose.get(cid, {})
        old_total = old_r.get("total_across_all_files")
        old_current = old_r.get("current_pipeline_would_find")
        new_total = r.get("n_subsidiaries")
        if "error" in r:
            print(f"{cid} {name}: שגיאה - {r['error']}")
            continue
        flag = ""
        if old_total is not None and new_total is not None:
            if new_total >= old_total:
                flag = " <<<< תיקון הצליח (מגיע ל'סה\"כ בפועל' הישן או מעבר לו)"
            elif new_total > (old_current or 0):
                flag = " <<<< שיפור חלקי (יותר מ'פייפליין ישן', פחות מ'סה\"כ בפועל')"
            else:
                flag = " <<<< אין שיפור - צריך בדיקה נוספת"
        print(f"{cid} {name}: פייפליין ישן (עצר ראשון)={old_current}, "
              f"סה\"כ בפועל (אבחון)={old_total}, פייפליין מתוקן={new_total}{flag}")

    print(f"\nנשמר ל-{OUT_PATH}")


if __name__ == "__main__":
    main()

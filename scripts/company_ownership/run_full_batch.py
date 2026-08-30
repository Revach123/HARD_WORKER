"""
run_full_batch.py

הרצה מלאה עם מעקב אידמפוטנטי ועיבוד מקבילי: עובר על כל selection_plan.json
(או חלקו עם --limit), מדלג על report_id שכבר עובד, ועוצר בעדינות כשמזוהה
מכסה יומית אמיתית (GeminiQuotaExceededError).

עיבוד מקבילי (--workers): כמה משימות בו-זמנית (הורדה + שליחה ל-Gemini),
כדי לחסוך בזמן ריצה כולל. ה-RPM האמיתי מול Gemini מוגן בכל מקרה על ידי
rate limiter גלובלי ב-extract_subsidiaries.py, משותף בין כל ה-threads -
concurrency לא "עוקף" את המכסה, רק מנצל טוב יותר את זמן ההמתנה
(הורדות, I/O) בין הבקשות המוגבלות-קצב.

מיועד לשימוש גם בהרצה היומית העתידית - processed_reports.json הוא קובץ
מתמשך בין הרצות (נשמר ב-cache ב-GitHub Actions, כמו .maya_cache).

הרצה:
    py run_full_batch.py --plan selection_plan.json
    py run_full_batch.py --plan selection_plan.json --limit 50 --workers 3
"""

import argparse
import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

import extract_subsidiaries as ex
import run_small_batch as rsb

PROCESSED_LOG_PATH = "processed_reports.json"

_processed_lock = threading.Lock()
_print_lock = threading.Lock()


def safe_print(*args, **kwargs) -> None:
    """print רגיל, רק נעול - כדי שפלט מ-threads שונים לא יתערבב שורה בתוך שורה."""
    with _print_lock:
        print(*args, **kwargs)


MAX_RETRY_ATTEMPTS = 3  # כמה פעמים לנסות שוב דוח שנכשל, לאורך הרצות שונות


def load_processed(path: str) -> dict:
    """{report_id: {"status": "success"|"failed", "company_id":..., "kind":...,
    "attempts": int, "at":...}}"""
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def save_processed(path: str, processed: dict) -> None:
    """כתיבה אטומית - קודם לקובץ זמני, ואז החלפה (os.replace הוא אטומי
    ב-POSIX). כך processed_reports.json עצמו לעולם לא נשאר בפורמט
    חצי-כתוב גם אם התהליך נהרג באמצע השמירה."""
    tmp_path = path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(processed, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp_path, path)


def mark(processed: dict, path: str, report_id: str, company_id: str, status: str, kind: str) -> None:
    """מסמן ושומר מיד לדיסק, thread-safe - קריאה-שינוי-כתיבה מוגנת בלוק
    כדי שכמה threads שמסיימים כמעט יחד לא ידרסו זה את עדכוני זה.
    status="success" הוא סופי (לא ינוסה שוב לעולם). status="failed"
    סופר ניסיונות - יינתן retry בהרצות הבאות עד MAX_RETRY_ATTEMPTS,
    ורק אז ייחשב סופי (כדי לא לבזבז מכסה לנצח על דוח שבאמת שבור)."""
    with _processed_lock:
        prev_attempts = processed.get(report_id, {}).get("attempts", 0)
        processed[report_id] = {
            "company_id": company_id,
            "status": status,
            "kind": kind,
            "attempts": prev_attempts + 1,
            "at": datetime.now(timezone.utc).isoformat(),
        }
        save_processed(path, processed)


def run_task(task) -> tuple:
    """מריץ משימה בודדת (בתוך thread). מחזיר (report_id, cid, status_or_None,
    quota_exceeded: bool, kind). status_or_None הוא None אם quota_exceeded=True -
    לא מסמנים כישלון במקרה הזה, כי זה לא כישלון של המשימה עצמה.

    תופס כל חריגה, לא רק GeminiQuotaExceededError - קרה בפועל (Gemini
    שהחזיר מערך גולמי במקום dict) שחריגה בלתי-צפויה הפילה את כל התהליך
    (future.result() ללא try/except בלולאה הראשית זורק אותה הלאה). עדיף
    לסמן משימה בודדת כ-failed (תנוסה שוב בהרצה הבאה, יש לנו כבר מנגנון
    retry) מאשר לאבד שעות של עבודה על שאר המשימות בגלל אחת."""
    kind, cid, company_name, report_id, payload = task
    try:
        if kind == "snapshot":
            ok = rsb.process_company(cid, payload)
        else:
            ok = rsb.process_change_report(cid, company_name, payload)
        return (report_id, cid, "success" if ok else "failed", False, kind)
    except ex.GeminiQuotaExceededError:
        return (report_id, cid, None, True, kind)
    except Exception as e:
        safe_print(f"  שגיאה בלתי-צפויה ב-{company_name} (דוח {report_id}): "
                   f"{type(e).__name__}: {e} - מסומן כ-failed, ינוסה שוב.")
        return (report_id, cid, "failed", False, kind)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--plan", default="selection_plan.json")
    parser.add_argument("--processed-log", default=PROCESSED_LOG_PATH)
    parser.add_argument("--results", default="private_subsidiaries.jsonl",
                         help="קובץ התוצאות - נטען כדי לבדוק גרסת סכימה לכל רשומה")
    parser.add_argument("--limit", type=int, default=None,
                         help="הגבלת מספר משימות (לבדיקה) - ברירת מחדל: הכל")
    parser.add_argument("--workers", type=int, default=3,
                         help="כמה משימות בו-זמנית (הורדה+Gemini). ה-RPM מוגן "
                              "בנפרד ע\"י rate limiter גלובלי, לא תלוי במספר הזה.")
    args = parser.parse_args()

    with open(args.plan, encoding="utf-8") as f:
        plan = json.load(f)

    processed = load_processed(args.processed_log)
    print(f"נטען processed_reports: {len(processed)} דוחות כבר טופלו בעבר.\n")

    # אינדקס report_id -> schema_version, מהתוצאות בפועל (לא מ-processed_reports
    # - אלה שני קבצים נפרדים). רשומה שנשמרה לפני שהוספנו schema_version
    # (או לפני שדה מסוים) פשוט תיחשב גרסה 1 - נמוכה מהעדכנית, ותעובד מחדש
    # אוטומטית. זה מחליף את --force-reprocess-changes הידני הישן: לא צריך
    # לזכור לכבות דגל, וזה עובד נכון גם בהרצות schedule (בלי קלט ידני).
    schema_by_report: dict[str, int] = {}
    if os.path.exists(args.results):
        with open(args.results, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                rid = rec.get("report_id")
                if rid is None:
                    continue
                ver = rec.get("schema_version", 1)
                schema_by_report[rid] = max(ver, schema_by_report.get(rid, 0))

    n_stale_schema = sum(
        1 for v in schema_by_report.values() if v < ex.CURRENT_SCHEMA_VERSION
    )
    if n_stale_schema:
        print(f"{n_stale_schema} רשומות קיימות בסכימה ישנה (< גרסה "
              f"{ex.CURRENT_SCHEMA_VERSION}) - יעובדו מחדש אוטומטית.\n")

    def _needs_processing(report_id: str, kind: str) -> bool:
        """True אם הדוח צריך עיבוד. שלוש סיבות אפשריות: (1) עדיין לא
        הצליח מעולם / נכשל ועדיין בתוך תקציב הניסיונות החוזרים, או
        (2) הצליח בסכימה ישנה יותר מהעדכנית - יעובד מחדש אוטומטית עד
        שיתעדכן.

        הערה על 2->3 (2026-08-30): בעליות גרסה קודמות (1->2) הבדיקה
        הוגבלה ל-kind=="change" בלבד, כי השינוי (event_type,
        ownership_pct_before) היה רלוונטי רק לשם - הרחבה ל-snapshot
        אז הייתה גורמת לכל snapshot קיים (גם תקין לגמרי) להיחשב
        \"ישן\" בטעות ולהתעבד מחדש לשווא (זה בדיוק מה שקרה בפועל).
        הפעם זה הפוך: 2->3 הוא בדיוק תיקון להתנהגות חילוץ ה-snapshot
        עצמו (עצירה-בהצלחה-ראשונה בין קבצים + בדיקה ממוקדת בתוך-קובץ) -
        אז הבדיקה חלה גם על snapshot בכוונה, לא רק change."""
        if schema_by_report.get(report_id, ex.CURRENT_SCHEMA_VERSION) < ex.CURRENT_SCHEMA_VERSION:
            return True
        entry = processed.get(report_id)
        if entry is None:
            return True
        if entry.get("status") == "success":
            return False
        return entry.get("attempts", 0) < MAX_RETRY_ATTEMPTS

    # רשימת משימות שטוחה: כל דוח (snapshot או change) הוא משימה נפרדת עם
    # report_id ייחודי משלו - אידמפוטנטיות ברמת הדוח הבודד. "failed" אינו
    # סופי - מקבל ניסיונות חוזרים (ראה _needs_processing), רק "success"
    # בסכימה עדכנית חוסם לצמיתות.
    tasks = []
    n_retrying = 0
    for cid, entry in plan.items():
        snap = entry.get("snapshot")
        if snap and _needs_processing(snap["report_id"], "snapshot"):
            if snap["report_id"] in processed:
                n_retrying += 1
            tasks.append(("snapshot", cid, entry.get("company_name"), snap["report_id"], entry))
        for change in entry.get("changes", []):
            if _needs_processing(change["report_id"], "change"):
                if change["report_id"] in processed:
                    n_retrying += 1
                tasks.append(("change", cid, entry.get("company_name"), change["report_id"], change))


    if n_retrying:
        print(f"מתוכם {n_retrying} הן ניסיונות חוזרים לדוחות שנכשלו קודם "
              f"(עד {MAX_RETRY_ATTEMPTS} ניסיונות לכל דוח).")

    total_snapshot = sum(1 for t in tasks if t[0] == "snapshot")
    total_change = sum(1 for t in tasks if t[0] == "change")
    print(f"סה\"כ בתוכנית: {len(plan)} חברות | "
          f"משימות סיכום ממתינות: {total_snapshot} | משימות עדכון ממתינות: {total_change}")

    if args.limit:
        tasks = tasks[: args.limit]
        print(f"(מוגבל ל-{args.limit} משימות לצורך הרצה זו)")

    print(f"מריץ עם {args.workers} workers מקביליים (RPM מוגן ע\"י rate limiter גלובלי).\n")

    n_ok, n_fail, n_quota_hits = 0, 0, 0
    quota_exceeded_flag = threading.Event()

    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        future_to_task = {}
        for task in tasks:
            if quota_exceeded_flag.is_set():
                break
            future = executor.submit(run_task, task)
            future_to_task[future] = task

        for future in as_completed(future_to_task):
            kind, cid, company_name, _, _ = future_to_task[future]
            try:
                report_id, cid_result, status, hit_quota, task_kind = future.result()
            except Exception as e:
                # רשת ביטחון אחרונה - לא אמור לקרות (run_task כבר תופס הכל),
                # אבל אם כן: לא מפילים את כל הריצה בגלל משימה אחת.
                safe_print(f"  שגיאה בלתי-צפויה בקבלת תוצאה עבור {company_name}: "
                           f"{type(e).__name__}: {e} - מדלג.")
                n_fail += 1
                continue

            if hit_quota:
                n_quota_hits += 1
                if not quota_exceeded_flag.is_set():
                    safe_print(f"\n*** מכסה יומית של Gemini נגמרה (זוהה ב-thread). "
                               f"לא שולחים משימות חדשות - ממתינים לסיום הפעילות "
                               f"הנוכחית. ***")
                quota_exceeded_flag.set()
                continue

            mark(processed, args.processed_log, report_id, cid_result, status, task_kind)
            if status == "success":
                n_ok += 1
            else:
                n_fail += 1
            safe_print(f"[{n_ok + n_fail}/{len(tasks)}] הושלם: {company_name} "
                       f"({kind}) -> {status}")

    print(f"\n=== סיכום הרצה זו ===")
    print(f"הצליחו: {n_ok} | נכשלו בהרצה זו (ינוסו שוב עד {MAX_RETRY_ATTEMPTS} "
          f"פעמים, אלא אם מוצו): {n_fail}")
    if quota_exceeded_flag.is_set():
        n_not_submitted = len(tasks) - n_ok - n_fail
        print(f"נעצר עקב מכסה - כ-{n_not_submitted} משימות נותרו להרצה הבאה.")
    print(f"סה\"כ processed_reports מצטבר: {len(processed)}")
    print(f"תוצאות ב-private_subsidiaries.jsonl, מעקב ב-{args.processed_log}")

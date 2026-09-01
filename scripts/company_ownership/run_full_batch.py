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
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

import extract_subsidiaries as ex
import run_small_batch as rsb

CHECKPOINT_EVERY = 10  # commit+push תוך כדי ריצה אחרי כל כך הרבה משימות -
# לא רק בסוף. נלמד בדרך הקשה: ריצה של 59 דקות איבדה את כל ההתקדמות
# שלה כי ה-commit היחיד (בסוף בלבד) התנגש עם push מקביל (תיקון קוד
# ידני תוך כדי הריצה) ונכשל בשקט - ה-workflow דיווח "success" למרות
# שההתקדמות מעולם לא הגיעה ל-git.


def _merge_processed_dicts(local: dict, remote: dict) -> dict:
    """איחוד ברמת הנתונים (לא ברמת טקסט) בין הגרסה המקומית לגרסה
    שכבר יושבת ב-origin/main. זה הפתרון האמיתי לקונפליקט תוכן שראינו
    בפועל ב-processed_reports_*.json: git עושה diff טקסטואלי על קובץ
    JSON, וכששני צדדים כותבים dict מחדש (סדר מפתחות/עיצוב שונה) הוא
    מתנגש גם כשהנתונים בפועל לא סותרים. פותרים את זה בפייתון: מתחילים
    מ-remote כבסיס, ולכל report_id מקומי - success מנצח כל דבר אחר,
    ואם שני הצדדים לא-success, מנצח מי שיש לו יותר attempts (מתקדם יותר).
    """
    merged = dict(remote)
    for rid, entry in local.items():
        other = merged.get(rid)
        if other is None or other.get("status") != "success":
            if entry.get("status") == "success" or entry.get("attempts", 0) >= (other or {}).get("attempts", 0):
                merged[rid] = entry
    return merged


def _fetch_remote_json(path: str) -> dict:
    """טוען את הגרסה הנוכחית של קובץ JSON מ-origin/main בלי לגעת ב-
    working tree (git show, לא checkout) - {} אם הקובץ לא קיים שם עדיין
    או שה-fetch לא הביא אותו."""
    result = subprocess.run(
        ["git", "show", f"origin/main:./{path}"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        return {}
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return {}


def _commit_progress(results_path: str, processed_log_path: str, processed: dict, reason: str = "") -> bool:
    """Commit+push התקדמות חלקית תוך כדי ריצה, מה-thread הראשי בלבד
    (לא בטוח לקרוא מכמה threads בו-זמנית - כל הקריאות לפונקציה הזו
    קורות מתוך לולאת as_completed הראשית, לא מתוך worker threads).
    מחזיר True אם ה-push הצליח (או שלא היה מה לחיוב), False אם נכשל
    אחרי כל הניסיונות - ההתקדמות עדיין קיימת מקומית על הדיסק, רק לא
    הגיעה ל-git. מעדכן את processed במקום (dict מועבר by reference) עם
    המיזוג מול origin/main, כך שהלולאה הראשית ממשיכה מהגרסה המאוחדת."""
    try:
        subprocess.run(["git", "config", "user.name", "github-actions[bot]"], check=True)
        subprocess.run(["git", "config", "user.email",
                         "github-actions[bot]@users.noreply.github.com"], check=True)

        # מיזוג ברמת-נתונים לפני ה-commit - ראה _merge_processed_dicts.
        # זה מה שמונע את קונפליקט התוכן שראינו בפועל, לא רק את בעיית
        # ה-ref-race (שהלולאה למטה כבר מטפלת בה).
        subprocess.run(["git", "fetch", "origin", "main"], check=True)
        remote_processed = _fetch_remote_json(processed_log_path)
        if remote_processed:
            merged = _merge_processed_dicts(processed, remote_processed)
            if merged != processed:
                processed.clear()
                processed.update(merged)
            save_processed(processed_log_path, processed)

        subprocess.run(["git", "add", results_path, processed_log_path], check=True)
        diff = subprocess.run(["git", "diff", "--staged", "--quiet"])
        if diff.returncode == 0:
            return True  # אין שינויים לחיוב - לא באמת כישלון

        subprocess.run(["git", "commit", "-m", f"Progress checkpoint {reason} [skip ci]"], check=True)
        for attempt in range(5):
            if subprocess.run(["git", "push"]).returncode == 0:
                return True
            safe_print(f"    checkpoint push נדחה (ניסיון {attempt + 1}/5) - מושך ועושה rebase...")
            # מבטלים rebase תקוע מניסיון קודם לפני שמתחילים אחד חדש - בלי
            # זה, ניסיון rebase נוסף על גבי rebase לא-גמור (למשל בגלל
            # קונפליקט) מייצר שגיאות מבלבלות כמו "Cannot rebase onto
            # multiple branches" (בדיוק מה שקרה לנו בשלב ה-YAML הסופי).
            subprocess.run(["git", "rebase", "--abort"], stderr=subprocess.DEVNULL)

            # קריטי: worker threads אחרים ממשיכים לכתוב שורות חדשות ל-
            # results_path (private_subsidiaries.jsonl) תוך כדי שאנחנו כאן -
            # ה-fetch/rebase לוקחים זמן אמיתי (רשת, retries), וזה בדיוק
            # החלון שבו thread אחר מסיים משימה ומוסיף שורה. baw שנצפה
            # בפועל: "error: cannot rebase: You have unstaged changes" -
            # git מסרב לעשות rebase כשיש שינויים לא-מחויבים בעץ העבודה.
            # פותרים עם stash זמני: שומרים את מה שנכתב ברקע בצד, עושים
            # rebase על עץ נקי, ומחזירים את זה בסוף כדי שייכנס ל-checkpoint
            # הבא (לא אובד - רק נדחה).
            stash = subprocess.run(
                ["git", "stash", "push", "--include-untracked", "-m", "checkpoint-inflight"],
                capture_output=True, text=True,
            )
            stashed = "No local changes" not in stash.stdout and stash.returncode == 0

            subprocess.run(["git", "fetch", "origin", "main"], check=True)
            rebase_ok = subprocess.run(["git", "rebase", "origin/main"]).returncode == 0
            if not rebase_ok:
                safe_print(f"    rebase נכשל (ניסיון {attempt + 1}) - מבטל ומנסה שוב בסיבוב הבא")
                subprocess.run(["git", "rebase", "--abort"], stderr=subprocess.DEVNULL)

            if stashed:
                pop = subprocess.run(["git", "stash", "pop"])
                if pop.returncode != 0:
                    safe_print("    אזהרה: git stash pop התנגש - התוכן שנכתב ברקע "
                               "נשאר ב-stash (git stash list) ולא אבד, אבל דורש טיפול ידני.")
        safe_print("    checkpoint נכשל אחרי 5 ניסיונות - ההתקדמות עדיין על הדיסק המקומי בלבד.")
        return False
    except subprocess.CalledProcessError as e:
        safe_print(f"    שגיאת git ב-checkpoint: {e}")
        return False

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
    #
    # אותו מעבר גם בונה best_known_subs (companyId -> מקסימום חברות בת
    # שנמצא אי-פעם, מכל גרסה) - לשימוש בחישוב סדר עדיפויות למטה: חברה
    # עם 0 ידועות כרגע חשודה יותר לפספוס אמיתי מחברה שכבר הראתה הרבה.
    schema_by_report: dict[str, int] = {}
    best_known_subs: dict[str, int] = {}
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
                cid_seen = rec.get("parent_hp")
                if cid_seen is not None:
                    n_subs = len(rec.get("subsidiaries", []))
                    best_known_subs[cid_seen] = max(n_subs, best_known_subs.get(cid_seen, 0))

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

    def _snapshot_priority(cid: str, entry: dict) -> tuple:
        """סדר עדיפות לעיבוד-מחדש: מי שהכי צפוי להיות עם נתונים חסרים
        קודם. שלושה גורמים, בסדר יורד של משקל:
        (1) extra_pdfs - הבאג שאושר בפועל (עוצר על הראשון שמצליח);
            חברות כאלה כמעט בוודאות פספסו נתונים.
        (2) 0 חברות בת ידועות כרגע - חשוד: או שאין באמת (חברה קטנה),
            או שהחילוץ פספס הכל. גודל הקובץ (הגורם הבא) מבדיל ביניהם.
        (3) גודל ה-PDF הראשי - מסמך גדול יותר = סיכוי גבוה יותר לדילול
            תשומת הלב (ראה אלרוב: 19 מול 68 בקטע ממוקד).
        מוחזר כ-tuple להשוואה ישירה (True/False ממוין נכון כ-1/0)."""
        snap = entry.get("snapshot") or {}
        has_extra = bool(snap.get("extra_pdfs"))
        n_known = best_known_subs.get(cid, 0)
        size_kb = snap.get("pdf_size_kb", 0)
        return (has_extra, n_known == 0, size_kb)

    # רשימת משימות שטוחה: כל דוח (snapshot או change) הוא משימה נפרדת עם
    # report_id ייחודי משלו - אידמפוטנטיות ברמת הדוח הבודד. "failed" אינו
    # סופי - מקבל ניסיונות חוזרים (ראה _needs_processing), רק "success"
    # בסכימה עדכנית חוסם לצמיתות.
    #
    # משימות snapshot ממוינות לפי _snapshot_priority (יורד) - עם מכסה
    # יומית זעירה (20 RPD/מפתח על gemini-3.6-flash), הסדר קובע מי בכלל
    # מגיע לעיבוד היום. משימות change נשארות בסדר הטבעי בסוף - נפח קטן
    # וערך נמוך יותר יחסית למטרה (השלמת עץ ההחזקות הפרטיות).
    snapshot_tasks = []
    change_tasks = []
    n_retrying = 0
    for cid, entry in plan.items():
        snap = entry.get("snapshot")
        if snap and _needs_processing(snap["report_id"], "snapshot"):
            if snap["report_id"] in processed:
                n_retrying += 1
            snapshot_tasks.append(
                (_snapshot_priority(cid, entry), "snapshot", cid, entry.get("company_name"), snap["report_id"], entry)
            )
        for change in entry.get("changes", []):
            if _needs_processing(change["report_id"], "change"):
                if change["report_id"] in processed:
                    n_retrying += 1
                change_tasks.append(("change", cid, entry.get("company_name"), change["report_id"], change))

    snapshot_tasks.sort(key=lambda t: t[0], reverse=True)
    tasks = [t[1:] for t in snapshot_tasks] + change_tasks
    print(f"משימות snapshot ממוינות לפי עדיפות: "
          f"{sum(1 for t in snapshot_tasks if t[0][0])} עם extra_pdfs, "
          f"{sum(1 for t in snapshot_tasks if t[0][1])} עם 0 ידועות כרגע.")


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
    rsb.QUOTA_EXCEEDED = quota_exceeded_flag  # ראה run_small_batch.py -
    # אותו אובייקט Event בדיוק, לא עותק - כדי שמשימות שכבר רצות בתוך
    # thread ייתקלו באותו דגל וייכנעו מיד בלי לחכות לקירור מלא (הבאג
    # שגרם לריצה להימשך שעות אחרי שהמכסה כבר זוהתה כנגמרת).

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

            if (n_ok + n_fail) % CHECKPOINT_EVERY == 0:
                safe_print(f"  --- checkpoint: מחייב התקדמות אחרי {n_ok + n_fail} משימות ---")
                _commit_progress(args.results, args.processed_log, processed,
                                  reason=f"after {n_ok + n_fail} tasks")

    print(f"\n=== סיכום הרצה זו ===")
    print(f"הצליחו: {n_ok} | נכשלו בהרצה זו (ינוסו שוב עד {MAX_RETRY_ATTEMPTS} "
          f"פעמים, אלא אם מוצו): {n_fail}")
    if quota_exceeded_flag.is_set():
        n_not_submitted = len(tasks) - n_ok - n_fail
        print(f"נעצר עקב מכסה - כ-{n_not_submitted} משימות נותרו להרצה הבאה.")
    print(f"סה\"כ processed_reports מצטבר: {len(processed)}")
    print(f"תוצאות ב-private_subsidiaries.jsonl, מעקב ב-{args.processed_log}")

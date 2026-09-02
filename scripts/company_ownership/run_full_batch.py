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
                    # לא מספיק רק להדפיס אזהרה - קונפליקט ב-stash pop משאיר
                    # קבצים עם סימוני התנגשות לא-מחויבים בעץ העבודה, וזה
                    # בדיוק מה שתוקע כל פעולת git הבאה בתהליך (כולל שלב
                    # ה-commit הסופי ב-YAML, בהמשך אחרי שכל הריצה נגמרת) עם
                    # "cannot rebase: you have unstaged changes" - נצפה
                    # בפועל. מנקים בכוח: HEAD כבר מכיל את ה-checkpoint שכן
                    # הצליח (הקומיט למעלה), רק תוכן ה-stash (עבודה של threads
                    # אחרים תוך כדי) הולך לאיבוד - נשאר ב-git stash list
                    # לשחזור ידני, בדרך כלל שורה-שתיים בלבד.
                    safe_print("    אזהרה: git stash pop התנגש - מנקה את עץ העבודה בכוח. "
                               "התוכן נשאר ב-git stash list לשחזור ידני, לא אבד לגמרי.")
                    subprocess.run(["git", "reset", "--hard", "HEAD"], stderr=subprocess.DEVNULL)
                    subprocess.run(["git", "clean", "-fd"], stderr=subprocess.DEVNULL)
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


MAX_RETRY_ATTEMPTS = 3  # משפיע רק על *סדר* התור (דחיפה-לסוף), לא על זכאות.
# דוח שנכשל יותר מזה עדיין ינוסה - רק בעדיפות נמוכה. ראה _needs_processing.


# ══════════════════════════════════════════════════════════════════════
# D1 sync — שכבת סטטוס לדשבורד. משתמש באותו דפוס REST כמו load_companies.py:
# POST ל-api.cloudflare.com עם CF_ACCOUNT_ID + CF_D1_TOKEN + CF_SECURITIES_DB_ID.
# הכל נכשל-בשקט: אם אין הגדרות D1, הפייפליין ממשיך כרגיל (מקור האמת ב-git).
# ══════════════════════════════════════════════════════════════════════

def _d1_config() -> dict | None:
    """מחזיר את הגדרות D1 מ-env, או None אם חסר משהו (אז מדלגים על סנכרון)."""
    acc = os.environ.get("CF_ACCOUNT_ID")
    token = os.environ.get("CF_D1_TOKEN")
    db = os.environ.get("CF_SECURITIES_DB_ID")  # securities_db, לא company-info-db!
    if not (acc and token and db):
        return None
    return {
        "url": f"https://api.cloudflare.com/client/v4/accounts/{acc}/d1/database/{db}/query",
        "token": token,
    }


def _d1_query(cfg: dict, sql: str, params: list | None = None) -> dict:
    """שולח SQL בודד ל-D1 דרך REST. זורק על שגיאה."""
    import urllib.request
    import urllib.error
    body = json.dumps({"sql": sql, "params": params or []}).encode("utf-8")
    req = urllib.request.Request(
        cfg["url"], data=body, method="POST",
        headers={"Authorization": f"Bearer {cfg['token']}",
                 "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            res = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")
        raise RuntimeError(f"D1 HTTP {e.code}: {detail[:400]}") from None
    if not res.get("success"):
        raise RuntimeError(f"D1 error: {json.dumps(res.get('errors'), ensure_ascii=False)}")
    return res


def read_priority_requests() -> list:
    """נקרא בתחילת ריצה: מחזיר רשימת company_id שסומנו כדחופים בדשבורד
    (request_type='urgent' או 'retry') וטרם טופלו. הפייפליין יקדים אותם
    לראש התור. מחזיר [] אם אין D1 או אין בקשות. לא מסמן resolved כאן -
    זה קורה רק אחרי שהחברה באמת עובדה (ראה resolve_requests)."""
    cfg = _d1_config()
    if not cfg:
        return []
    try:
        res = _d1_query(cfg,
            "SELECT DISTINCT company_id FROM extraction_requests "
            "WHERE resolved_at IS NULL AND request_type IN ('urgent','retry')")
        rows = res.get("result", [{}])[0].get("results", [])
        return [str(r["company_id"]) for r in rows]
    except Exception as e:
        print(f"אזהרה: קריאת בקשות דחיפות מ-D1 נכשלה (לא קריטי): {e}")
        return []


def resolve_requests(cfg: dict, company_ids: set, request_types: tuple = ("urgent", "retry", "emergency")) -> None:
    """מסמן בקשות דחיפות כ-resolved, מוגבל לסוגי בקשה מסוימים - ראה
    הקריאות ב-_sync_status_to_d1 להסבר למה urgent/retry ו-emergency
    צריכים קריטריון פתרון שונה."""
    if not company_ids:
        return
    now = datetime.now(timezone.utc).isoformat()
    placeholders = ",".join("?" for _ in request_types)
    for cid in company_ids:
        try:
            _d1_query(cfg,
                f"UPDATE extraction_requests SET resolved_at=? "
                f"WHERE company_id=? AND resolved_at IS NULL "
                f"AND request_type IN ({placeholders})",
                [now, int(cid), *request_types])
        except Exception:
            pass  # לא קריטי


def _load_merged_processed(current_processed: dict, current_log_path: str) -> dict:
    """ממזג את שני קבצי ה-processed (3.6 ו-3.5) למבט אחד לכל report_id.
    לכל דוח בוחר את ה"טוב ביותר" שנראה באיזשהו מודל: success ב-3.6 מנצח
    success ב-3.5, ששניהם מנצחים כישלון. זה מה שמאפשר לדשבורד לדעת
    שחברה היא "מלא" (עברה 3.6) גם אם הריצה הנוכחית הייתה 3.5.

    current_processed כבר בזיכרון (הקובץ של המודל שרץ עכשיו, הכי עדכני);
    את הקובץ של המודל השני טוענים מהדיסק. אם הוא לא קיים - פשוט משתמשים
    במה שיש."""
    # שמות שני הקבצים הקבועים
    both_paths = [
        "processed_reports_gemini-3_6-flash.json",
        "processed_reports_gemini-3_5-flash-lite.json",
    ]

    def _rank(entry: dict) -> tuple:
        """דירוג לבחירת ה'טוב ביותר': (הצליח?, ב-3.6?, מספר ניסיונות).
        success מנצח כישלון; בין שתי הצלחות, 3.6 מנצח; אחרת יותר ניסיונות."""
        is_success = entry.get("status") == "success"
        model = entry.get("model", "")
        is_36 = "3.6" in model or "3_6" in model
        return (is_success, is_success and is_36, entry.get("attempts", 0))

    merged: dict = {}

    def _absorb(source: dict):
        for rid, entry in source.items():
            existing = merged.get(rid)
            if existing is None or _rank(entry) > _rank(existing):
                merged[rid] = entry

    # קודם הגרסה שבזיכרון (המודל הנוכחי, הכי טרייה)
    _absorb(current_processed)
    # ואז שני הקבצים מהדיסק (כולל המודל השני)
    for p in both_paths:
        if os.path.abspath(p) == os.path.abspath(current_log_path):
            continue  # כבר קלטנו את זה מהזיכרון (עדכני יותר)
        if os.path.exists(p):
            try:
                with open(p, encoding="utf-8") as f:
                    _absorb(json.load(f))
            except Exception as e:
                print(f"אזהרה: טעינת {p} למיזוג נכשלה (לא קריטי): {e}")
    return merged


def _load_model_success_from_jsonl(results_path: str):
    """קורא ישירות מ-private_subsidiaries.jsonl: (1) אילו מודלים הפיקו
    הצלחה לכל report_id - מקור אמת עצמאי מ-processed_reports_*.json
    (ראה תיעוד למטה); (2) המספר הגבוה ביותר שנמצא אי-פעם של חברות בת
    לכל report_id - לתצוגה בדשבורד (עמודת \"חברות בת\").

    חשוב: רשומות ישנות ב-processed_reports (לפני שהוספנו תיוג model
    ל-mark()) חסרות את השדה model לגמרי, מה שגרם ל-snap_full_36 להיות
    תמיד False (0 חברות \"מלא\" למרות הצלחות אמיתיות ב-3.6 - נצפה בפועל).
    jsonl עצמו כן מתויג נכון תמיד (save_extraction_json כותב model בכל
    שורה), אז זה מקור אמין יותר, בלי תלות בזמן שבו processed_reports
    נכתב."""
    models_by_report: dict = {}
    max_subs_by_report: dict = {}
    if not os.path.exists(results_path):
        return models_by_report, max_subs_by_report
    with open(results_path, encoding="utf-8") as f:
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
            model = rec.get("model")
            if model:
                models_by_report.setdefault(rid, set()).add(model)
            n_subs = len(rec.get("subsidiaries", []))
            max_subs_by_report[rid] = max(n_subs, max_subs_by_report.get(rid, 0))
    return models_by_report, max_subs_by_report


def _compute_company_status(cid: str, entry: dict, merged: dict, models_by_report: dict,
                             max_subs_by_report: dict) -> dict:
    """גוזר את סטטוס החברה משני מקורות: התוכנית (כמה דוחות יש) וה-
    merged (מבט ממוזג על שני קבצי processed - 3.6 ו-3.5 יחד). מחזיר
    dict מוכן ל-D1.

    קריטי: merged חייב לכלול את *שני* המודלים. חברה נחשבת "מלא" רק אם
    ה-snapshot שלה עבר ב-3.6, גם אם הריצה הנוכחית הייתה של 3.5 (או להפך).
    בלי מיזוג שני הקבצים, כל הרצה הייתה רואה חצי תמונה והסטטוס היה שגוי.

    לוגיקת הסטטוס:
      full    = ה-snapshot עבר בהצלחה ב-3.6 (האיכות הגבוהה)
      partial = יש הצלחה כלשהי (3.5, או חלק מהדוחות) אבל לא snapshot מלא ב-3.6
      pending = שום דוח לא עובד בהצלחה עדיין
      error   = כל הדוחות שנוסו נכשלו (ויש ניסיונות) - אין שום הצלחה
    """
    snap = entry.get("snapshot")
    changes = entry.get("changes", [])
    all_report_ids = ([snap["report_id"]] if snap else []) + [c["report_id"] for c in changes]
    total = len(all_report_ids)

    done = 0
    done_36 = 0
    max_att = 0
    any_success = False
    snap_full_36 = False
    snap_any = False

    for rid in all_report_ids:
        e = merged.get(rid)
        if not e:
            continue
        max_att = max(max_att, e.get("attempts", 0))
        if e.get("status") == "success":
            done += 1
            any_success = True
            models_seen = {e.get("model", "")} | models_by_report.get(rid, set())
            if any("3.6" in m or "3_6" in m for m in models_seen):
                done_36 += 1

    # סטטוס ה-snapshot ספציפית (הוא הקובע ל-full/partial)
    if snap:
        se = merged.get(snap["report_id"])
        if se and se.get("status") == "success":
            snap_any = True
            models_seen = {se.get("model", "")} | models_by_report.get(snap["report_id"], set())
            if any("3.6" in m or "3_6" in m for m in models_seen):
                snap_full_36 = True

    if snap_full_36:
        status = "full"
    elif any_success:
        status = "partial"
    elif max_att > 0:
        status = "error"
    else:
        status = "pending"

    return {
        "company_id": int(cid),
        "company_name": entry.get("company_name") or "",
        "status": status,
        "has_snapshot_36": 1 if snap_full_36 else 0,
        "has_snapshot_35": 1 if (snap_any and not snap_full_36) else 0,
        "total_reports": total,
        "done_reports": done,
        "done_reports_36": done_36,
        "max_attempts": max_att,
        "n_subsidiaries": max_subs_by_report.get(snap["report_id"], 0) if snap else 0,
    }


def _sync_status_to_d1(plan: dict, processed: dict, model: str,
                        n_ok: int, n_fail: int, stopped_quota: bool,
                        current_log_path: str, results_path: str) -> None:
    """כותב את שכבת הסטטוס המלאה ל-D1 בסוף ריצה. בונה שורה לכל חברה,
    שולח ב-batch (INSERT OR REPLACE), ומוסיף שורת extraction_runs לתצוגת
    שימוש. נכשל-בשקט אם אין D1.

    קריטי: ממזג את שני קבצי processed (3.6 + 3.5) לפני החישוב, כדי
    שהסטטוס ישקף את שני המודלים - לא רק זה שרץ בריצה הנוכחית."""
    cfg = _d1_config()
    if not cfg:
        print("אין הגדרות D1 (CF_SECURITIES_DB_ID) - מדלג על סנכרון סטטוס.")
        return

    now = datetime.now(timezone.utc).isoformat()
    # מבט ממוזג על שני המודלים - זה מה שמבטיח מידע מלא לגבי שתי הרשימות.
    merged = _load_merged_processed(processed, current_log_path)
    models_by_report, max_subs_by_report = _load_model_success_from_jsonl(results_path)
    rows = [_compute_company_status(cid, entry, merged, models_by_report, max_subs_by_report)
            for cid, entry in plan.items()]

    # INSERT OR REPLACE בקבוצות. D1/SQLite מגביל ל-100 משתנים (?) ל-statement
    # יחיד. יש לנו 10 עמודות לשורה, אז מקסימום 10 שורות לבאטש (100 משתנים).
    # 9 ליתר ביטחון (נצפה בפועל: 50 שורות = 500 משתנים = SQLITE_ERROR 7500).
    BATCH_ROWS = 9
    cols = ("company_id, company_name, status, has_snapshot_36, has_snapshot_35, "
            "total_reports, done_reports, done_reports_36, max_attempts, n_subsidiaries, last_updated")
    sent = 0
    for i in range(0, len(rows), BATCH_ROWS):
        batch = rows[i:i + BATCH_ROWS]
        placeholders = []
        params = []
        for r in batch:
            placeholders.append("(?,?,?,?,?,?,?,?,?,?,?)")
            params += [r["company_id"], r["company_name"], r["status"],
                       r["has_snapshot_36"], r["has_snapshot_35"],
                       r["total_reports"], r["done_reports"], r["done_reports_36"],
                       r["max_attempts"], r["n_subsidiaries"], now]
        sql = f"INSERT OR REPLACE INTO extraction_status ({cols}) VALUES " + ",".join(placeholders)
        try:
            _d1_query(cfg, sql, params)
            sent += len(batch)
        except Exception as e:
            print(f"אזהרה: באטש סטטוס ל-D1 נכשל: {e}")

    print(f"סונכרנו {sent}/{len(rows)} שורות סטטוס ל-D1.")

    # ── סטטיסטיקה יומית (להיסטוריית מגמה - extraction_daily_stats) ──────
    # extraction_status הוא INSERT OR REPLACE - אין בו זיכרון של אתמול.
    # שומרים שורה אחת ליום (מפתח = תאריך UTC), מעודכנת בכל ריצה מאותו
    # יום (לא מצטברת - זה המצב הסופי של היום, לא סכום ריצות).
    today = datetime.now(timezone.utc).date().isoformat()
    daily = {"full": 0, "partial": 0, "pending": 0, "error": 0}
    for r in rows:
        if r["status"] in daily:
            daily[r["status"]] += 1
    try:
        _d1_query(cfg,
            "INSERT OR REPLACE INTO extraction_daily_stats "
            "(date, full, partial, pending, error, total) VALUES (?,?,?,?,?,?)",
            [today, daily["full"], daily["partial"], daily["pending"], daily["error"],
             len(rows)])
    except Exception as e:
        print(f"אזהרה: שמירת סטטיסטיקה יומית ל-D1 נכשלה: {e}")

    # שורת לוג ריצה (לתצוגת שימוש)
    is_paid = 1 if os.environ.get("GEMINI_PAID_KEY") else 0
    try:
        _d1_query(cfg,
            "INSERT OR REPLACE INTO extraction_runs "
            "(run_at, model, reports_ok, reports_fail, stopped_quota, is_paid) "
            "VALUES (?,?,?,?,?,?)",
            [now, model, n_ok, n_fail, 1 if stopped_quota else 0, is_paid])
    except Exception as e:
        print(f"אזהרה: רישום ריצה ל-D1 נכשל: {e}")

    # סימון בקשות דחיפות שטופלו. urgent/retry - העדיפות רלוונטית רק
    # ל-3.6 (ראה בניית tasks למעלה), אז גם הפתרון תלוי רק בהצלחת 3.6
    # (status=="full") - לא בשני המודלים. שינוי מ-2026-09-02: הגרסה
    # הקודמת חיכתה ל-has_snapshot_36 וגם has_snapshot_35, אבל זה יכול
    # להיסגר מוקדם מדי אם לחברה כבר הייתה הצלחת 3.6 ישנה מלפני שהבקשה
    # בכלל נוצרה - ריצת 3.5 בלבד הייתה "סוגרת" את הבקשה בטעות.
    full_now_cids = {str(r["company_id"]) for r in rows if r["status"] == "full"}
    resolve_requests(cfg, full_now_cids, request_types=("urgent", "retry"))

    # emergency - מטבעה ריצה בודדת וממוקדת; נחשבת "טופלה" ברגע שיש הצלחה
    # כלשהי (המודל שבו רצה בפועל, כולל fallback ל-3.5 אם 3.6 נכשל - ראה
    # main()), לא ממתינה לשני המודלים.
    emergency_done_cids = {str(r["company_id"]) for r in rows if r["status"] in ("full", "partial")}
    resolve_requests(cfg, emergency_done_cids, request_types=("emergency",))


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
    status="success" סופי לגרסת הסכימה הנוכחית. status="failed" ינוסה
    שוב (אי-ויתור).

    כותב את המודל שעיבד (ex.GEMINI_MODEL) - קריטי: בלי זה הדשבורד לא
    יכול להבחין בין "מלא" (3.6) ל"חלקי" (3.5), וכל החברות היו נראות
    חלקי לנצח. מתייג רק על success (לכישלון אין תוצאת-מודל משמעותית)."""
    with _processed_lock:
        prev_attempts = processed.get(report_id, {}).get("attempts", 0)
        rec = {
            "company_id": company_id,
            "status": status,
            "kind": kind,
            "attempts": prev_attempts + 1,
            "at": datetime.now(timezone.utc).isoformat(),
        }
        if status == "success":
            rec["model"] = ex.GEMINI_MODEL
        processed[report_id] = rec
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
    parser.add_argument("--company-id", default=None,
                         help="הרץ רק על חברה אחת (companyId של מאיה) - לבדיקת "
                              "חירום מהדשבורד. מסנן את התוכנית לחברה הזו בלבד.")
    args = parser.parse_args()

    with open(args.plan, encoding="utf-8") as f:
        plan = json.load(f)

    # מצב חירום: סינון התוכנית לחברה אחת בלבד. שאר הלוגיקה זהה - החברה
    # הזו תעובד (snapshot + כל ה-changes שלה), הסטטוס יסונכרן ל-D1 כרגיל.
    if args.company_id:
        cid = str(args.company_id)
        if cid in plan:
            plan = {cid: plan[cid]}
            print(f"מצב חירום: מריץ רק על חברה {cid} ({plan[cid].get('company_name')}).")
        else:
            print(f"מצב חירום: חברה {cid} לא נמצאה בתוכנית - אין מה להריץ.")
            plan = {}

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
        """True אם הדוח צריך עיבוד. שתי סיבות: (1) עדיין לא הצליח מעולם
        (או נכשל - וכעת לעולם לא מוותרים, ראה למטה), או (2) הצליח בסכימה
        ישנה יותר מהעדכנית - יעובד מחדש אוטומטית עד שיתעדכן.

        שינוי מדיניות (אי-ויתור): בעבר דוח שנכשל MAX_RETRY_ATTEMPTS פעמים
        נחשב מת לצמיתות ולא נוסה שוב. זה גרם לאובדן קבוע של דוחות שנכשלו
        מסיבות זמניות (מכסה, עומס Gemini) - ובמיוחד, ביחד עם באג ה-quota
        שתוקן, נמחקו כך אלפי דוחות שמעולם לא נוסו באמת. עכשיו: כישלון
        לעולם לא חוסם עיבוד-חוזר. במקום זה, דוחות שנכשלו הרבה נדחפים
        לסוף התור (ראה _fail_priority) כדי לא לחסום דוחות טריים, אבל
        תמיד יקבלו הזדמנות נוספת בסופו של דבר. MAX_RETRY_ATTEMPTS עכשיו
        משפיע רק על *סדר* (עדיפות), לא על *זכאות*.

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
        # נכשל בעבר - תמיד ינוסה שוב (אי-ויתור). לא בודקים יותר תקרת ניסיונות.
        return True

    def _attempts_bucket(report_id: str) -> int:
        """כמה פעמים הדוח כבר נכשל. משמש לדחיפה-לסוף: דוחות שנכשלו הרבה
        מקבלים עדיפות נמוכה יותר (כדי לא לחסום דוחות טריים), אבל לעולם
        לא נופלים מהתור לגמרי (אי-ויתור). דוח שנכשל פחות פעמים יטופל
        לפני דוח שכבר נכשל הרבה - אבל שניהם יטופלו בסוף."""
        entry = processed.get(report_id)
        if entry is None or entry.get("status") == "success":
            return 0
        return entry.get("attempts", 0)

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
        # דחיפה-לסוף: פחות כישלונות = עדיפות גבוהה יותר. השלילי הופך
        # "מעט כישלונות" לערך גבוה במיון היורד. דוח טרי (0 כישלונות)
        # תמיד לפני דוח שכבר נכשל, אבל שניהם נשארים בתור.
        neg_attempts = -_attempts_bucket(snap.get("report_id", ""))
        return (neg_attempts, has_extra, n_known == 0, size_kb)

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
    # change: גם כאן דוחות שנכשלו הרבה נדחפים לסוף (אי-ויתור), אבל
    # נשמרים בתור. פחות כישלונות קודם. בתוך אותו מספר כישלונות - הסדר
    # הטבעי (לפי סדר הוספה) נשמר, כי sort ב-Python יציב.
    change_tasks.sort(key=lambda t: _attempts_bucket(t[3]))
    tasks = [t[1:] for t in snapshot_tasks] + change_tasks

    # ── בקשות דחיפות מהדשבורד: מקדימים חברות מסומנות לראש התור ──────────
    # tasks כאן הם tuples שבהם אינדקס 1 = company_id (אחרי הסרת עדיפות).
    # מבנה: (kind, cid, name, report_id, entry_or_change)
    #
    # רק ל-3.6 - לא ל-3.5. שינוי מכוון (2026-09-02): במקור עדיפות הוחלה
    # על שני המודלים, אבל בפועל זה לא עבד כמצופה - בקשה נסגרה כ"טופלה"
    # אחרי ריצת 3.5 בלבד אם לחברה כבר היה has_snapshot_36=1 מוצלחת ישנה,
    # לפני שריצת 3.6 בכלל הספיקה להתעדכן. במקום לתקן את זה, מפשטים:
    # עדיפות רלוונטית רק כשבאמת רצים 3.6 (האיכות שבשבילה יש דחיפות
    # מלכתחילה) - ל-3.5 יש תפוקה גבוהה ממילא ואינו זקוק לזה.
    if ex.GEMINI_MODEL == "gemini-3.6-flash":
        urgent_cids = set(read_priority_requests())
        if urgent_cids:
            urgent = [t for t in tasks if str(t[1]) in urgent_cids]
            rest = [t for t in tasks if str(t[1]) not in urgent_cids]
            tasks = urgent + rest
            print(f"בקשות דחיפות מהדשבורד (3.6 בלבד): {len(urgent)} משימות הוקדמו "
                  f"לראש התור ({len(urgent_cids)} חברות סומנו).")
    print(f"משימות snapshot ממוינות לפי עדיפות: "
          f"{sum(1 for t in snapshot_tasks if t[0][1])} עם extra_pdfs, "
          f"{sum(1 for t in snapshot_tasks if t[0][2])} עם 0 ידועות כרגע.")


    if n_retrying:
        print(f"מתוכם {n_retrying} הן ניסיונות חוזרים לדוחות שנכשלו קודם "
              f"(אי-ויתור: מנוסים שוב תמיד, דוחות עם כשלים רבים נדחפים לסוף התור).")

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
    print(f"הצליחו: {n_ok} | נכשלו בהרצה זו (ינוסו שוב תמיד - אי-ויתור): {n_fail}")
    if quota_exceeded_flag.is_set():
        n_not_submitted = len(tasks) - n_ok - n_fail
        print(f"נעצר עקב מכסה - כ-{n_not_submitted} משימות נותרו להרצה הבאה.")
    print(f"סה\"כ processed_reports מצטבר: {len(processed)}")
    print(f"תוצאות ב-private_subsidiaries.jsonl, מעקב ב-{args.processed_log}")

    # ── פולבאק אוטומטי ל-3.5 (רק במצב חירום, כשל 3.6 עקב מכסה) ─────────
    # extraction.js תמיד שולח בקשות חירום ל-3.6-flash (איכות עדיפה) בלי
    # לציין מודל אחר. אם זה נכשל *ספציפית* עקב מכסה יומית (לא סיבה
    # אחרת) - מנסים מיד שוב באותה ריצה עם 3.5-flash-lite (מכסה גבוהה
    # בהרבה, 500 RPD/מפתח מול 20), כדי שהמשתמש בדשבורד עדיין יקבל
    # תוצאה עכשיו, לא רק "נכשל, חכה למחר".
    if (args.company_id and n_ok == 0 and quota_exceeded_flag.is_set()
            and ex.GEMINI_MODEL == "gemini-3.6-flash"):
        safe_print(f"\n*** 3.6-flash נכשל עקב מכסה - מנסה פולבאק אוטומטי "
                   f"ל-3.5-flash-lite... ***")
        ex.switch_model("gemini-3.5-flash-lite")
        fallback_log_path = "processed_reports_gemini-3_5-flash-lite.json"
        fallback_processed = load_processed(fallback_log_path)
        cid = str(args.company_id)
        entry = plan.get(cid)
        if entry:
            ok = rsb.process_company(cid, entry)
            snap = entry.get("snapshot")
            if snap:
                mark(fallback_processed, fallback_log_path, snap["report_id"], cid,
                     "success" if ok else "failed", "snapshot")
            if ok:
                n_ok, n_fail = 1, 0
                safe_print("*** פולבאק ל-3.5-flash-lite הצליח. ***")
            else:
                safe_print("*** פולבאק ל-3.5-flash-lite גם נכשל. ***")
            _commit_progress(args.results, fallback_log_path, fallback_processed,
                              reason="emergency fallback to 3.5")

    # ── כתיבת שכבת סטטוס ל-D1 (לדשבורד) ──────────────────────────────
    # נגזרת מהנתונים שכבר בזיכרון/דיסק. נכשל בשקט אם אין הגדרות D1 -
    # זו שכבת-תצוגה, לא קריטית לפייפליין עצמו (מקור האמת ב-git).
    # ex.GEMINI_MODEL (לא רק GEMINI_MODEL_OVERRIDE) - כדי לשקף פולבאק
    # אם קרה.
    model = ex.GEMINI_MODEL
    try:
        _sync_status_to_d1(plan, processed, model, n_ok, n_fail,
                            quota_exceeded_flag.is_set(), args.processed_log, args.results)
    except Exception as e:
        print(f"אזהרה: סנכרון סטטוס ל-D1 נכשל (לא קריטי): {e}")

"""
report_selector.py

שלב 1 בתוכנית העבודה: לכל חברה, מוצא את דוח ה"סיכום" האחרון (שנתי/20-F)
כבסיס, ואת כל דוחות ה"שינויים" (רבעוניים/מיידיים) שפורסמו אחריו.

לא שולח כלום ל-Gemini בשלב הזה - רק בונה את התוכנית: אילו PDFs לעבד
ובאיזה תפקיד (בסיס מלא / דלתא).

הרצה:
    python report_selector.py --from-date 2024-01-01 --to-date 2026-08-24 \
        --out selection_plan.json
"""

import argparse
import json
from collections import defaultdict
from datetime import date, datetime, timedelta

import maya_reports_client as mrc

# קודים מאושרים על ידי אריה (לא ניחוש):
SNAPSHOT_EVENT_IDS = [101, 103]      # דוח שנתי, 20-F - כולל את כל רשימת ההחזקות
CHANGE_EVENT_IDS = [104, 105, 106]   # רבעוניים/מיידיים - רק דיווחי שינוי


def _parse_publish_date(s: str) -> datetime:
    # publishDate מגיע כמו "2026-08-21T09:47:01.833" - בלי אזור זמן.
    return datetime.fromisoformat(s)


def _title_score(title: str) -> int:
    """ציון התאמה לכותרת "דוח תקופתי אמיתי" - גבוה יותר = סביר יותר
    שזה הדוח המלא, לא מצגת/תקציר. מבוסס על כותרות אמיתיות שראינו
    בפועל (עברית: "דוח תקופתי ושנתי לשנת X"; אנגלית: "20-F"/"10-K"/
    "Annual Report"), לא ניחוש."""

    t = title or ""
    if "מצגת" in t:
        return -10  # אף פעם לא הדוח האמיתי - ראינו זאת מאומת (תפרון, אלקו)
    if "דוח תקופתי" in t and "ושנתי" in t:
        return 10  # ההתאמה המדויקת ביותר שראינו בפועל
    if "20-F" in t or "10-K" in t or "Annual Report" in t:
        return 10
    if "דוח תקופתי" in t:
        return 5
    return 0  # לא ידוע - לא פוסל, רק לא מועדף


def build_snapshot_index(from_date: date, to_date: date) -> dict:
    """מחזיר {companyId: {"company_name", "report_id", "publish_date", "pdf_url", "extra_pdfs"}}
    - לכל חברה, בוחר את הדוח הנכון מבין כמה מועמדים אפשריים באותו טווח,
    לפי סדר עדיפות:
    1. כותרת - ציון לפי _title_score (מוודא מאומת: "מצגת" בכותרת אף פעם
       איננה הדוח האמיתי, גם אם היא מאוחרת/גדולה יותר).
    2. תאריך - בין דוחות עם אותו ציון כותרת (למשל דוח מקורי + תיקון עם
       אותה כותרת בדיוק), מעדיפים את המאוחר - זו כנראה גרסה מעודכנת.
    3. גודל - רק כ-fallback אחרון, כשאין שום דוח עם ציון כותרת חיובי
       (מקרה קצה: לא ידוע איזו כותרת מייצגת את הדוח האמיתי)."""

    print("שולף דוחות סיכום (שנתי/20-F)...")
    reports = mrc.fetch_all_reports_chunked_cached(
        from_date, to_date, SNAPSHOT_EVENT_IDS, cache_tag="snapshot"
    )
    print(f"  סה\"כ {len(reports)} דוחות סיכום בטווח.")

    best_by_company = {}
    for r in reports:
        pdf_url = mrc.pick_main_pdf_url(r)
        if not pdf_url:
            continue  # אין PDF (למשל iXBRL בלבד) - אין מה לשלוח ל-Gemini

        title = r.get("title") or ""
        title_score = _title_score(title)
        pdf_size = mrc.pick_main_pdf_size(r)
        pub_date = _parse_publish_date(r["publishDate"])

        for c in r.get("companies", []):
            if c.get("isDeleted"):
                continue
            cid = c["companyId"]
            existing = best_by_company.get(cid)

            if existing is None:
                is_better = True
            elif title_score > existing["title_score"]:
                is_better = True
            elif title_score < existing["title_score"]:
                is_better = False
            elif pub_date != existing["publish_date"]:
                is_better = pub_date > existing["publish_date"]  # אותו ציון כותרת - המאוחר עדיף
            else:
                is_better = pdf_size > existing["pdf_size"]  # גם תאריך זהה - fallback לגודל

            if is_better:
                best_by_company[cid] = {
                    "company_name": c["name"],
                    "report_id": str(r["id"]),
                    "title": title,
                    "title_score": title_score,
                    "publish_date": pub_date,
                    "pdf_url": pdf_url,
                    "pdf_size": pdf_size,
                    "extra_pdfs": mrc.extra_pdfs(r),
                }

    print(f"  {len(best_by_company)} חברות עם דוח סיכום.")
    return best_by_company


def build_change_index(from_date: date, to_date: date, snapshot_index: dict) -> dict:
    """מחזיר {companyId: [{"report_id","publish_date","pdf_url"}, ...]} -
    רק דוחות שפורסמו *אחרי* ה-snapshot של אותה חברה."""

    print("\nשולף דוחות שינויים (רבעוניים/מיידיים)...")
    reports = mrc.fetch_all_reports_chunked_cached(
        from_date, to_date, CHANGE_EVENT_IDS, cache_tag="change"
    )
    print(f"  סה\"כ {len(reports)} דוחות שינויים בטווח.")

    changes_by_company = defaultdict(list)
    for r in reports:
        pdf_url = mrc.pick_main_pdf_url(r)
        if not pdf_url:
            continue
        pub_date = _parse_publish_date(r["publishDate"])
        for c in r.get("companies", []):
            if c.get("isDeleted"):
                continue
            cid = c["companyId"]
            snap = snapshot_index.get(cid)
            # אם אין snapshot בכלל לחברה הזו, שומרים את השינוי בכל זאת -
            # יעובד כ"סיכום" חלופי בהיעדר דוח שנתי בטווח (למשל חברה חדשה).
            if snap is not None and pub_date <= snap["publish_date"]:
                continue
            changes_by_company[cid].append({
                "company_name": c["name"],
                "report_id": str(r["id"]),
                "publish_date": pub_date,
                "pdf_url": pdf_url,
                "extra_pdfs": mrc.extra_pdfs(r),
            })

    for cid in changes_by_company:
        changes_by_company[cid].sort(key=lambda x: x["publish_date"])

    n_companies_with_changes = len(changes_by_company)
    n_total_changes = sum(len(v) for v in changes_by_company.values())
    print(f"  {n_total_changes} דוחות שינויים רלוונטיים ({n_companies_with_changes} חברות).")
    return changes_by_company


def build_plan(from_date: date, to_date: date) -> dict:
    snapshot_index = build_snapshot_index(from_date, to_date)
    change_index = build_change_index(from_date, to_date, snapshot_index)

    all_company_ids = set(snapshot_index) | set(change_index)
    plan = {}
    for cid in all_company_ids:
        snap = snapshot_index.get(cid)
        changes = change_index.get(cid, [])
        plan[str(cid)] = {
            "company_name": (snap or (changes[0] if changes else {})).get("company_name"),
            "snapshot": (
                {
                    "report_id": snap["report_id"],
                    "title": snap["title"],
                    "publish_date": snap["publish_date"].isoformat(),
                    "pdf_url": snap["pdf_url"],
                    "pdf_size_kb": snap["pdf_size"],
                    "extra_pdfs": snap["extra_pdfs"],
                }
                if snap else None
            ),
            "changes": [
                {
                    "report_id": c["report_id"],
                    "publish_date": c["publish_date"].isoformat(),
                    "pdf_url": c["pdf_url"],
                }
                for c in changes
            ],
        }
    return plan


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--from-date", required=True, help="YYYY-MM-DD")
    parser.add_argument("--to-date", required=True, help="YYYY-MM-DD")
    parser.add_argument("--out", default="selection_plan.json")
    args = parser.parse_args()

    from_date = date.fromisoformat(args.from_date)
    to_date = date.fromisoformat(args.to_date)

    # Maya מסרבת לבקשות שה-toDate שלהן כולל את "היום" (אימות בפועל:
    # "'To Date Value Date' חייב להיות קטן או שווה ל-<אתמול> 0:00:00") -
    # מגבילים אוטומטית ל"אתמול" לכל היותר, בלי תלות במה שהמשתמש הזין.
    yesterday = date.today() - timedelta(days=1)
    if to_date > yesterday:
        print(f"הערה: to_date ({to_date}) מוגבל אוטומטית ל-{yesterday} "
              f"(Maya לא מקבלת בקשות שכוללות את היום הנוכחי).")
        to_date = yesterday

    plan = build_plan(from_date, to_date)

    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(plan, f, ensure_ascii=False, indent=2)

    n_with_snapshot = sum(1 for v in plan.values() if v["snapshot"])
    n_snapshot_only = sum(1 for v in plan.values() if v["snapshot"] and not v["changes"])
    n_with_changes = sum(1 for v in plan.values() if v["changes"])
    print(f"\n=== סיכום ===")
    print(f"סה\"כ חברות בתוכנית: {len(plan)}")
    print(f"  עם דוח סיכום: {n_with_snapshot}")
    print(f"  עם דוחות שינויים ממתינים: {n_with_changes}")
    print(f"  סיכום בלבד, ללא שינויים: {n_snapshot_only}")
    print(f"נשמר ל-{args.out}")

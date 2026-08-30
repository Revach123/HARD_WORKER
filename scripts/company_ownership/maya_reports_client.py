"""
maya_reports_client.py

קליינט לאיתור דוחות ב-Maya (reports/companies), עם פאגינציה מלאה.
בנוי לפי הבקשה/תגובה שנתפסו בפועל מ-DevTools (לא לוגיקה משוערת).

הערות/הנחות שדורשות אישור (מסומנות גם ב-TODO בקוד):
1. בסיס URL לקבצים מצורפים - מאומת בפועל: https://mayafiles.tase.co.il/
   דורש header ייעודי בהורדה בפועל: x-maya-with: allow (ראה
   download_report_file למטה - זה שונה מ-headers של קריאת רשימת הדוחות).
2. company_id כאן הוא המזהה הפנימי של Maya (למשל 2514) - שונה ממספר
   הרישום/ח.פ ("company_legal_id") ששימש בסקריפט extract_subsidiaries.py.
   יש להחליט: לעבור לזיהוי לפי maya_company_id, או לשמור טבלת מיפוי.
"""

import json
import os
import time
from datetime import date, datetime, timedelta, timezone

import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

MAYA_REPORTS_URL = "https://maya.tase.co.il/api/v1/reports/companies"


class MayaOffsetLimitError(Exception):
    """נזרק כש-Maya מסרבת עם 'Offset' חייב להיות קטן/שווה ל-1000 - סימן
    שטווח התאריכים הנוכחי מכיל יותר מדי דוחות לשליפה בבקשה אחת."""
    pass

# אומת בפועל מול DevTools - זהו בסיס ה-URL הנכון להורדת מצורפים.
MAYA_FILES_BASE_URL = "https://mayafiles.tase.co.il/"

# נדרש להורדת קבצים בפועל (אומת מול קריאה אמיתית) - בלעדיו כנראה תגיע חסימה.
FILE_DOWNLOAD_HEADERS = {
    "accept": "application/json, text/plain, */*",
    "accept-language": "he-IL",
    "x-maya-with": "allow",
}

# קודים ידועים (לפי מה שאושר):
EVENT_ID_ANNUAL_REPORT = 101   # דוח שנתי (ת950/ת930 עם reportType מתאים)
EVENT_ID_20F = 103             # 20-F (חברות דואליות)

HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "he-IL",
    "Content-Type": "application/json",
    "Origin": "https://maya.tase.co.il",
    "Referer": "https://maya.tase.co.il/he/reports/companies",
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/125.0.0.0 Safari/537.36"),
}


def _iso_start_of_day_utc(d: date) -> str:
    """ממיר תאריך למחרוזת ISO בפורמט שראינו בבקשה שנתפסה
    (חצות שעון ישראל, מבוטא ב-UTC עם היסט של יום קודם ב-21:00)."""
    dt = datetime(d.year, d.month, d.day, tzinfo=timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def fetch_reports_page(
    from_date: date,
    to_date: date,
    event_ids: list[int],
    page_number: int = 1,
    limit: int = 20,
    is_priority: bool = False,
    is_trade_halt: bool = False,
    max_retries: int = 4,
) -> list[dict]:
    """שליפת עמוד בודד מ-Maya. מחזיר את הרשימה הגולמית (list[dict]) כפי
    שחוזרת מהשרת - ראה דוגמת תגובה אמיתית בהערות בסוף הקובץ."""

    body = {
        "pageNumber": page_number,
        "fromDate": _iso_start_of_day_utc(from_date),
        "toDate": _iso_start_of_day_utc(to_date),
        "isPriority": is_priority,
        "isTradeHalt": is_trade_halt,
        "by": "company",
        "eventsIds": event_ids,
        "limit": limit,
        "offset": (page_number - 1) * limit,
    }

    last_error = None
    for attempt in range(max_retries):
        try:
            resp = requests.post(
                MAYA_REPORTS_URL, headers=HEADERS, json=body, verify=False, timeout=30
            )
        except requests.exceptions.ConnectionError as e:
            last_error = e
            wait = 2 ** attempt  # 1, 2, 4, 8 שניות
            print(f"    שגיאת חיבור (ניסיון {attempt + 1}/{max_retries}) - ממתין {wait}s...")
            time.sleep(wait)
            continue

        if resp.status_code == 400 and "Offset" in resp.text:
            raise MayaOffsetLimitError(resp.text)
        if resp.status_code == 400:
            # 400 לא מזוהה - לא Offset. מדפיסים הכל כדי לא לנחש בפעם הבאה.
            print(f"=== שגיאה 400 לא מזוהה מ-Maya ===")
            print(f"בקשה: fromDate={body['fromDate']}, toDate={body['toDate']}, "
                  f"eventsIds={event_ids}, pageNumber={page_number}")
            print(f"תגובה מלאה: {resp.text}")
            print(f"=== סוף פרטי השגיאה ===")
            resp.raise_for_status()
        if resp.status_code == 403:
            last_error = requests.exceptions.HTTPError(f"403: {resp.text[:200]}")
            if attempt == max_retries - 1:
                print(f"=== 403 - כישלון סופי, פרטים מלאים לאבחון ===")
                print(f"Response headers: {dict(resp.headers)}")
                print(f"Response body (עד 3000 תווים): {resp.text[:3000]}")
                print(f"=== סוף פרטים ===")
            wait = 5 * (2 ** attempt)  # 5, 10, 20, 40 שניות - חשד לחסימת קצב/anti-bot
            print(f"    403 חסום (ניסיון {attempt + 1}/{max_retries}) - ממתין {wait}s "
                  f"(ייתכן חסימת קצב זמנית)...")
            time.sleep(wait)
            continue
        if resp.status_code == 429 or resp.status_code >= 500:
            last_error = requests.exceptions.HTTPError(f"{resp.status_code}: {resp.text[:200]}")
            wait = 2 ** attempt
            print(f"    שגיאת שרת {resp.status_code} (ניסיון {attempt + 1}/{max_retries}) - ממתין {wait}s...")
            time.sleep(wait)
            continue

        resp.raise_for_status()
        return resp.json()

    raise last_error


def fetch_all_reports(
    from_date: date,
    to_date: date,
    event_ids: list[int],
    limit: int = 20,
    max_pages: int = 500,
    sleep_seconds: float = 1.2,
) -> list[dict]:
    """פאגינציה מלאה עד שמתקבל עמוד קצר מ-limit (סימן לעמוד אחרון).
    max_pages הוא רשת ביטחון נגד לולאה אינסופית אם הפורמט משתנה."""

    all_reports: list[dict] = []
    page = 1

    while page <= max_pages:
        batch = fetch_reports_page(
            from_date=from_date,
            to_date=to_date,
            event_ids=event_ids,
            page_number=page,
            limit=limit,
        )
        print(f"עמוד {page}: התקבלו {len(batch)} דוחות")
        all_reports.extend(batch)

        if len(batch) < limit:
            break

        page += 1
        time.sleep(sleep_seconds)  # נימוס כלפי השרת, לא לדפוק בלי הפסקה

    return all_reports


def _largest_pdf_attachment(report: dict) -> dict | None:
    """מחזיר את ה-attachment (dict גולמי) של ה-PDF הגדול ביותר בדוח, בלי
    להניח ש-fileType=='pdf1' משמעו 'הראשי' - זה רק סדר העלאה למאיה,
    לא סימן תוכן. גודל קובץ הוא פרוקסי אמין יותר לזיהוי הדוח המרכזי."""

    attachments = report.get("attachments", [])
    pdf_attachments = [a for a in attachments if a["fileType"].startswith("pdf")]
    if not pdf_attachments:
        return None
    return max(pdf_attachments, key=lambda a: a.get("fileSize", 0) or 0)


def pick_main_pdf_url(report: dict) -> str | None:
    """בוחר את קובץ ה-PDF הגדול ביותר מתוך attachments (לא בהכרח pdf1 -
    ראה _largest_pdf_attachment). מחזיר None אם אין PDF כלל (למשל,
    כשיש רק iXBRL - formId ת950)."""

    a = _largest_pdf_attachment(report)
    return MAYA_FILES_BASE_URL + a["url"] if a else None


def pick_main_pdf_size(report: dict) -> int:
    """גודל קובץ ה-PDF הגדול ביותר בדוח (יחידות כפי שמוחזרות מ-Maya,
    ככל הנראה KB) - 0 אם לא ידוע."""

    a = _largest_pdf_attachment(report)
    return (a.get("fileSize", 0) or 0) if a else 0


def extra_pdfs(report: dict) -> list[str]:
    """מחזיר קבצי PDF נוספים (כל מה שלא נבחר כראשי) - לצורך לוג/בדיקה
    ידנית, לא לעיבוד אוטומטי בשלב זה."""

    attachments = report.get("attachments", [])
    pdf_attachments = [a for a in attachments if a["fileType"].startswith("pdf")]
    main_url = pick_main_pdf_url(report)

    return [
        MAYA_FILES_BASE_URL + a["url"]
        for a in pdf_attachments
        if MAYA_FILES_BASE_URL + a["url"] != main_url
    ]


def download_report_file(url: str) -> bytes:
    """הורדת קובץ מצורף בפועל. דורש את FILE_DOWNLOAD_HEADERS (אומת ב-DevTools),
    בשונה מ-HEADERS ששימש לקריאת רשימת הדוחות."""
    resp = requests.get(url, headers=FILE_DOWNLOAD_HEADERS, verify=False, timeout=60)
    resp.raise_for_status()
    return resp.content


def fetch_all_reports_safe(
    from_date: date,
    to_date: date,
    event_ids: list[int],
    limit: int = 20,
    _depth: int = 0,
) -> list[dict]:
    """כמו fetch_all_reports, אבל מטפלת אוטומטית בגבול ה-offset של Maya
    (מקסימום ~1000 דוחות לשאילתה): כשנתקלים בחריגה, מפצלים את טווח
    התאריכים לשניים וממשיכים רקורסיבית על כל חצי, עד שכל תת-טווח נכנס
    בגבול. התוצאות מאוחדות ומנוקות מכפילויות (לפי report id) בסוף."""

    indent = "  " * _depth
    try:
        return fetch_all_reports(from_date, to_date, event_ids, limit=limit)
    except MayaOffsetLimitError:
        if from_date >= to_date:
            print(f"{indent}אזהרה: טווח יום בודד ({from_date}) עדיין חורג מהגבול - "
                  f"מקבל רק את 1000 הדוחות הראשונים של אותו יום (חלק ייחסר).")
            return fetch_all_reports(from_date, to_date, event_ids, limit=limit, max_pages=50)

        mid = from_date + (to_date - from_date) / 2
        print(f"{indent}טווח {from_date}..{to_date} חורג מגבול ה-offset - מפצל סביב {mid}.")

        left = fetch_all_reports_safe(from_date, mid, event_ids, limit, _depth + 1)
        right = fetch_all_reports_safe(mid + timedelta(days=1),
                                        to_date, event_ids, limit, _depth + 1)

        seen_ids = set()
        merged = []
        for r in left + right:
            if r["id"] not in seen_ids:
                seen_ids.add(r["id"])
                merged.append(r)
        return merged


def fetch_all_reports_chunked_cached(
    from_date: date,
    to_date: date,
    event_ids: list[int],
    cache_tag: str,
    chunk_days: int = 5,
    cache_dir: str = ".maya_cache",
    limit: int = 20,
) -> list[dict]:
    """מחלק את הטווח לחלונות קבועים, שולף כל חלון שעוד לא נשלף, ושומר
    הכל לקובץ ממוזג יחיד (לא עשרות קבצי-חלון בודדים כמו קודם) -
    {cache_dir}/{cache_tag}_merged.json, עם manifest פנימי של אילו
    חלונות כבר נשלפו. שמירה מיד אחרי כל חלון בודד (לא בסוף) - כדי
    שגם ריצה שנקטעת באמצע לא תאבד את מה שכבר הצליחה.

    קובץ יחיד לכל cache_tag אומר שקל לחייב אותו ל-git (בניגוד לעשרות
    קבצים) - זו הדרך שבה ה-\"כבר נשלף\" נשמר לצמיתות, לא תלוי ב-cache
    ארעי של GitHub Actions שראינו שנשבר בין branches.

    cache_tag: מזהה קצר לסוג השאילתה (למשל \"snapshot\" או \"change\") -
    כדי שקבצי מטמון של שני סוגי הדוחות לא יתנגשו.
    """
    os.makedirs(cache_dir, exist_ok=True)
    merged_path = os.path.join(cache_dir, f"{cache_tag}_merged.json")

    if os.path.exists(merged_path):
        with open(merged_path, encoding="utf-8") as f:
            merged = json.load(f)
    else:
        merged = {"windows_fetched": [], "reports": []}

    fetched_windows = {tuple(w) for w in merged["windows_fetched"]}

    chunks = []
    cur = from_date
    while cur <= to_date:
        chunk_end = min(cur + timedelta(days=chunk_days - 1), to_date)
        chunks.append((cur, chunk_end))
        cur = chunk_end + timedelta(days=1)

    n_cached, n_fetched = 0, 0

    for i, (c_from, c_to) in enumerate(chunks, 1):
        window_key = (c_from.isoformat(), c_to.isoformat())

        if window_key in fetched_windows:
            print(f"[{i}/{len(chunks)}] {c_from}..{c_to}: מהמטמון")
            n_cached += 1
            continue

        print(f"[{i}/{len(chunks)}] {c_from}..{c_to}: שולף...")
        chunk_reports = fetch_all_reports_safe(c_from, c_to, event_ids, limit=limit)
        merged["reports"].extend(chunk_reports)
        merged["windows_fetched"].append(list(window_key))

        with open(merged_path, "w", encoding="utf-8") as f:
            json.dump(merged, f, ensure_ascii=False)

        print(f"    נשמר ({len(chunk_reports)} דוחות).")
        n_fetched += 1

    print(f"\nסה\"כ: {n_cached} חלונות מהמטמון, {n_fetched} חלונות נשלפו עכשיו.")

    # ניקוי כפילויות אם דוח נופל בדיוק על גבול בין חלונות
    seen_ids = set()
    deduped = []
    for r in merged["reports"]:
        if r["id"] not in seen_ids:
            seen_ids.add(r["id"])
            deduped.append(r)
    return deduped


if __name__ == "__main__":
    # הרצת בדיקה ידנית: שליפת עמוד ראשון בלבד, ללא כתיבה ל-D1
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--from-date", required=True, help="YYYY-MM-DD")
    parser.add_argument("--to-date", required=True, help="YYYY-MM-DD")
    args = parser.parse_args()

    reports = fetch_reports_page(
        from_date=date.fromisoformat(args.from_date),
        to_date=date.fromisoformat(args.to_date),
        event_ids=[EVENT_ID_ANNUAL_REPORT, EVENT_ID_20F],
        page_number=1,
    )

    for r in reports[:5]:
        names = [c["name"] for c in r["companies"]]
        print(r["id"], r["publishDate"], names, "->", pick_main_pdf_url(r))

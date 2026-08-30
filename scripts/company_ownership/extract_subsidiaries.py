"""
extract_subsidiaries.py

פייפליין יומי לחילוץ מבנה החזקות (חברות בנות + אחוזי החזקה) מדוחות/מצגות
שפורסמו ב-Maya, באמצעות Gemini API (טייר חינמי), וכתיבה ל-Cloudflare D1.

הרצה: python extract_subsidiaries.py --company-legal-id 520025370 --date 2026-08-24

תלויות: requests
    pip install requests --break-system-packages

הערות סביבה:
- כל בקשות ה-HTTP רצות מאחורי NetFree proxy -> verify=False + urllib3 warning suppress.
- כתיבה ל-D1: SQL נכתב ל-/tmp/payload.sql, ואז wrangler d1 execute --remote --file.
  (--remote הוא חובה, אחרת עובדים על עותק מקומי ריק).
"""

import argparse
import base64
import json
import os
import re
import subprocess
import sys
import threading
import time
from collections import deque
from datetime import date, datetime

import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

GEMINI_MODEL = os.environ.get("GEMINI_MODEL_OVERRIDE", "gemini-3.6-flash")
# ברירת מחדל: gemini-3.6-flash (איכות חילוץ עדיפה, 20 RPD/מפתח).
# ניתן לעקוף עם GEMINI_MODEL_OVERRIDE=gemini-3.5-flash-lite - למשל
# כדי להמשיך לעבד תוך כדי שהמכסה של 3.6 מתאפסת (500 RPD/מפתח, איכות
# פחות טובה). כל מודל עוקב אחרי processed_reports משלו (ראה
# run_full_batch.py --processed-log) - לא חוסמים זה את זה, וכל אחד
# יגיע בסוף לכל 647 החברות, לפי אותו סדר עדיפות.

# עולה בכל פעם שהשדות בפלט (בעיקר change_events) משתנים מהותית - למשל
# הוספת event_type/ownership_pct_before. run_full_batch.py משווה מול
# schema_version שנשמר בכל רשומה כדי לדעת אוטומטית אילו רשומות ישנות
# צריכות עיבוד מחדש - בלי צורך בדגל ידני שאפשר לשכוח לכבות.
#
# 2->3 (2026-08-30): לא שינוי שדות - שינוי בהתנהגות החילוץ עצמו (תיקון
# עצירה-בהצלחה-ראשונה בין קבצים + בדיקה ממוקדת בתוך-קובץ, ראה
# diagnose_multipart.py/ab_test_results.json). רשומות snapshot ישנות
# (schema<3) עלולות להחמיץ עשרות חברות בת אמיתיות (נצפה בפועל: 11 מול
# 58 בדיסקונט) - מעלים גרסה כדי שהן יתעבדו מחדש אוטומטית, לא רק change.
CURRENT_SCHEMA_VERSION = 3


class GeminiQuotaExceededError(Exception):
    """נזרק כש-Gemini מחזיר 429 עם 'exceeded your current quota' אחרי כל
    ניסיונות ה-retry - סימן למכסה יומית/RPM אמיתית שלא תיפתר בהמתנה
    קצרה. מבדיל מ-503/429 חולפים (עומס רגעי) שכן שווה לנסות שוב."""
    pass


class RateLimiter:
    """מגביל קצב thread-safe - חלון נגלל של 60 שניות. חוסם (לא זורק)
    עד שיש סלוט פנוי. משותף בין כל ה-threads בהרצה מקבילית, כדי שכולם
    יחד לא יחצו את ה-RPM האמיתי של Gemini (15 - משאירים מרווח ביטחון)."""

    def __init__(self, max_per_minute: int):
        self.max_per_minute = max_per_minute
        self.calls: deque = deque()
        self.lock = threading.Lock()

    def acquire(self) -> None:
        while True:
            with self.lock:
                now = time.monotonic()
                while self.calls and now - self.calls[0] > 60:
                    self.calls.popleft()
                if len(self.calls) < self.max_per_minute:
                    self.calls.append(now)
                    return
                wait = 60 - (now - self.calls[0]) + 0.1
            time.sleep(wait)


# 14 ולא 15 - מרווח ביטחון קטן מול המכסה האמיתית (ראה dashboard: 15 RPM
# עבור gemini-3.5-flash-lite). rate limiter נפרד לכל מפתח API - ראה
# GeminiKeyPool למטה.

# לוק לכתיבת קבצים (private_subsidiaries.jsonl) - מונע שזירת שורות בין
# threads שכותבים בו-זמנית (append עצמו בטוח, אבל שתי כתיבות חופפות
# עלולות "להתערבב" לשורה לא-תקינה בלי הלוק).
_file_write_lock = threading.Lock()

GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/models"


class GeminiKeyPool:
    """מאגר מפתחות API, thread-safe. round-robin בין מפתחות שעדיין לא
    בקירור; לכל מפתח rate limiter (RPM) נפרד משלו. GEMINI_API_KEY חובה,
    GEMINI_API_KEY_2 (וכל GEMINI_API_KEY_N נוסף) אופציונליים - אם קיימים
    ב-env, מצטרפים אוטומטית למאגר.

    כל מפתח מתאושש בעצמו - COOLDOWN_SECONDS אחרי שהוא נכשל עם 'quota
    exceeded' הוא זמין שוב אוטומטית, בלי שום תלות במפתחות אחרים. זה
    נכון גם אם המפתחות חולקים בפועל מכסה אחת (אותו Google Project) -
    אין סיבה להמתין למפתח שלא נכשל, ואין סיבה לחכות יותר מהנדרש
    למפתח שכן נכשל."""

    COOLDOWN_SECONDS = 65  # TPM/RPM זמני - חוזר לבד תוך ~60s

    def __init__(self, keys: list[str]):
        if not keys:
            raise RuntimeError("לא נמצא אף GEMINI_API_KEY ב-environment")
        self._keys = keys
        self._limiters = {k: RateLimiter(max_per_minute=14) for k in keys}
        self._exhausted_at: dict[str, float] = {}  # key -> timestamp (monotonic)
        self._daily_exhausted: set[str] = set()  # RPD אמיתי - לא מתאושש היום
        self._lock = threading.Lock()
        self._next_idx = 0

    def get_next_key(self) -> str | None:
        """מחזיר מפתח זמין, או None אם כולם עדיין בקירור/במכסה יומית.
        מפתח בקירור זמני חוזר אוטומטית להיות זמין ברגע ש-COOLDOWN_SECONDS
        חלפו. מפתח שסומן daily_exhausted לא חוזר לעולם באותו תהליך -
        אין טעם לנסות שוב היום."""
        with self._lock:
            now = time.monotonic()
            available = [
                k for k in self._keys
                if k not in self._daily_exhausted
                and (k not in self._exhausted_at
                     or now - self._exhausted_at[k] >= self.COOLDOWN_SECONDS)
            ]
            if not available:
                return None
            key = available[self._next_idx % len(available)]
            self._next_idx += 1
            self._exhausted_at.pop(key, None)  # מפתח שנבחר כבר לא "בקירור"
            return key

    def mark_exhausted(self, key: str, daily: bool = False) -> None:
        """daily=True - מכסה יומית אמיתית (RPD), מזוהה לפי quotaId עם
        'PerDay' בתגובת השגיאה (ראה call_gemini_extraction). המפתח
        נחסם לגמרי עד סוף התהליך הנוכחי - לא עוד קירור-ואז-ניסיון-חוזר
        על אותו מפתח, זה רק מבזבז זמן על מכסה שלא תתאפס עד מחר."""
        with self._lock:
            if daily:
                self._daily_exhausted.add(key)
            else:
                self._exhausted_at[key] = time.monotonic()

    def all_daily_exhausted(self) -> bool:
        with self._lock:
            return len(self._daily_exhausted) == len(self._keys)

    def seconds_until_next_available(self) -> float:
        """כמה שניות עד שהמפתח הכי-קרוב-להחלמה יהיה זמין - לשימוש
        כשכולם בקירור, כדי לחכות בדיוק את הזמן הנדרש ולא יותר. מחזיר
        inf אם כל המפתחות הפנויים (לא daily_exhausted) כבר בקירור אבל
        עדיין 0 בפועל כי COOLDOWN_SECONDS תמיד חולף - inf רק אם ממש
        כולם daily_exhausted (אין למי לחכות בכלל)."""
        with self._lock:
            candidates = [k for k in self._keys if k not in self._daily_exhausted]
            if not candidates:
                return float("inf")
            if not self._exhausted_at:
                return 0.0
            now = time.monotonic()
            remaining = [
                self.COOLDOWN_SECONDS - (now - t)
                for k, t in self._exhausted_at.items() if k in candidates
            ]
            return max(0.0, min(remaining)) if remaining else 0.0

    def limiter_for(self, key: str) -> "RateLimiter":
        return self._limiters[key]


def _collect_api_keys() -> list[str]:
    """אוסף את כל משתני ה-GEMINI_API_KEY* מה-environment, ממוינים לפי שם
    (כך ש-GEMINI_API_KEY תמיד ראשון, GEMINI_API_KEY_2 שני וכו'). מתעלם
    ממשתנים ריקים - secret שלא הוגדר ב-GitHub Actions מגיע כמחרוזת ריקה,
    לא נעדר לגמרי, ולכן חייבים לסנן במפורש."""
    keys = []
    for name in sorted(os.environ):
        if name == "GEMINI_API_KEY" or re.match(r"^GEMINI_API_KEY_\d+$", name):
            value = os.environ[name]
            if value:
                keys.append(value)
    return keys


_key_pool = GeminiKeyPool(_collect_api_keys())

D1_DATABASE_NAME = "index_compare_db"  # להחליף לפי מסד היעד בפועל

EXTRACTION_SYSTEM_PROMPT = """\
אתה מנוע חילוץ מידע פיננסי. תפקידך לזהות מבנה החזקות (חברות בנות/מוחזקות)
בתוך מסמך שהועלה (דוח כספי, דוח דירקטוריון, או מצגת משקיעים של חברה ישראלית
הנסחרת בבורסה בתל אביב).

כללים מחייבים:
1. החזר אך ורק JSON תקני. אל תוסיף טקסט, הסברים, או Markdown code fences.
2. חלץ רק חברות שמצוין לגביהן במפורש אחוז החזקה (מספר) בתוך המסמך - טקסט,
   טבלה, או גרפיקה/אינפוגרפיקה. אל תשער או תמלא אחוזים חסרים.

איפה לחפש (מקורות מובנים, בסדר עדיפות):
- בדוחות תקופתיים ישראליים: טבלת "חברות מוחזקות עיקריות" תחת פרק ד'
  ("פרטים נוספים על התאגיד"), מוכרת גם כטבלת "תקנה 11".
- בדוחות 20-F (חברות דואליות): סעיף "Organizational Structure" (Item 4.C)
  ו/או נספח "List of Subsidiaries".
- אם אין טבלה כזו, חפש בטקסט חופשי בדוח הדירקטוריון ובבאורים לדוחות
  הכספיים (לרוב באור 1 "כללי" או באור נפרד על חברות מוחזקות).

אזהרה קריטית - כיוון ההחזקה: אנחנו רוצים אך ורק חברות שהחברה המדווחת
*מחזיקה בהן* (subsidiaries/investees), לעולם לא את מי שמחזיק *בחברה
המדווחת עצמה*. אל תשתמש בטבלאות "Major Shareholders" / "Principal
Shareholders" / "בעלי עניין" / "בעלי שליטה" - אלו מתארות את הכיוון ההפוך
ולא רלוונטיות בכלל. דוגמה למלכודת נפוצה בדוחות 20-F: אם המסמך מראה
"Company X - 64.9% - held by Shareholder Y", זה אומר ש-Y מחזיק ב-X, לא
ש-X היא חברת בת של החברה המדווחת - יש להתעלם משורה כזו לחלוטין אלא אם
"Shareholder Y" הוא בעצמו החברה המדווחת.
3. אם אין אחוז מפורש לחברה מסוימת, עדיין כלול אותה ברשימה עם
   "ownership_pct": null - אל תמציא מספר ואל תשמיט אותה.
4. לכל חברה ציין page_reference (מספר עמוד/שקף במסמך שבו מופיע הנתון),
   כדי לאפשר בדיקה ידנית מהירה.
5. לכל חברה מלא section_title (כותרת הפרק/הסעיף המדויקת כפי שמופיעה
   במסמך) ו-table_title (כותרת הטבלה הספציפית, אם המידע הגיע מטבלה עם
   כותרת - אחרת null). זה קריטי: המטרה היא לזהות דפוסים חוזרים בין
   דוחות שונים, אז חשוב שהכותרות יהיו מדויקות ולא מנוסחות-מחדש.
   report_format נקבע לפי שפת/מבנה/מסגרת הרגולציה של הדוח כולו (למשל
   דוח בעברית עם מבנה תקנות ניירות ערך = israeli_periodic) - זה בלתי
   תלוי לחלוטין בשאלה אם מצאת טבלה פורמלית של חברות מוחזקות. אל תסמן
   "other" רק כי לא מצאת עדיין את הטבלה - המשך לחפש בכל המסמך.
5א. חובה לחפש בכל אורך המסמך (לא להסתפק בעמודים הראשונים) אחרי הטבלה
   הפורמלית הספציפית - "תקנה 11" (עברית) או "Organizational Structure"/
   "List of Subsidiaries" (אנגלית) - היא תמיד עדיפה כמקור על פני אזכורים
   לא-פורמליים כמו "מבנה הקבוצה", "אודות החברה" או שקפי אינפוגרפיקה.
   אם מצאת גם טבלה פורמלית וגם אזכורים לא-פורמליים, תעדיף ותשתמש בטבלה
   הפורמלית. רק אם אחרי חיפוש מלא בכל המסמך אין טבלה פורמלית כלל, השתמש
   במקורות לא-פורמליים כגיבוי.
6. אם המסמך מציין תאריך "נכון ליום" עבור טבלת ההחזקות, החזר אותו בשדה
   as_of_date בפורמט YYYY-MM-DD.
7. אם זהו דוח תקופתי (לא מצגת) והמידע העיקרי הוא על שינויים/עסקאות
   (רכישה/מכירה של אחוזים) ולא טבלת מצב מלאה, סמן
   "document_type": "change_events" והחזר כל אירוע בנפרד עם השדות
   company, ownership_pct_after (האחוז אחרי העסקה, אם צוין), event_date,
   event_description.
6א. לכל אירוע שינוי, סווג event_type לפי אחת מהקטגוריות:
   - "new_acquisition" - רכישה ראשונית של החזקה בחברה שלא הייתה מוחזקת קודם
   - "increase" - הגדלת אחוז החזקה קיים
   - "decrease" - הקטנה חלקית של אחוז החזקה (עדיין נשארת החזקה כלשהי)
   - "full_disposal" - מכירה/מימוש מלא של כל ההחזקה (ownership_pct_after
     צריך להיות 0 במקרה הזה, אלא אם המסמך לא נותן מספר מדויק)
   - "other" - לא ברור, או לא מתאים לאף קטגוריה (למשל שינוי מבני שאינו
     קנייה/מכירה, כמו מיזוג/פיצול)
   השדה הזה קריטי לשילוב אוטומטי עתידי בין דוח שנתי לעדכונים - אל תשמיט
   אותו ואל תנחש אם המסמך לא נותן מספיק מידע לסיווג ודאי (במקרה כזה
   סמן "other" ותן את הפרטים הזמינים ב-event_description).
8. אם זו מצגת/דוח עם טבלת מצב מלאה (snapshot), סמן
   "document_type": "ownership_snapshot".

סכימת פלט (JSON בלבד):
{
  "report_format": "israeli_periodic" | "20-F" | "10-K" | "other" | "unknown",
  "document_type": "ownership_snapshot" | "change_events" | "unknown",
  "as_of_date": "YYYY-MM-DD" | null,
  "subsidiaries": [
    {
      "name": "שם החברה כפי שמופיע במסמך",
      "ownership_pct": number | null,
      "parent": "שם חברת האם הישירה, אם שונה מהחברה המדווחת עצמה" | null,
      "page_reference": number | null,
      "section_title": "כותרת הפרק/הסעיף שבו נמצא המידע, בדיוק כפי שמופיעה במסמך (למשל: 'פרק ד - פרטים נוספים על התאגיד', 'Item 4.C Organizational Structure')" | null,
      "table_title": "כותרת הטבלה הספציפית אם המידע הגיע מטבלה (למשל: 'תקנה 11', 'List of Subsidiaries') - null אם זה טקסט חופשי ולא טבלה" | null,
      "note": "הערה קצרה אם רלוונטי (למשל: לאחר מימוש אופציה)" | null
    }
  ],
  "change_events": [
    {
      "company": "שם החברה",
      "event_type": "new_acquisition" | "increase" | "decrease" | "full_disposal" | "other",
      "ownership_pct_before": number | null,
      "ownership_pct_after": number | null,
      "event_date": "YYYY-MM-DD" | null,
      "event_description": "תיאור קצר",
      "page_reference": number | null,
      "section_title": "כמו למעלה" | null
    }
  ]
}
"""


# תרגום מדויק של סכימת הפלט המתוארת בטקסט חופשי ב-EXTRACTION_SYSTEM_PROMPT
# (בסעיף "סכימת פלט (JSON בלבד)") ל-responseSchema אוכף של Gemini. שני
# המקורות חייבים להישאר מסונכרנים - אם משנים שדה באחד, לשנות גם בשני.
EXTRACTION_RESPONSE_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "report_format": {
            "type": "STRING",
            "enum": ["israeli_periodic", "20-F", "10-K", "other", "unknown"],
        },
        "document_type": {
            "type": "STRING",
            "enum": ["ownership_snapshot", "change_events", "unknown"],
        },
        "as_of_date": {"type": "STRING", "nullable": True},
        "subsidiaries": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "name": {"type": "STRING"},
                    "ownership_pct": {"type": "NUMBER", "nullable": True},
                    "parent": {"type": "STRING", "nullable": True},
                    "page_reference": {"type": "NUMBER", "nullable": True},
                    "section_title": {"type": "STRING", "nullable": True},
                    "table_title": {"type": "STRING", "nullable": True},
                    "note": {"type": "STRING", "nullable": True},
                },
                "required": ["name"],
            },
        },
        "change_events": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "company": {"type": "STRING"},
                    "event_type": {
                        "type": "STRING",
                        "enum": ["new_acquisition", "increase", "decrease",
                                 "full_disposal", "other"],
                    },
                    "ownership_pct_before": {"type": "NUMBER", "nullable": True},
                    "ownership_pct_after": {"type": "NUMBER", "nullable": True},
                    "event_date": {"type": "STRING", "nullable": True},
                    "event_description": {"type": "STRING"},
                    "page_reference": {"type": "NUMBER", "nullable": True},
                    "section_title": {"type": "STRING", "nullable": True},
                },
                "required": ["company", "event_type", "event_description"],
            },
        },
    },
    "required": ["subsidiaries", "change_events"],
}


def download_pdf(url: str) -> bytes:
    # x-maya-with נדרש בפועל להורדת קבצים מ-mayafiles.tase.co.il (אומת ב-DevTools).
    # אם המקור אינו Maya, ה-header הזה לא אמור להזיק אך אפשר להסירו.
    headers = {
        "accept": "application/json, text/plain, */*",
        "accept-language": "he-IL",
        "x-maya-with": "allow",
    }
    resp = requests.get(url, headers=headers, verify=False, timeout=60)
    resp.raise_for_status()
    return resp.content


def all_keys_daily_exhausted() -> bool:
    """True אם כל המפתחות (GEMINI_API_KEY*) הגיעו למכסה יומית אמיתית
    (RPD) - לשימוש חיצוני (run_small_batch.py) כדי לוותר מיד בלי לחכות
    קירור נוסף שלא יעזור."""
    return _key_pool.all_daily_exhausted()


def call_gemini_extraction(pdf_bytes: bytes, filename_hint: str, max_retries: int = 2) -> dict:
    b64 = base64.b64encode(pdf_bytes).decode()
    payload = {
        "system_instruction": {"parts": [{"text": EXTRACTION_SYSTEM_PROMPT}]},
        "contents": [
            {
                "role": "user",
                "parts": [
                    {"inline_data": {"mime_type": "application/pdf", "data": b64}},
                    {
                        "text": (
                            f"שם הקובץ המקורי: {filename_hint}. "
                            "חלץ את מבנה ההחזקות לפי הכללים שהוגדרו."
                        )
                    },
                ],
            }
        ],
        "generationConfig": {
            "responseMimeType": "application/json",
            # responseSchema הוסר אחרי בדיקת A/B ב-20 חברות: גרם לרגרסיה
            # חמורה (חברות עם עשרות חברות בת ידועות - אפי נכסים 123,
            # לייטסטון 137, מבטח שמיר 43 - חזרו עם 0-2 תוצאות תחת
            # responseSchema, בזמן שהפייפליין הישן/ללא-סכימה נתן את
            # המספרים המלאים). הסיבה המדויקת לא אובחנה במלואה - חשוד
            # שהאכיפה הקשיחה גורמת למודל "להסתפק" בתשובה ריקה תקנית
            # סכמטית כשהוא פחות בטוח, במקום לחפש לעומק במסמך ענק. שאר
            # התיקונים (מיזוג קבצים, quota_retries) לא הושפעו ונשארים.
            "thinkingConfig": {"thinkingLevel": "high"},  # איכות > מהירות
        },
    }

    last_error = None
    for attempt in range(max_retries):
        if _key_pool.all_daily_exhausted():
            raise GeminiQuotaExceededError(
                "כל המפתחות (GEMINI_API_KEY*) הגיעו למכסה יומית אמיתית (RPD) - "
                "לא ינוסו שוב עד מחר."
            )

        key = _key_pool.get_next_key()

        if key is None:
            wait = _key_pool.seconds_until_next_available()
            if wait == float("inf"):
                raise GeminiQuotaExceededError(
                    "כל המפתחות (GEMINI_API_KEY*) הגיעו למכסה יומית אמיתית (RPD) - "
                    "לא ינוסו שוב עד מחר."
                )
            if wait > 0:
                print(f"    כל המפתחות בקירור זמני - ממתין {wait:.0f}s "
                      f"(המפתח הקרוב ביותר להחלמה)...")
                time.sleep(wait)
            key = _key_pool.get_next_key()
            if key is None:  # מקרה קצה נדיר - עדיין לא זמין, נכשל הפעם
                raise GeminiQuotaExceededError(
                    "כל המפתחות (GEMINI_API_KEY*) עדיין בקירור אחרי המתנה."
                )

        _key_pool.limiter_for(key).acquire()  # RPM של המפתח הזה בלבד
        url = f"{GEMINI_BASE_URL}/{GEMINI_MODEL}:generateContent?key={key}"

        try:
            resp = requests.post(url, json=payload, verify=False, timeout=180)
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
            last_error = e
            wait = 5 * (2 ** attempt)
            print(f"    שגיאת רשת (ניסיון {attempt + 1}/{max_retries}) - ממתין {wait}s...")
            time.sleep(wait)
            continue

        if resp.status_code == 429 and "exceeded your current quota" in resp.text:
            # ניתן להבדיל בין RPD אמיתי לזמני-TPM/RPM: תגובת השגיאה של
            # Gemini כוללת details[].violations[].quotaId, ומכסות
            # יומיות תמיד מכילות "PerDay" בשם (למשל
            # "GenerateRequestsPerDayPerProjectPerModel-FreeTier") לעומת
            # "PerMinute" לזמני. מאומת מול תיעוד/דוגמאות אמיתיות של
            # Google - זו לא הנחה.
            is_daily = False
            try:
                violations = []
                for detail in resp.json().get("error", {}).get("details", []):
                    if detail.get("@type", "").endswith("QuotaFailure"):
                        violations.extend(detail.get("violations", []))
                is_daily = any("PerDay" in v.get("quotaId", "") for v in violations)
            except Exception:
                pass  # פורמט לא צפוי - מתייחסים כזמני (בטוח יותר, פשוט ננסה שוב)

            _key_pool.mark_exhausted(key, daily=is_daily)
            if is_daily:
                print(f"    מפתח ...{key[-4:]} - מכסה יומית אמיתית (RPD) נגמרה - "
                      f"לא ינוסה שוב היום (מפתחות אחרים לא נפגעים).")
            else:
                print(f"    מפתח ...{key[-4:]} הגיע למכסה - נכנס לקירור זמני של "
                      f"{GeminiKeyPool.COOLDOWN_SECONDS}s (RPM/TPM, מפתחות אחרים לא נפגעים).")
            last_error = requests.exceptions.HTTPError(f"429: {resp.text[:200]}")
            continue

        if resp.status_code == 503 or resp.status_code == 429:
            last_error = requests.exceptions.HTTPError(f"{resp.status_code}: {resp.text[:200]}")
            wait = 5 * (2 ** attempt)  # 5, 10, 20, 40 שניות
            print(f"    Gemini עמוס ({resp.status_code}, ניסיון {attempt + 1}/{max_retries}) - "
                  f"ממתין {wait}s...")
            time.sleep(wait)
            continue

        resp.raise_for_status()
        break
    else:
        if last_error is not None and "exceeded your current quota" in str(last_error):
            raise GeminiQuotaExceededError(str(last_error))
        raise last_error

    data = resp.json()

    try:
        text = data["candidates"][0]["content"]["parts"][0]["text"]
    except (KeyError, IndexError) as e:
        raise RuntimeError(f"תגובת Gemini לא בפורמט צפוי: {json.dumps(data)[:500]}") from e

    # אמצעי בטיחות: גם עם responseMimeType=json לפעמים חוזרים code fences
    text = re.sub(r"^```json\s*|\s*```$", "", text.strip())

    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        print("=== JSON גולמי שהתקבל (לצורך אבחון) ===", file=sys.stderr)
        print(text, file=sys.stderr)
        print("=== סוף הטקסט הגולמי ===", file=sys.stderr)
        raise


def sql_escape(value):
    if value is None:
        return "NULL"
    if isinstance(value, (int, float)):
        return str(value)
    escaped = str(value).replace("'", "''")
    return f"'{escaped}'"


def build_insert_statements(
    company_legal_id: str, report_id: str, source_type: str, extracted: dict
) -> list[str]:
    statements = []
    as_of = extracted.get("as_of_date")
    doc_type = extracted.get("document_type", "unknown")

    for sub in extracted.get("subsidiaries", []):
        statements.append(
            "INSERT INTO ownership_snapshots "
            "(company_legal_id, report_id, source_type, document_type, as_of_date, "
            "subsidiary_name, ownership_pct, parent_name, page_reference, note, extracted_at) "
            "VALUES ("
            f"{sql_escape(company_legal_id)}, {sql_escape(report_id)}, "
            f"{sql_escape(source_type)}, {sql_escape(doc_type)}, {sql_escape(as_of)}, "
            f"{sql_escape(sub.get('name'))}, {sql_escape(sub.get('ownership_pct'))}, "
            f"{sql_escape(sub.get('parent'))}, {sql_escape(sub.get('page_reference'))}, "
            f"{sql_escape(sub.get('note'))}, {sql_escape(datetime.utcnow().isoformat())}"
            ");"
        )

    for ev in extracted.get("change_events", []):
        statements.append(
            "INSERT INTO ownership_change_events "
            "(company_legal_id, report_id, source_type, company_name, "
            "ownership_pct_after, event_date, event_description, page_reference, extracted_at) "
            "VALUES ("
            f"{sql_escape(company_legal_id)}, {sql_escape(report_id)}, {sql_escape(source_type)}, "
            f"{sql_escape(ev.get('company'))}, {sql_escape(ev.get('ownership_pct_after'))}, "
            f"{sql_escape(ev.get('event_date'))}, {sql_escape(ev.get('event_description'))}, "
            f"{sql_escape(ev.get('page_reference'))}, {sql_escape(datetime.utcnow().isoformat())}"
            ");"
        )

    return statements


def write_to_d1(statements: list[str]) -> None:
    if not statements:
        print("אין שורות לכתיבה - דילוג על D1.")
        return

    sql_path = "/tmp/payload.sql"
    with open(sql_path, "w", encoding="utf-8") as f:
        f.write("\n".join(statements))

    # --remote הוא חובה: בלי זה עובדים על עותק מקומי ריק (ריק תמיד).
    cmd = [
        "npx",
        "wrangler",
        "d1",
        "execute",
        D1_DATABASE_NAME,
        "--remote",
        "--file",
        sql_path,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print("שגיאת wrangler:", result.stderr, file=sys.stderr)
        sys.exit(1)
    print(f"נכתבו {len(statements)} שורות ל-D1.")


def already_processed(report_id: str) -> bool:
    """בדיקת אידמפוטנטיות מול טבלת processed_reports לפני עיבוד חוזר."""
    cmd = [
        "npx",
        "wrangler",
        "d1",
        "execute",
        D1_DATABASE_NAME,
        "--remote",
        "--command",
        f"SELECT 1 FROM processed_reports WHERE report_id = '{report_id}' LIMIT 1;",
        "--json",
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        return False
    try:
        parsed = json.loads(result.stdout)
        return len(parsed[0]["results"]) > 0
    except (json.JSONDecodeError, KeyError, IndexError):
        return False


def mark_processed(report_id: str, company_legal_id: str) -> None:
    statements = [
        "INSERT INTO processed_reports (report_id, company_legal_id, processed_at) "
        f"VALUES ({sql_escape(report_id)}, {sql_escape(company_legal_id)}, "
        f"{sql_escape(datetime.utcnow().isoformat())});"
    ]
    write_to_d1(statements)


def process_document(
    pdf_url: str, report_id: str, company_legal_id: str, source_type: str
) -> None:
    if already_processed(report_id):
        print(f"דוח {report_id} כבר עובד - דילוג.")
        return

    print(f"מוריד {pdf_url} ...")
    pdf_bytes = download_pdf(pdf_url)

    print("שולח ל-Gemini לחילוץ ...")
    extracted = call_gemini_extraction(pdf_bytes, filename_hint=pdf_url.split("/")[-1])

    statements = build_insert_statements(company_legal_id, report_id, source_type, extracted)
    write_to_d1(statements)
    mark_processed(report_id, company_legal_id)


def save_extraction_json(
    company_legal_id: str, report_id: str, source_type: str, extracted: dict,
    out_path: str = "private_subsidiaries.jsonl",
    report_publish_date: str | None = None,
) -> None:
    """שומר את תוצאת החילוץ לקובץ JSONL מקומי - שורה אחת לכל רשומה,
    append בלבד (לא קריאה-שכתוב-מלא). זו נקודה קריטית: הפורמט הישן
    (JSON array) דרש לקרוא את כל הקובץ, להוסיף רשומה, ולכתוב הכל מחדש
    בכל שמירה - אם התהליך נהרג באמצע (עצירה ידנית, timeout) בדיוק
    בזמן הכתיבה המחדש, כל הקובץ ההיסטורי היה עלול להיפגם. ב-JSONL,
    היפגמות אפשרית רק בשורה האחרונה שנכתבה בפועל - שאר ההיסטוריה
    תמיד שלמה, כי כל שורה נכתבת פעם אחת ולא נוגעים בשורות קודמות.

    report_publish_date הוא תאריך פרסום הדוח עצמו (מ-Maya) - שונה מ-
    event_date שבתוך כל change_event (תאריך העסקה בפועל, כפי שמופיע
    בטקסט). נשמר ברמת הרשומה למעקב כרונולוגי, גם כשאירועים בודדים
    חסרי תאריך מדויק."""
    record = {
        "parent_hp": company_legal_id,
        "parent_company_name": None,  # למלא אם ידוע; לא חובה לחילוץ עצמו
        "report_id": report_id,
        "report_publish_date": report_publish_date,
        "schema_version": CURRENT_SCHEMA_VERSION,
        "model": GEMINI_MODEL,  # איזה מודל ביצע את החילוץ הזה - ראה
        # GEMINI_MODEL למעלה. חשוב כשרצים כמה תורים במקביל (3.6 ו-3.5)
        # על אותן חברות - כדי לדעת אחר כך מקור כל רשומה.
        "source_type": source_type,
        "report_format": extracted.get("report_format"),
        "as_of_date": extracted.get("as_of_date"),
        "document_type": extracted.get("document_type"),
        "subsidiaries": extracted.get("subsidiaries", []),
        "change_events": extracted.get("change_events", []),
    }

    with _file_write_lock:  # מונע שזירת שורות בין threads, לא יותר מזה
        with open(out_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
            f.flush()
            os.fsync(f.fileno())  # מכריח כתיבה בפועל לדיסק, לא רק לבאפר

    print(f"נוסף ל-{out_path}")


def load_extraction_jsonl(path: str = "private_subsidiaries.jsonl") -> list[dict]:
    """קורא קובץ JSONL לרשימת רשומות. אם השורה האחרונה נפגמה (נכתבה
    בחלקה בגלל עצירה באמצע), מדלגים עליה ומזהירים - לא מפילים את כל
    הקריאה בגללה, וכל השורות התקינות שלפניה עדיין נטענות בהצלחה."""
    records = []
    if not os.path.exists(path):
        return records
    with open(path, encoding="utf-8") as f:
        for i, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                print(f"אזהרה: שורה {i} ב-{path} פגומה (כנראה נכתבה בחלקה) - מדלג עליה.")
    return records


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--pdf-url", required=True, help="קישור ישיר ל-PDF ב-Maya")
    parser.add_argument("--report-id", required=True, help="אסמכתא/מזהה ייחודי של הדוח")
    parser.add_argument("--company-legal-id", required=True, help="מספר ברשם (ח.פ / entity id)")
    parser.add_argument(
        "--source-type",
        required=True,
        choices=["quarterly_report", "annual_report", "investor_presentation"],
    )
    args = parser.parse_args()

    process_document(
        pdf_url=args.pdf_url,
        report_id=args.report_id,
        company_legal_id=args.company_legal_id,
        source_type=args.source_type,
    )

# הערת מודל: נכון לאוגוסט 2026, gemini-2.5-flash כבר לא זמין למשתמשים חדשים -
# גוגל מפנים ל-gemini-3.6-flash. שמות המודלים אצל גוגל מתחלפים בתדירות גבוהה
# יחסית; אם תתקבל שוב שגיאת 404 עם "no longer available", זה סימן לבדוק
# מחדש דרך GET https://generativelanguage.googleapis.com/v1beta/models?key=...
# ולעדכן את הקבוע כאן בהתאם - לא להניח ששם המודל קבוע לאורך זמן.
#
# הערת מכסות (אומת מול ה-dashboard האמיתי ב-Google AI Studio, לא מבלוגים
# חיצוניים - הם התבררו כלא מדויקים!): ל-gemini-3.6-flash יש בחשבון הזה רק
# 20 RPD (בקשות ביום) - מגבלה נמוכה מאוד, לא ריאלית לריצה בקנה מידה. עברנו
# ל-gemini-3.5-flash-lite שנותן 500 RPD באותו חשבון. לפני שינוי מודל נוסף
# בעתיד - בדוק תמיד את הלשונית "Rate Limits" ב-AI Studio, לא מקורות חיצוניים.

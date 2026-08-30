"""
match_subsidiary_names.py

שלב 2 בחיבור לעץ הבעלות: לכל חברת בת שחילצנו (בתוך subsidiaries/
change_events), מחפש התאמה מדויקת (לא fuzzy) מול הרישום, דרך ה-API
החי של האתר (/api/search) - לא ניגשים ישירות ל-D1, נשענים על מה שכבר
עובד בפועל באתר.

הנרמול זהה בדיוק לזה שב-functions/api/search.js (normalize()) - כדי
שהתוצאה תהיה עקבית עם מה שהאתר עצמו כבר "יודע". התאמה מתקבלת רק אם,
אחרי נרמול, יש בדיוק תוצאה אחת שהשם שלה זהה במלואו לשם המבוקש - לא
substring, לא fuzzy. אם 0 או יותר מהתאמה אחת נמצאות - נשאר ללא מיפוי
(ל-UI: "?" גדולה / חיפוש טרי בלחיצה, כמו שסוכם).

הרצה:
    py match_subsidiary_names.py
"""

import json
import re
import time

import requests

SEARCH_API = "https://revach.pages.dev/api/search"
SLEEP_BETWEEN_CALLS = 1.5  # 0.3s גרם ל-11% כישלון (500) על /api/search החי -
# נראה כמו עומס אמיתי על D1 (שאילתות LIKE כבדות, ללא אינדקס יעיל למספר
# מילים גבוה) שהקצב שלנו החמיר. מאט משמעותית כדי לא להעמיס על משתמשים
# אמיתיים באתר החי.


def normalize(s: str | None) -> str:
    """זהה בדיוק ל-normalize() ב-functions/api/search.js - לא לשנות
    כאן בלי לשנות שם, אחרת התוצאות יסטו מהאתר עצמו."""
    if not s:
        return ""
    s = re.sub(r"\([^)]*\)", " ", s)
    s = re.sub(r"[^\u05d0-\u05ea0-9A-Za-z ]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    s = re.sub(r"[יו]", "", s)
    return s


class SearchTemporaryError(Exception):
    """נזרק כשהחיפוש עצמו נכשל טכנית (500, timeout, רשת) - לא כשהחיפוש
    הצליח ופשוט לא מצא התאמה. ההבחנה קריטית: רק 'הצליח, אין התאמה'
    נשמר ל-cache לצמיתות. 'נכשל טכנית' לא נשמר בכלל - ינוסה שוב
    בהרצה הבאה (או בניסיון החוזר כאן), במקום להיחשב 'אין התאמה' לנצח."""
    pass


def levenshtein(a: str, b: str) -> int:
    """מרחק עריכה קלאסי - ללא תלות חיצונית (מחרוזות קצרות, לא בעיית ביצועים)."""
    if a == b:
        return 0
    if len(a) < len(b):
        a, b = b, a
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i] + [0] * len(b)
        for j, cb in enumerate(b, 1):
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb))
        prev = cur
    return prev[-1]


def is_close(a_norm: str, b_norm: str, max_ratio: float = 0.15) -> bool:
    """'קרוב' = מרחק עריכה יחסית קטן לאורך המחרוזת הארוכה (לא זהה,
    זה כבר טופל בנפרד). סף 15% - תפור לדוגמאות אמיתיות שראינו (למשל
    'בטחון' מול 'ביטחון' - הבדל של אות אחת מתוך ~20, כ-5%)."""
    if not a_norm or not b_norm or a_norm == b_norm:
        return False
    dist = levenshtein(a_norm, b_norm)
    return dist / max(len(a_norm), len(b_norm)) <= max_ratio


SUFFIX_TOKENS = {"בע", "בעמ", "מ"}  # שאריות "בע\"מ" אחרי normalize - כמעט
# בכל שם ישראלי, לא תורמות שום סינון אמיתי לחיפוש, רק מכבידות את השאילתה


def build_search_query(name: str, min_words: int = 3, min_chars: int = 10) -> str:
    """מקצר את השם לשליחה בפועל ל-API - פחות מילים = שאילתת LIKE קלה
    יותר ב-D1 (כל מילה = תנאי AND נפרד, ראינו בקוד search.js שזה עלול
    לגרום ל-500 על שמות ארוכים/מרובי-מילים). מסיר קודם סיומת \"בע\"מ\"
    מהסוף בלבד (לא מכל מופע - אחרת שם שמתחיל בראשי-תיבות כמו \"מ.
    מלינדה\" היה מאבד את ה-\"מ\" האמיתי שלו), ואז לוקח לפחות min_words
    מילים וגם לפחות min_chars תווים - הגדול מביניהם קובע. ההשוואה
    המדויקת בהמשך עדיין נעשית מול השם המלא - זה משפיע רק על מה שנשלח
    כ-q, לא על דיוק ההתאמה."""
    words = [w for w in normalize(name).split(" ") if w]
    while words and words[-1] in SUFFIX_TOKENS:
        words.pop()
    if not words:
        return normalize(name)  # שם היה כולו סיומת, לא סביר - fallback

    chosen = []
    total_len = 0
    for w in words:
        chosen.append(w)
        total_len += len(w)
        if len(chosen) >= min_words and total_len >= min_chars:
            break
    return " ".join(chosen)


def find_match(name: str, max_retries: int = 3) -> dict | None:
    """מחפש מול /api/search?q=<name>. מחזיר {kind, id, name, confidence}:
    - confidence="exact": התאמה מדויקת יחידה (כמו קודם).
    - confidence="fuzzy": אין התאמה מדויקת, אבל יש בדיוק מועמד קרוב אחד
      (מרחק עריכה קטן) - לא שניים ומעלה (אז לא ודאי מספיק, לא מחברים).
    None אם אין שום מועמד סביר. זורק SearchTemporaryError בכישלון טכני
    (ראה find_exact_match הישן - אותו מנגנון retry בדיוק)."""
    if not name or len(name.strip()) < 2:
        return None

    query = build_search_query(name)

    last_error = None
    for attempt in range(max_retries):
        try:
            resp = requests.get(SEARCH_API, params={"q": query}, timeout=15)
            resp.raise_for_status()
            data = resp.json()
            break
        except Exception as e:
            last_error = e
            wait = 2 * (attempt + 1)
            print(f"  שגיאת חיפוש עבור '{name}' (שאילתה: '{query}', ניסיון {attempt + 1}/{max_retries}) - "
                  f"ממתין {wait}s: {e}")
            time.sleep(wait)
    else:
        raise SearchTemporaryError(f"נכשל אחרי {max_retries} ניסיונות: {last_error}")

    target_norm = normalize(name)
    exact_matches, fuzzy_matches = [], []
    for r in data.get("results", []):
        r_norm = normalize(r.get("name"))
        if r_norm == target_norm:
            exact_matches.append(r)
        elif is_close(target_norm, r_norm):
            fuzzy_matches.append(r)

    if len(exact_matches) == 1:
        m = exact_matches[0]
        return {"kind": m.get("kind"), "id": m.get("id"), "name": m.get("name"),
                "confidence": "exact"}
    if len(exact_matches) == 0 and len(fuzzy_matches) == 1:
        m = fuzzy_matches[0]
        return {"kind": m.get("kind"), "id": m.get("id"), "name": m.get("name"),
                "confidence": "fuzzy"}
    return None  # 0 מועמדים, או יותר מדי (לא ודאי) - לא מחברים בכלל


# נשמר לשם תאימות לאחור - קריאות קיימות ל-find_exact_match ימשיכו לעבוד
def find_exact_match(name: str, max_retries: int = 3) -> dict | None:
    return find_match(name, max_retries=max_retries)


def collect_subsidiary_names(in_path: str) -> set[str]:
    """אוסף את כל שמות החברות הבת הייחודיים מכל הרשומות (גם
    subsidiaries וגם change_events) - כדי לחפש כל שם פעם אחת בלבד,
    לא פעם לכל הופעה."""
    names = set()
    with open(in_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            for s in rec.get("subsidiaries", []):
                if s.get("name"):
                    names.add(s["name"])
            for e in rec.get("change_events", []):
                if e.get("company"):
                    names.add(e["company"])
    return names


def build_name_to_match_cache(
    names: set[str], cache_path: str = "subsidiary_name_matches.json"
) -> dict[str, dict | None]:
    """בונה (או טוען מ-cache) מיפוי שם->התאמה. שמור לקובץ כדי לא
    לחפש שוב שמות שכבר נבדקו בהרצות קודמות. שומר אחרי כל שם בודד
    (לא רק כל כמה שמות) - ההרצה יכולה לקחת עשרות דקות, ואם היא נעצרת
    (ידני, timeout, ניתוק) באמצע, לא רוצים לאבד יותר משם בודד. כתיבה
    אטומית (temp+replace) כדי שגם קטיעה בדיוק באמצע כתיבה לא תשאיר
    קובץ פגום."""
    import os

    cache: dict[str, dict | None] = {}
    if os.path.exists(cache_path):
        with open(cache_path, encoding="utf-8") as f:
            cache = json.load(f)
        print(f"נטען cache קיים: {len(cache)} שמות כבר נבדקו.")

    new_names = [n for n in names if n not in cache]
    print(f"סה\"כ {len(names)} שמות ייחודיים, {len(new_names)} חדשים לבדיקה.")

    n_matched, n_fuzzy, n_errors = 0, 0, 0
    tmp_path = cache_path + ".tmp"
    for i, name in enumerate(new_names, 1):
        try:
            match = find_match(name)
        except SearchTemporaryError as e:
            n_errors += 1
            print(f"[{i}/{len(new_names)}] '{name}' -> {e} - מדלג בלי לשמור (ינוסה שוב)")
            continue  # לא שומרים - לא מתקדם ב-cache, ינוסה בהרצה הבאה

        cache[name] = match
        if match and match["confidence"] == "exact":
            n_matched += 1
            print(f"[{i}/{len(new_names)}] '{name}' -> [מדויק] {match['kind']} {match['id']} ({match['name']})")
        elif match and match["confidence"] == "fuzzy":
            n_fuzzy += 1
            print(f"[{i}/{len(new_names)}] '{name}' -> [קרוב] {match['kind']} {match['id']} ({match['name']})")
        else:
            print(f"[{i}/{len(new_names)}] '{name}' -> אין התאמה")

        # שמירה אחרי כל שם - לא מצטברת, לא מאבדים יותר משם בודד בקטיעה
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, cache_path)

        time.sleep(SLEEP_BETWEEN_CALLS)

    print(f"\nסה\"כ: {n_matched} מדויק, {n_fuzzy} קרוב, "
          f"{n_errors} נכשלו טכנית (ינוסו בהרצה הבאה)" if new_names else "אין שמות חדשים.")
    return cache


if __name__ == "__main__":
    names = collect_subsidiary_names("private_subsidiaries.jsonl")
    build_name_to_match_cache(names)

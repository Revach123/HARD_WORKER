"""
match_subsidiary_names.py  (HARD_WORKER)

שלב 2 בחיבור לעץ הבעלות: לכל חברת-בת שחולצה (private_subsidiaries.jsonl),
מחפש התאמה מול הרישום. שני טיירים:

  exact  - דרך /api/match (seek שוויון על name_norm, זול: ~6 rows לשם).
           תמיד, בלי תקרה.
  fuzzy  - דרך /api/search (FTS + levenshtein), רק על שמות ש-exact החזיר
           עליהם null, ועד FUZZY_PER_RUN_LIMIT קריאות להרצה.

מטמון (subsidiary_name_matches.json): נכתב אך ורק כשיש תשובה *סופית* -
exact שנמצא, או exact-null שעליו כבר הורץ fuzzy. שם ש-exact נכשל עליו
ו-fuzzy נדחה (התקרה נגמרה) *לא* נשמר -> ינוסה בהרצה הבאה. כך התקרה
מתנקזת על פני הרצות בלי לאבד אף שם. רשומות ישנות במטמון (כולל null-ים
שכבר עברו exact+fuzzy אצל המצ'ר הקודם) נחשבות "נבדק" ולא נבדקות שוב.

גישה: דורש env MATCH_KEY (זהה ל-secret ב-Cloudflare). נשלח כ-header
X-Match-Key ומדלג על ה-rate-limit של guard() בשני ה-endpoints. בלי
המפתח - נעצר מיד ברעש (בלעדיו כל קריאה שנייה הייתה 428 והריצה הייתה
"נראית" עובדת אך נכשלת על הרוב).

הרצה:
    py match_subsidiary_names.py             # מצ'ר רגיל
    py match_subsidiary_names.py --validate  # שחזור מול validation_sample.json
"""

import json
import os
import re
import sys
import time

import requests

BASE = "https://revach.pages.dev"
MATCH_API = BASE + "/api/match"     # exact (זול)
SEARCH_API = BASE + "/api/search"   # fuzzy fallback בלבד

MATCH_KEY = os.environ.get("MATCH_KEY", "")
if not MATCH_KEY:
    sys.exit("MATCH_KEY חסר בסביבה - הגדר את ה-secret לפני הרצה (fail loud).")

HEADERS = {"Origin": BASE, "Referer": BASE + "/", "X-Match-Key": MATCH_KEY}

FUZZY_PER_RUN_LIMIT = 400   # תקרת קריאות fuzzy (=/api/search) להרצה. exact ללא תקרה.
SLEEP_FUZZY = 0.3           # /api/search כבד יותר; האטה קלה. exact ללא sleep.
CACHE_PATH = "subsidiary_name_matches.json"


# -- normalize two-version -- MUST stay identical across 6 files (see warning)
# Background (2026-09-07): old normalize turned quote/hyphen (" ' * ' -) into a
# SPACE, splitting a word in two. Fix: two versions. A deletes quote/hyphen
# (merges word); B (old) turns them to space. D1: name_norm = B, name_norm_a = A.
# 6 files must match byte-for-byte: functions/api/match.js, functions/api/
# search.js, scripts/registry/load_companies.py / load_partnerships.py /
# load_associations.py, and this file. /api/match seeks name_norm_a then name_norm.
def normalize_a(s):
    if not s:
        return ""
    s = re.sub(r"\([^)]*\)", " ", s)
    s = re.sub(r"[\"'\u05f4\u05f3\-]", "", s)
    s = re.sub(r"[^\u05d0-\u05ea0-9A-Za-z ]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    s = re.sub(r"[\u05d9\u05d5]", "", s)
    return s


def normalize_b(s):
    if not s:
        return ""
    s = re.sub(r"\([^)]*\)", " ", s)
    s = re.sub(r"[^\u05d0-\u05ea0-9A-Za-z ]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    s = re.sub(r"[\u05d9\u05d5]", "", s)
    return s


# normalize() = B (used by build_search_query / is_close for the fuzzy tier).
normalize = normalize_b


class SearchTemporaryError(Exception):
    """כשל טכני (500/timeout/רשת) - להבדיל מ'הצליח, אין התאמה'. רק
    'הצליח, אין התאמה' נשמר; כשל טכני לא נשמר וינוסה שוב."""
    pass


def levenshtein(a, b):
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


def is_close(a_norm, b_norm, max_ratio=0.15):
    if not a_norm or not b_norm or a_norm == b_norm:
        return False
    return levenshtein(a_norm, b_norm) / max(len(a_norm), len(b_norm)) <= max_ratio


SUFFIX_TOKENS = {"בע", "בעמ", "מ"}


def build_search_query(name, min_words=3, min_chars=10):
    """מקצר את השם לשליחה ל-/api/search - פחות מילים = שאילתה קלה יותר.
    ההשוואה המדויקת עדיין מול השם המלא."""
    words = [w for w in normalize(name).split(" ") if w]
    while words and words[-1] in SUFFIX_TOKENS:
        words.pop()
    if not words:
        return normalize(name)
    chosen, total = [], 0
    for w in words:
        chosen.append(w)
        total += len(w)
        if len(chosen) >= min_words and total >= min_chars:
            break
    return " ".join(chosen)


def _get_json(url, params, max_retries=3):
    """GET עם retry על כשל טכני בלבד. זורק SearchTemporaryError אחרי max."""
    last = None
    for attempt in range(max_retries):
        try:
            r = requests.get(url, params=params, headers=HEADERS, timeout=15)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            last = e
            wait = 2 * (attempt + 1)
            print(f"    כשל ({url.rsplit('/', 1)[-1]}, ניסיון {attempt + 1}/{max_retries}) "
                  f"- ממתין {wait}s: {e}")
            time.sleep(wait)
    raise SearchTemporaryError(f"{url} נכשל אחרי {max_retries} ניסיונות: {last}")


def match_exact(name):
    """exact דרך /api/match. שולח שם גולמי (נרמול server-side).
    מחזיר {kind,id,name,confidence:'exact'} או None."""
    data = _get_json(MATCH_API, {"q": name})
    return (data.get("matches") or {}).get(name)


def match_fuzzy(name):
    """fuzzy דרך /api/search: מביא מועמדים, מסנן ב-levenshtein. מחזיר
    התאמת fuzzy יחידה או None. exact כבר נשלל ע"י /api/match, לכן כאן
    מעניין רק מועמד קרוב *יחיד* (שניים+ = לא ודאי, לא מחברים)."""
    if not name or len(name.strip()) < 2:
        return None
    data = _get_json(SEARCH_API, {"q": build_search_query(name)})
    target = normalize(name)
    fuzzy = []
    for r in data.get("results", []):
        rn = normalize(r.get("name"))
        if rn != target and is_close(target, rn):
            fuzzy.append(r)
    if len(fuzzy) == 1:
        m = fuzzy[0]
        return {"kind": m.get("kind"), "id": m.get("id"),
                "name": m.get("name"), "confidence": "fuzzy"}
    return None


def collect_subsidiary_names(in_path):
    """כל שמות חברות-הבת הייחודיים (subsidiaries + change_events)."""
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


def build_name_to_match_cache(names, cache_path=CACHE_PATH):
    cache = {}
    if os.path.exists(cache_path):
        with open(cache_path, encoding="utf-8") as f:
            cache = json.load(f)
        print(f"נטען cache: {len(cache)} שמות כבר נבדקו.")

    new_names = [n for n in names if n not in cache]
    print(f"{len(names)} שמות ייחודיים, {len(new_names)} חדשים לבדיקה.")

    n_exact = n_fuzzy = n_none = n_err = n_deferred = 0
    fuzzy_used = 0
    tmp = cache_path + ".tmp"

    def save():
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False, indent=2)
        os.replace(tmp, cache_path)

    for i, name in enumerate(new_names, 1):
        # --- טייר exact (תמיד) ---
        try:
            m = match_exact(name)
        except SearchTemporaryError as e:
            n_err += 1
            print(f"[{i}/{len(new_names)}] '{name}' -> exact כשל טכני, מדלג (ינוסה שוב): {e}")
            continue

        if m is not None:
            cache[name] = m
            n_exact += 1
            print(f"[{i}/{len(new_names)}] '{name}' -> [מדויק] {m['kind']} {m['id']} ({m['name']})")
            save()
            continue

        # --- exact=null: טייר fuzzy, בכפוף לתקרה ---
        if fuzzy_used >= FUZZY_PER_RUN_LIMIT:
            n_deferred += 1
            # לא שומרים -> ינוסה בהרצה הבאה (התקרה מתנקזת על פני הרצות)
            continue

        try:
            fm = match_fuzzy(name)
        except SearchTemporaryError as e:
            n_err += 1
            print(f"[{i}/{len(new_names)}] '{name}' -> fuzzy כשל טכני, מדלג (ינוסה שוב): {e}")
            continue

        fuzzy_used += 1
        cache[name] = fm  # סופי: exact+fuzzy מוצו (fm = התאמה קרובה או None)
        if fm:
            n_fuzzy += 1
            print(f"[{i}/{len(new_names)}] '{name}' -> [קרוב] {fm['kind']} {fm['id']} ({fm['name']})")
        else:
            n_none += 1
            print(f"[{i}/{len(new_names)}] '{name}' -> אין התאמה")
        save()
        time.sleep(SLEEP_FUZZY)

    print(f"\nסיכום: {n_exact} מדויק, {n_fuzzy} קרוב, {n_none} אין התאמה, "
          f"{n_deferred} נדחו (תקרת fuzzy={FUZZY_PER_RUN_LIMIT}), "
          f"{n_err} כשלים טכניים. fuzzy_used={fuzzy_used}."
          if new_names else "אין שמות חדשים.")
    return cache


def validate(sample_path="validation_sample.json"):
    """מוודא ש-/api/match משחזר התאמות exact ידועות. מסווג:
      same  - אותו kind+id (תקין).
      null  - /api/match החזיר None. קביל: לרוב דו-משמעות אמיתית שה-
              מצ'ר הישן פספס (הוא בדק exact רק בתוך 50 תוצאות FTS של
              שאילתה מקוצרת; /api/match בודק את *כל* השורות עם name_norm
              שווה). מדווח לעיון, לא מפיל.
      diff  - kind/id *אחר* -> כשל קשה (חיבור שגוי, לא לחבר עד שנקי)."""
    sample = json.load(open(sample_path, encoding="utf-8"))
    same = null = diff = 0
    diffs, nulls = [], []
    for name, exp in sample.items():
        try:
            m = match_exact(name)
        except SearchTemporaryError as e:
            sys.exit(f"אימות נעצר - כשל טכני על '{name}': {e} (בדוק deploy/index/MATCH_KEY).")
        if m and m.get("kind") == exp["kind"] and str(m.get("id")) == str(exp["id"]):
            same += 1
        elif m is None:
            null += 1
            nulls.append(name)
        else:
            diff += 1
            diffs.append((name, exp, m))

    print(f"אימות: {same} תואמים, {null} הפכו ל-null (קביל, לעיון), {diff} סטו ל-id אחר.")
    for name, exp, got in diffs[:30]:
        print(f"  [DIFF] '{name}': ציפינו {exp['kind']} {exp['id']}, קיבלנו {got.get('kind')} {got.get('id')}")
    for name in nulls[:30]:
        print(f"  [null] '{name}'")
    if diff:
        sys.exit(f"אימות נכשל: {diff} סטיות ל-id אחר - לא לחבר את המצ'ר עד שזה נקי.")
    print("אימות עבר: אפס סטיות ל-id שגוי. "
          f"({null} null לעיון - צפוי שחלקם דו-משמעות אמיתית).")


if __name__ == "__main__":
    if "--validate" in sys.argv:
        validate()
    else:
        names = collect_subsidiary_names("private_subsidiaries.jsonl")
        build_name_to_match_cache(names)

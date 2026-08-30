# -*- coding: utf-8 -*-
"""
ליבת ת077 - מצבת החזקות בעלי עניין.
scripts/company_ownership/t077_core.py

מכיל: פיד הדוחות, הורדה, ופרסינג. הרצה דרך t077_backfill.py / t077_daily.py.

מספרי הזיהוי נשמרים כפי שהם, יחד עם סוג המספר כפי שהוצהר בדוח.
שדה hp מאוכלס רק כשסוג המספר הוא 'מספר ברשם החברות' - כך שהצלבות
במורד הזרם לעולם לא יטעו ת.ז. בח.פ.

שים לב: הפלט הגולמי מכיל תעודות זהות. אינו מיועד לפרסום.
"""
import csv
import hashlib
import hmac
import io
import json
import os
import re
import time

import pandas as pd
import requests
import urllib3

VERIFY_SSL = os.environ.get("CI") is not None
if not VERIFY_SSL:
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

MAYA = "https://maya.tase.co.il"
FILES = "https://mayafiles.tase.co.il/"
FEED = MAYA + "/api/v1/reports/companies"
PAGE = (MAYA + "/he/reports/companies?fromDate=2025-07-16&toDate=2026-07-16"
        "&isPriority=false&isTradeHalt=false&by=company&formId=%D7%AA077")
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36")

# ============================================================
# נתיבים - יחסית לשורש ה-repo, לא ל-cwd.
# Thonny מריץ מתיקיית הסקריפט; Actions מריץ מהשורש.
# ============================================================
def _repo_root():
    starts = [os.getcwd()]
    try:
        starts.insert(0, os.path.dirname(os.path.abspath(__file__)))
    except NameError:
        pass
    for start in starts:
        d = start
        for _ in range(6):
            if os.path.isdir(os.path.join(d, ".git")) or \
               os.path.isdir(os.path.join(d, "data")):
                return d
            parent = os.path.dirname(d)
            if parent == d:
                break
            d = parent
    return os.getcwd()


ROOT = os.environ.get("REPO_ROOT") or _repo_root()

# ההפרדה לפי רגישות, לא לפי תפקיד:
#   RAW_DIR  - דוחות מפוענחים עם ת.ז. -> .gitignore, מקומי בלבד
#   PUB_DIR  - אינדקס (מטא בלבד) + מפת תאגידים -> ב-repo
RAW_DIR = os.environ.get("T077_RAW") or os.path.join(ROOT, "private", "t077")
PUB_DIR = os.environ.get("T077_PUB") or os.path.join(ROOT, "data", "holdings")

INDEX_PATH = os.path.join(PUB_DIR, "t077-index.json")
MAP_PATH = os.path.join(PUB_DIR, "holder-hp.json")
# קצות בעלות ישירים: ח.פ של החברה המדווחת -> ח.פ מוצהר של כל מחזיק תאגידי.
# שני המספרים מוצהרים באותו דוח, ולכן זהו מקור אמת ללא התאמת שמות כלל.
EDGES_PATH = os.path.join(PUB_DIR, "t077-edges.json")

# פלט בדיקה פרטי: כל המחזיקים עם ת.ז/ח.פ בגלוי, לבדיקת נכונות ידנית
# באקסל. תחת RAW_DIR (private/, gitignored) — לעולם לא עולה לאתר.
HOLDERS_CSV = os.path.join(RAW_DIR, "holders-full.csv")

# רק זיהוי תאגידי. כל השאר - החוצה.
CORPORATE_ID = "מספר ברשם החברות בישראל"


def looks_like_hp(num):
    """הגדרה יחידה, במקום אחד, לכל החלטת hp-או-סוד בקובץ הזה.

    ח.פ אמיתי (חברה/שותפות/עמותה/אגודה שיתופית) הוא תמיד 9 ספרות
    שמתחילות ב-5. זו ההבחנה שקובעת הכל מהרגע הראשון: מספר שעובר אותה
    הוא ח.פ - מתפרסם בגלוי, ניתן להצלבה מול הרשם. מספר שלא עובר -
    בין אם זו ת.ז., ח.פ שגוי, או כל דבר אחר - הוא סודי כברירת מחדל
    ואסור שיגיע לקובץ ציבורי בשום צורה (לא כ-hp, לא בתוך שם, שום דבר).

    משמש גם עבור מספר המחזיק (parse_holder_card) וגם עבור מספר החברה
    המדווחת עצמה (parse_t077) - שני המקומות שבהם מספר גולמי נכנס
    למערכת. ללא הגדרה יחידה משותפת, מספיק ששני מקומות ינסחו את אותה
    בדיקה מעט אחרת כדי שאחד מהם ידלוף (זה בדיוק מה שקרה: המחזיק נבדק,
    החברה המדווחת - לא).
    """
    return bool(num) and num.isdigit() and len(num) == 9 and num[0] == "5"


def scrub_name(s):
    """מסיר מתוך שם תצוגה כל ריצה עצמאית של 9 ספרות שאינה ח.פ (כלומר
    לא מתחילה ב-5). שם לעולם לא אמור להכיל ת.ז., אבל שום דבר לא מונע
    ממדווח להדביק אחת בטעות לתוך שדה השם - ושדה שם אינו עובר את בדיקת
    ה-hp כי הוא לא אמור מלכתחילה להיות מספר. ריצה של 9 ספרות שכן
    מתחילה ב-5 (כלומר ח.פ אמיתי, לעיתים חלק לגיטימי מהשם) נשארת."""
    if not s:
        return s
    return re.sub(r"(?<!\d)(?!5)\d{9}(?!\d)", "", str(s)).strip()

# ============================================================
# מזהה יחיד אטום (pid)
#
# המטרה: לחבר את אותו אדם בין חברות, בלי לפרסם את הת.ז. שלו. הת.ז. היא
# מפתח החיבור היחיד האמין - שמות נכתבים אחרת בין דוחות, ולכן התאמת שמות
# תיצור מיזוג של שני אנשים שונים או פיצול של אדם אחד.
#
# למה HMAC עם מלח סודי ולא sha256 פשוט:
#   ת.ז. ישראלית היא 9 ספרות עם ספרת ביקורת - כ-10^8 ערכים תקינים בלבד.
#   כרטיס מסך מחשב מיליארדי גיבובי sha256 בשנייה. כלומר sha256(ת.ז.) הוא
#   הפיך במלואו תוך פחות משנייה עבור *כל* המספרים. פרסום גיבוב לא-מלוח
#   שקול לפרסום הת.ז. עצמה. המלח הסודי הוא מה שהופך את זה לבלתי הפיך,
#   כי בלעדיו אין מה להשוות אליו.
#
# למה נכשל בשקט (fail closed):
#   בלי מלח - לא נוצר pid כלל, והיחידים פשוט לא נכנסים לעץ. לעולם לא
#   ליפול חזרה לגיבוב לא-מלוח: זו התקלה שהופכת "הגנו על הפרטיות" ל"פרסמנו
#   מאגר ת.ז." בלי ששום דבר בלוג ייראה חריג.
#
# המלח סוד קבוע. אם הוא מתחלף - כל ה-pid משתנים, וזהויות היחידים בעץ
# מתאפסות (לא נורא, אבל דורש בנייה מחדש). אם הוא דולף - המיפוי הפיך.
# ============================================================
PID_SALT = os.environ.get("PID_SALT") or ""
PID_LEN = 16          # 64 ביט - די והותר ל-~10^4 יחידים, בלי התנגשויות


def holder_pid(id_type, id_num):
    """מזהה יציב ואטום לאדם. None אם אין מלח או אין מספר.

    הסוג נכנס לגיבוב יחד עם המספר כדי שדרכון זר שמספרו זהה במקרה לת.ז.
    ישראלית לא ייצור התנגשות בין שני אנשים.
    """
    if not PID_SALT or not id_num:
        return None
    msg = f"{(id_type or '').strip()}|{id_num}".encode("utf-8")
    return hmac.new(PID_SALT.encode("utf-8"), msg,
                    hashlib.sha256).hexdigest()[:PID_LEN]


def decode(raw):
    """מגנא מגישים windows-1255"""
    for enc in ("windows-1255", "utf-8-sig", "utf-8"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("windows-1255", errors="replace")


def _val(line, label):
    """'סוג מספר זיהוי: מספר ברשם' -> 'מספר ברשם'"""
    m = re.match(re.escape(label) + r"\s*:?\s*(.*)$", line.strip())
    if not m:
        return None
    v = m.group(1).strip()
    if not v or v.strip("_ ") == "":
        return None
    return v


def _entity_kind(h, id_type):
    """סוג הישות המוצהר בת077. היררכיה (לבקשת המשתמש):

    1. יש ח.פ ישראלי תקין -> company / partnership (לפי סוג הרשם).
    2. אחרת, לפי 'סוג המחזיק' (holderType) — המקור הישיר והאמין:
       - דירקטור/מנכ"ל, נושא משרה, קרוב, בעל מניות יחיד -> person
       - סוג תאגידי/מוסדי מובהק -> company
    3. רק כאשר holderType הוא הערך הגנרי "בעל ענין שאינו עונה על אף אחת
       מההגדרות האחרות" (שמופיע גם על אדם וגם על חברה זרה) — נופלים לשדה
       ההתאגדות/סוג-מספר-זיהוי:
         'אדם פרטי ...'  -> person
         'התאגד בחו"ל'   -> company-foreign
         'התאגד בישראל'  -> person (ח.פ שגוי — לא ממציאים חברה)

    שדה ההתאגדות הוא גם השער הסופי לחשיפת המספר (ראה parse_holder_card):
    מספר נחשף רק לחברה, ולעולם לא כשההתאגדות 'אדם פרטי'.
    """
    inc = (h.get("incorporation") or "").strip()
    it = (id_type or "").strip()
    ht = (h.get("holderType") or "").strip()

    if h.get("hp"):
        if "שותפוי" in it or "שותפות" in it:
            return "partnership"
        return "company"

    GENERIC = "שאינו עונה"        # "בעל ענין שאינו עונה על אף אחת..."
    PERSON_HT = ("דירקטור", "מנכ", "נושא משרה", "קרוב", "יחיד",
                 "בן משפחה", "עובד")
    COMPANY_HT = ("תאגיד", "חברה", "מוסדי", "קרן", "גמל", "נאמנות",
                  "שותפות", "ביטוח", "בנק")

    # שלב 2 — holderType ישיר (כשאינו הגנרי)
    if ht and GENERIC not in ht:
        if any(k in ht for k in PERSON_HT):
            return "person"
        if any(k in ht for k in COMPANY_HT):
            # תאגיד לפי holderType אך בלי ח.פ — אם ההתאגדות זרה, חברה זרה;
            # אחרת שמרני לאדם (אין ח.פ תקין להצדיק חברה ישראלית).
            if "התאגד בחו" in inc or "ארץ ההתאגדות" in it or 'בחו"ל' in it:
                return "company-foreign"
            return "company-foreign" if "התאגד" in inc else "person"

    # שלב 3 — holderType גנרי (או ריק): מכריעים לפי ההתאגדות
    if "אדם פרטי" in inc:
        return "person"
    if "התאגד בחו" in inc or "ארץ ההתאגדות" in it or 'בחו"ל' in it:
        return "company-foreign"
    # התאגד בישראל בלי ח.פ תקין, או לא ידוע — שמרני לאדם
    return "person"


def parse_holder_card(cells):
    """כרטיס מחזיק: רשימת מחרוזות, שדה/ערך לסירוגין."""
    if not cells or cells[0].strip() != "שם המחזיק":
        return None

    h = {"name": cells[1].strip() if len(cells) > 1 else None}
    if not h["name"] or h["name"].lower() == "nan":
        return None

    id_type = id_num = None
    controller = None
    for i, c in enumerate(cells):
        c = c.strip()
        if c == "שם המחזיק באנגלית כפי שמופיע בדרכון" and i + 1 < len(cells):
            v = cells[i + 1].strip()
            h["nameEn"] = v if v.lower() != "nan" else None
        if c == "בעל השליטה במחזיק:" and i + 1 < len(cells):
            v = cells[i + 1].strip()
            if v and v not in ("-", "_________") and v.lower() != "nan":
                controller = v
        for lbl, key in (
            ("מס' מחזיק", "holderNo"),
            ("סוג המחזיק", "holderType"),
            ("אזרחות/ ארץ התאגדות או רישום", "incorporation"),
            ("מספר נייר ערך בבורסה", "securityNumber"),
        ):
            v = _val(c, lbl)
            if v:
                h[key] = v
        v = _val(c, "סוג מספר זיהוי")
        if v:
            id_type = v
        v = _val(c, "מספר זיהוי")
        if v:
            id_num = v

    # המספר נשמר כפי שהוא, יחד עם הסוג המוצהר.
    h["idType"] = id_type
    h["idNumber"] = re.sub(r"\D", "", id_num) if id_num else None

    # hp = זיהוי תאגידי מוצהר *וגם* תואם תבנית ח.פ. אמיתית.
    # למה גם וגם: נמצאו בפועל דיווחים שבהם המדווח הצהיר "מספר ברשם החברות
    # בישראל" אך הזין בפועל ת.ז. של יחיד (שגיאת דיווח, לרוב מתוקנת בדוח
    # מתקן מאוחר יותר). מספר ברשם אמיתי הוא תמיד 9 ספרות שמתחילות ב-5
    # (חברה/שותפות/עמותה/אגודה שיתופית) — ת.ז. מרופדת באפסים לעולם לא.
    # ללא הבדיקה הזו, ת.ז. כאלה חומקות לתוך המפה הציבורית ומפילות את שער
    # הבטיחות ב-workflow (שבודק שאין ת.ז. תחת data/).
    # זיהוי תאגידי ישראלי מוצהר: חברה, שותפות, עמותה, או אגודה שיתופית —
    # לכולם מספר ברשם ישראלי בן 9 ספרות שמתחיל ב-5, וכולם ניתנים לחיפוש
    # ברשם. קודם נבדק רק "רשם החברות", וכך שותפות מוגבלת (רשם השותפויות)
    # עם מספר תקין כמו 550251409 נפלה ובטעות נותבה כתאגיד זר.
    declared_israeli_registrar = bool(id_type and (
        CORPORATE_ID in id_type              # רשם החברות
        or "רשם השותפויות" in id_type        # רשם השותפויות
        or "רשם העמותות" in id_type          # רשם העמותות
        or "אגודות שיתופיות" in id_type      # רשם האגודות השיתופיות
    ))
    is_hp = looks_like_hp(h["idNumber"])
    h["hp"] = h["idNumber"] if (declared_israeli_registrar and is_hp) else None
    h["isCorporate"] = bool(h["hp"])
    # דגל אבחוני בלבד (לא נכתב לקובץ הציבורי): הוצהר כתאגיד ישראלי אך המספר
    # לא תואם תבנית ח.פ — כנראה ח.פ. שגוי בדיווח.
    h["misfiledCorporate"] = bool(declared_israeli_registrar and h["idNumber"] and not is_hp)
    # שם המחזיק: מגן מפני ת.ז. שנדבקה בטעות לתוך שדה שם (ראה scrub_name).
    h["name"] = scrub_name(h["name"])
    if h.get("nameEn"):
        h["nameEn"] = scrub_name(h["nameEn"])

    # סוג הישות — מוכרע לפי היררכיה: holderType קודם, התאגדות רק כשה-
    # holderType גנרי (ראה _entity_kind). זה מה שמבדיל אדם מתאגיד זר.
    h["kind"] = _entity_kind(h, id_type)

    # חשיפת המספר — שער כפול, בטוח:
    #   חברה זרה (company-foreign): המספר הוא מספר תאגיד ציבורי בחו"ל, מותר
    #     לפרסמו. נשמר ב-foreignId (שדה נפרד מ-hp כי אינו ח.פ ישראלי, ולא
    #     ניתן לחיפוש ברשם הישראלי). הישות מוצגת כחברה.
    #   אדם: pid בלבד. המספר (ת.ז/דרכון) לעולם לא נחשף, בשום מצב.
    # הבחירה נשענת על kind==company-foreign, ו-_entity_kind נותן זאת רק
    # כשההתאגדות אינה "אדם פרטי" — כך שת.ז של אדם עם holderType גנרי
    # (כמו "אהרון כהן") לעולם לא זולגת.
    if h["kind"] == "company-foreign":
        h["foreignId"] = h["idNumber"]        # גלוי — מספר תאגיד זר
        h["pid"] = None
    else:
        h["foreignId"] = None
        # pid = מזהה אטום לאדם. המספר (ת.ז/דרכון) נשאר רק ב-idNumber, שנכתב
        # ל-private/ בלבד; ל-data/ עובר רק ה-pid.
        h["pid"] = None if h["hp"] else holder_pid(id_type, h["idNumber"])

    # בעל השליטה במחזיק: טקסט חופשי, לעתים עם ת.ז חשופה. מנוקה מיד, ונשמר
    # לבדיקה ידנית ב-private/ בלבד. לא בונה קצה (אין מזהה אמין).
    h["controller"] = scrub_name(controller) if controller else None
    return h


# סינון לעץ השליטה נעשה לפי כוח הצבעה נוכחי (votPct>0), לא לפי סוג הנייר:
# בעל אג"ח, אופציות שטרם מומשו, או מניות רדומות — לכולם 0% הצבעה נוכחית,
# וכולם אינם בעלי שליטה. זה גם הקריטריון ההלכתי (השפעה בפועל) וגם משוחרר
# מפרסינג שביר של שמות ניירות ("אפ3/23", "עובד23" וכו').


def parse_summary_table(flat):
    """הטבלה המסכמת שבראש הדוח: שורה לכל (מחזיק, נייר), עם שיעור ההחזקה.

    מבנה כל שורה בטקסט המשוטח:
      <מס' מחזיק> <שם> ... <סוג נייר> <כמות> <%הון> <%הצבעה> <%הון-דילול> <%הצבעה-דילול>

    מפתח החיבור הוא מספר המחזיק (העמודה הראשונה) — אותו מספר בדיוק שמופיע
    ככרטיס המפורט תחת "מס' מחזיק". לכן אין כאן שום התאמת שמות: האחוז
    מהטבלה המסכמת נצמד לכרטיס לפי מספר, נקי לגמרי.

    מחזיר {holderNo(int): {"capPct", "votPct", "hasEquity", "secTypes"}}.
    אחוזים מסוכמים על פני ניירות הוניים בלבד (אג"ח מנוכה). מחזיק שכל
    ניירותיו אג"ח -> hasEquity=False, ואחוז 0 -> יסונן מהעץ במורד הזרם.
    """
    # מאתר את תחילת הטבלה (אחרי כותרת העמודות) ואת סופה (שורת "סה"כ" הכללית).
    hdr = flat.find("שם, סוג וסדרה של נייר ערך")
    if hdr < 0:
        return {}
    seg = flat[hdr:]

    # כל שורה מסתיימת ב: <כמות> <%הון> <%הצבעה> <%הון-דילול> <%הצבעה-דילול>.
    # שני האחרונים (דילול מלא) עשויים להיות "_________" כשאין דילול — ואז
    # אסור שהשורה תיפול מהרגקס, אחרת מספר המחזיק של השורה הבאה נתפס שגוי
    # (זה בדיוק מה שקרה: רבינוביץ #6/#7 עם "0 0 _________ _________" לא
    # נתפסו, וכל המספרים אחריהם זזו). לכן הדילול מקבל חלופה: מספר או קו
    # תחתונים. הערכים עצמם לא בשימוש — רק ה-%הון/%הצבעה הרגילים.
    # מספר המחזיק חייב להיות מעוגן: מספר קצר, ואחריו מילה עברית (שם) — לא
    # ספרה — כדי שלא ייתפס מספר נייר/כמות בטעות.
    _num = r"(?:-?\d{1,3}(?:,\d{3})*(?:\.\d+)?|-?\d+(?:\.\d+)?)"
    _dil = r"(?:[\d.]+|_+)"                            # דילול: מספר או "____"
    row_re = re.compile(
        r"(?:^|\s)(\d{1,3})\s+"                        # מספר מחזיק
        r"([^\d].*?)\s+"                                # שם+תיאור (מתחיל באות)
        r"(" + _num + r")\s+"                          # כמות
        r"([\d.]+)\s+([\d.]+)\s+"                      # %הון %הצבעה (רגיל)
        r"" + _dil + r"\s+" + _dil + r"(?=\s|$)"       # דילול ×2 (או קווים)
    )
    out = {}
    for m in row_re.finditer(seg):
        no = int(m.group(1))
        desc = m.group(2)                             # שם + תיאור נייר
        try:
            cap = float(m.group(4)); vot = float(m.group(5))
        except ValueError:
            continue
        rec = out.setdefault(no, {"capPct": 0.0, "votPct": 0.0,
                                  "hasVote": False, "secTypes": set()})
        # סוג הנייר נשמר לתצוגה/בדיקה בלבד — לא לסינון. הסינון לעץ הוא לפי
        # כוח הצבעה נוכחי, לא לפי שם הנייר (שמותיו קודים שבירים: "אפ3/23",
        # "עובד23"). כל נייר מסוכם, וההצבעה הכוללת קובעת אם המחזיק שולט.
        rec["secTypes"].add(desc.split()[-1] if desc.split() else desc)
        rec["capPct"] += cap
        rec["votPct"] += vot
    for rec in out.values():
        rec["capPct"] = round(rec["capPct"], 2)
        rec["votPct"] = round(rec["votPct"], 2)
        rec["hasVote"] = rec["votPct"] > 0        # כוח הצבעה נוכחי בפועל
        rec["secTypes"] = sorted(rec["secTypes"])
    return out


def _controller_of(flat, holder_no):
    """הטקסט החופשי של 'בעל השליטה במחזיק' עבור מחזיק מסוים.

    זהו שדה חופשי לחלוטין — לעתים מפנה ('ראה סעיף 6'), לעתים שם בלבד,
    ולעתים שם *עם ת.ז חשופה* ('יאיר המבורגר ת.ז 007048671'). ולכן:
      1. לעולם לא בונים ממנו קצה אוטומטי בעץ — אין בו מזהה אמין.
      2. חובה להעבירו דרך scrub_name לפני שמירה, כדי שת.ז לא תזלוג אפילו
         לפלט הפרטי בטעות (scrub_name משאיר ח.פ, מסיר ריצת 9-ספרות שאינה).
    נשמר ל-private/ בלבד, כאיתות לבדיקה ידנית — לא לפרסום.
    """
    # לא ממומש כאיתור לפי מספר (הטקסט אינו מקושר בוודאות למספר בטקסט
    # המשוטח); הערך נלקח בכרטיס עצמו ב-parse_holder_card. פונקציה זו
    # שמורה לעתיד אם נרצה איתור גלובלי. כרגע מחזירה None.
    return None


def parse_t077(raw_or_text):
    text = decode(raw_or_text) if isinstance(raw_or_text, bytes) else raw_or_text

    if "ת077" not in text:
        raise ValueError("הקובץ אינו נראה כמו ת077")

    tables = pd.read_html(io.StringIO(text))

    # התוויות והערכים יושבים בתאים נפרדים, ולכן regex על ה-HTML הגולמי
    # לא תופס אותם. משטחים לטקסט נקי קודם.
    flat = re.sub(r"<[^>]+>", " ", text)
    flat = re.sub(r"&nbsp;?", " ", flat)
    flat = re.sub(r"\s+", " ", flat)

    # החברה המדווחת: הטבלה הקטנה עם 'מספר ברשם:'
    # הכותרת: שם, [שם באנגלית - לא תמיד], 'מספר ברשם: NNN'
    company = {"name": None, "nameEn": None, "hp": None}
    for df in tables:
        if df.shape[1] != 1 or df.shape[0] > 6:
            continue
        cells = [str(x).strip() for x in df.iloc[:, 0].tolist()]
        idx = next((j for j, c in enumerate(cells)
                    if c.startswith("מספר ברשם")), None)
        if idx is None or idx == 0:
            continue
        raw = re.sub(r"\D", "", cells[idx]) or None
        company["name"] = scrub_name(cells[0])
        company["nameEn"] = scrub_name(cells[1]) if idx > 1 else None
        # אותה הבחנה, מיד, לא רק אצל המחזיקים: מספר החברה המדווחת עצמה
        # עובר את אותו שער looks_like_hp לפני שהוא נחשב hp פרסום. אם לא -
        # הוא כנראה חברה זרה עם מספר רישום אחר, או שגיאת פרסור - בכל
        # מקרה לא מתפרסם כח.פ. hpRaw נשמר רק ל-private/ (לא מגיע לקובץ
        # ציבורי) לצורך אבחון בין השניים.
        company["hp"] = raw if looks_like_hp(raw) else None
        company["hpRaw"] = raw
        break

    ref = re.search(r"אסמכתא:\s*([\d\-]+)", flat)
    sent = re.search(r"שודר במגנא:\s*([\d/]+)", flat)

    # דוח מתקן מבטל דוח קודם. ראינו במדגם תיקון של ח.פ. שגוי,
    # ולכן חובה לדעת מי מתקן את מי.
    fix = re.search(
        r"דוח מתקן לדוח משובש שנשלח בתאריך\s*([\d/]+)\s*"
        r"שמספר אסמכתא שלו:\s*([\d\-]+)", flat)
    corr = None
    if fix:
        det = re.search(r"השיבוש:\s*(.*?)\s*סיבת השיבוש:", flat)
        corr = {
            "correctsDate": fix.group(1),
            "correctsReference": fix.group(2),
            "defect": (det.group(1).strip()[:200] if det else None),
        }

    holders, seen = [], set()
    for df in tables:
        if df.shape[1] != 1:
            continue
        cells = [str(x) for x in df.iloc[:, 0].tolist()]
        h = parse_holder_card(cells)
        if not h:
            continue
        key = (h["name"], h.get("holderNo"))
        if key in seen:
            continue
        seen.add(key)
        holders.append(h)

    # שיעורי החזקה מהטבלה המסכמת, מחוברים לפי מספר מחזיק (בלי התאמת שמות).
    # זהו המקור לאחוזים שקודם הגיעו רק ממאיה (ולרוב לא הצליחו להתחבר).
    summary = parse_summary_table(flat)
    for h in holders:
        no = h.get("holderNo")
        try:
            rec = summary.get(int(no)) if no is not None else None
        except (ValueError, TypeError):
            rec = None
        if rec:
            h["capPct"] = rec["capPct"]
            h["votPct"] = rec["votPct"]
            h["hasVote"] = rec["hasVote"]
            h["secTypes"] = rec["secTypes"]
        else:
            h["capPct"] = None
            h["votPct"] = None
            h["hasVote"] = None        # לא נמצא בטבלה — לא מסננים ליתר ביטחון
            h["secTypes"] = []

    return {
        "company": company,
        "reference": ref.group(1) if ref else None,
        "sentDate": sent.group(1) if sent else None,
        "correction": corr,
        "holders": holders,
    }




# ============================================================
# רשת
# ============================================================
def new_session():
    """חימום חובה - בלי הקוקיז של Incapsula הפיד מחזיר חסימה."""
    s = requests.Session()
    s.verify = VERIFY_SSL
    s.trust_env = VERIFY_SSL
    s.headers.update({"user-agent": UA, "accept-language": "he-IL,he;q=0.9"})
    s.get(PAGE, timeout=40, verify=VERIFY_SSL)
    return s


FEED_HDRS = {
    "accept": "application/json, text/plain, */*",
    "accept-language": "he-IL",
    "content-type": "application/json",
    "origin": MAYA,
    "referer": PAGE,
    "x-maya-with": "allow",
}


class FeedBadRequest(Exception):
    """400 שנשאר גם אחרי חימום מחדש — הבקשה עצמה נדחית (טווח/פרמטר),
    לא בעיית קוקיז. הקורא יכול לצמצם את החלון ולנסות שוב."""


def fetch_feed_page(s, d_from, d_to, offset, limit=20, retries=4, log=None):
    """מוריד עמוד אחד מהפיד, עם retry שמבחין בין סוגי כשל.

    מחזיר (data, session) — ה-session עשוי להתחלף אם נדרש חימום מחדש,
    והקורא חייב להשתמש בזה שחזר (הקוקיז החדשים).

    למה זה לא רק retry רגיל: 400 מ-maya על סריקה עמוקה נגרם בדרך כלל
    מפקיעת קוקיז Incapsula באמצע ריצה ארוכה (סריקת 15-20 שנה אורכת דקות
    רבות, והקוקיז שקיבלנו בחימום הראשוני פגים). ניסיון חוזר על *אותה*
    בקשה בלי חימום מחדש לעולם לא יעזור — לכן ה-retry הישן (except רחב)
    בזבז 3 ניסיונות וזרק. עכשיו: 400/403 -> חימום מחדש של ה-session ואז
    ניסיון חוזר; אם 400 שורד גם אחרי חימום -> FeedBadRequest (הבקשה
    עצמה נדחית, לא הקוקיז) כדי שהקורא יצמצם את החלון.
    """
    body = {
        "pageNumber": offset // limit + 1,
        "fromDate": d_from,
        "toDate": d_to,
        "isPriority": False,
        "isTradeHalt": False,
        "by": "company",
        "formId": "ת077",
        "limit": limit,
        "offset": offset,
    }
    rewarmed = False
    for a in range(retries):
        try:
            r = s.post(FEED, data=json.dumps(body).encode("utf-8"),
                       headers=FEED_HDRS, timeout=40, verify=VERIFY_SSL)
            status = r.status_code
            if status in (400, 401, 403):
                # ייתכן קוקיז פגים. חימום מחדש פעם אחת ואז ניסיון נוסף.
                if not rewarmed:
                    if log:
                        log(f"      {status} — מחמם session מחדש ומנסה שוב")
                    time.sleep(1.5)
                    s = new_session()
                    rewarmed = True
                    continue
                # כבר חיממנו והבקשה עדיין נדחית — הבקשה עצמה פסולה.
                raise FeedBadRequest(
                    f"{status} על החלון {d_from[:10]}..{d_to[:10]} "
                    f"offset {offset} (נשאר אחרי חימום מחדש)")
            r.raise_for_status()
            return r.json(), s
        except FeedBadRequest:
            raise                       # לא מנסים שוב — צמצום חלון הוא באחריות הקורא
        except Exception:
            if a == retries - 1:
                raise
            time.sleep(2 * (a + 1))
    raise RuntimeError("fetch_feed_page: לא אמור להגיע לכאן")


def fetch_feed(s, d_from, d_to, throttle=0.4, log=print):
    """אין TotalRec בתשובה - מעמדים עד שחוזר עמוד חלקי.

    מחזיר (reports, session): ה-session עשוי להתחלף (חימום מחדש בתוך
    fetch_feed_page), והקורא חייב להשתמש בזה שחזר להמשך.
    """
    out, offset, limit = [], 0, 20
    while True:
        page, s = fetch_feed_page(s, d_from, d_to, offset, limit, log=log)
        if not page:
            break
        out.extend(page)
        log(f"      offset {offset:>5} -> {len(page):>2} רשומות "
            f"(סה\"כ {len(out)})")
        if len(page) < limit:
            break
        offset += limit
        if offset > 100000:
            raise RuntimeError("יותר מדי עמודים בפיד")
        time.sleep(throttle)
    return out, s


def fetch_feed_window(s, iso_from, iso_to, d_from, d_to, throttle=0.4,
                      log=print, depth=0):
    """עוטף fetch_feed עם צמצום-חלון על FeedBadRequest.

    כשמאיה דוחה חלון (400 ששרד חימום מחדש) — לרוב החלון גדול/עמוק מדי —
    חוצים אותו לשניים ומנסים כל חצי, עד רצפה של יום בודד. כך סריקה של
    15-20 שנה לא נופלת על חלון בעייתי אחד, אלא מדללת אותו עד שהוא עובר.
    מחזיר (reports, session).

    iso_* הם המחרוזות לבקשה; d_* הם datetime לחישוב חציית הטווח.
    """
    try:
        return fetch_feed(s, iso_from, iso_to, throttle, log=log)
    except FeedBadRequest as e:
        span = (d_to - d_from).days
        if span <= 1 or depth > 12:
            # רצפה: יום בודד עדיין נדחה — מדלגים עליו בלוג ברור, לא מפילים
            # את כל הסריקה בגלל חלון בודד.
            log(f"      דילוג על {iso_from[:10]}..{iso_to[:10]}: {e}")
            return [], s
        mid = d_from + (d_to - d_from) / 2
        iso_mid = mid.strftime("%Y-%m-%dT21:00:00.000Z")
        log(f"      חוצה חלון {iso_from[:10]}..{iso_to[:10]} "
            f"(דחייה, מנסה חצאים)")
        left, s = fetch_feed_window(s, iso_from, iso_mid, d_from, mid,
                                    throttle, log, depth + 1)
        time.sleep(throttle)
        right, s = fetch_feed_window(s, iso_mid, iso_to, mid, d_to,
                                     throttle, log, depth + 1)
        return left + right, s


def report_meta(r):
    """שדות הפיד שאנחנו צריכים"""
    att = (r.get("attachments") or [])
    htm = next((a for a in att if a.get("fileType") == "htm"), None)
    co = (r.get("companies") or [{}])[0]
    return {
        "id": r.get("id"),
        "title": r.get("title"),
        "publishDate": r.get("publishDate"),
        "formId": r.get("formId"),
        "companyId": co.get("companyId"),
        "companyName": co.get("name"),
        "isDual": co.get("isDual"),
        "url": htm.get("url") if htm else None,
        "fileSize": htm.get("fileSize") if htm else None,
    }


def fetch_report_html(s, url, retries=3):
    full = FILES + url.lstrip("/")
    for a in range(retries):
        try:
            r = s.get(full, timeout=60, verify=VERIFY_SSL)
            r.raise_for_status()
            return r.content
        except Exception:
            if a == retries - 1:
                raise
            time.sleep(2 * (a + 1))


# ============================================================
# פלט
# ============================================================
def save_report(meta, parsed):
    """שומר את הגולמי (עם ת.ז.) לתיקייה הפרטית."""
    os.makedirs(os.path.join(RAW_DIR, "reports"), exist_ok=True)
    rec = {"meta": meta, **parsed}
    path = os.path.join(RAW_DIR, "reports", f"{meta['id']}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(rec, f, ensure_ascii=False, separators=(",", ":"))
    return path


def have_report(rid):
    return os.path.exists(os.path.join(RAW_DIR, "reports", f"{rid}.json"))


def _fold(best, pub, holders):
    """מכניס מחזיקים למפה. publishDate מאוחר גובר - דוחות מתקנים
    מתקנים ח.פ. שגוי, ולכן האחרון קובע."""
    for h in holders or []:
        if not h.get("hp") or not h.get("name"):
            continue
        key = norm_name(h["name"])
        if not key:
            continue
        prev = best.get(key)
        if prev and (prev.get("asOf") or "") >= (pub[:10] if pub else ""):
            continue
        best[key] = {
            "name": h["name"],
            "nameEn": h.get("nameEn"),
            "hp": h["hp"],
            "holderType": h.get("holderType"),
            "incorporation": h.get("incorporation"),
            "asOf": pub[:10] if pub else None,
        }


def _fold_edges(edges, pub, company, holders, skipped):
    """קצה בעלות ישיר מהדוח: ח.פ החברה המדווחת -> המחזיקים המוצהרים בה.

    זהו הנתון שהדוח נועד לתת. אין כאן ניחוש שמות: גם 'מספר ברשם' של החברה
    המדווחת וגם 'מספר זיהוי' של המחזיק מוצהרים באותו קובץ.

    לכל מחזיק בדיוק אחד משניים:
      hp  - תאגיד. מספר ברשם אמיתי, ציבורי, ניתן להצלבה מול הרשם.
      pid - יחיד. מזהה אטום מגובב. הת.ז. עצמה לעולם לא מגיעה לכאן.

    שני מפתחות נפרדים ולא שדה id אחד - בכוונה. hp הוא מספר שאפשר לחפש
    ברשם החברות; pid הוא מחרוזת חסרת משמעות מחוץ לאתר. ערבוב שלהם היה
    גורם ל-frontend לנסות לפתוח כרטיס רשם לאדם, ולשער הבטיחות (שבודק
    שדות hp) לבדוק את הדבר הלא נכון.

    מחזיק מופיע פעם אחת לכל נייר ערך, ולכן מסננים כפילויות לפי המזהה.
    הדוח האחרון לכל חברה גובר - הוא מצבת ההחזקות הנוכחית, לא תוספת.
    """
    chp = (company or {}).get("hp")
    if not chp:
        raw = (company or {}).get("hpRaw")
        if raw:
            skipped.append({"nameRaw": (company or {}).get("name"), "hpRaw": raw})
        return
    asof = pub[:10] if pub else ""
    prev = edges.get(chp)
    if prev and (prev.get("asOf") or "") > asof:
        return
    # מחזיק אחד מופיע בכמה שורות — נייר לנייר, וגם חשבון לחשבון (נוסטרו,
    # קרנות נאמנות, קופות גמל). כולן אותה ישות (אותו ח.פ) ולכן שיעור
    # ההחזקה האמיתי הוא ה*סכום* על פני החשבונות, לא שורה בודדת. קודם נשמרה
    # רק השורה הראשונה, ולכן הראל הראה 0.08% במקום 9.41% (0.08+9.33).
    # מאגדים לפי מזהה: סוכמים vot/cap, שומרים את השם/סוג של השורה בעלת
    # ההחזקה הגבוהה ביותר (הנציג הטבעי של המחזיק).
    agg = {}
    order = []
    for h in holders or []:
        hp, pid, fid = h.get("hp"), h.get("pid"), h.get("foreignId")
        key = hp or fid or pid
        if not key or key == chp:
            continue
        v = h.get("votPct")
        c = h.get("capPct")
        if key not in agg:
            agg[key] = {"vot": None, "cap": None, "rep": h, "repVot": None,
                        "hp": hp, "fid": fid, "pid": pid}
            order.append(key)
        a = agg[key]
        if v is not None:
            a["vot"] = (a["vot"] or 0) + v
        if c is not None:
            a["cap"] = (a["cap"] or 0) + c
        # הנציג = השורה עם ה-vot הגבוה ביותר (לשם/סוג התצוגה)
        if v is not None and (a["repVot"] is None or v > a["repVot"]):
            a["repVot"] = v
            a["rep"] = h

    hs = []
    for key in order:
        a = agg[key]
        vot = None if a["vot"] is None else round(a["vot"], 2)
        cap = None if a["cap"] is None else round(a["cap"], 2)
        # מחזיק ללא כוח הצבעה נוכחי כלל (סכום 0, או רק אג"ח/אופציות/רדומות)
        # אינו בעל שליטה — לא נכנס לעץ. vot=None פירושו "לא נמצא בטבלה
        # המסכמת" — נשמר (זהות ודאית, אחוז לא ידוע), לא מסונן.
        if vot is not None and vot <= 0:
            continue
        h = a["rep"]
        rec = {"name": h.get("name"), "holderType": h.get("holderType"),
               "kind": h.get("kind"), "vot": vot, "cap": cap}
        if a["hp"]:
            rec["hp"] = a["hp"]
        elif a["fid"]:
            rec["foreignId"] = a["fid"]   # מספר תאגיד זר, גלוי, לא לחיפוש ברשם
        else:
            rec["pid"] = a["pid"]
        hs.append(rec)
    edges[chp] = {"name": (company or {}).get("name"),
                  "asOf": asof or None, "holders": hs}


def _unpack(rep):
    """תומך גם ב-(pub, holders) הישן וגם ב-(pub, company, holders) החדש,
    כדי שגרסה ישנה של t077_latest/daily לא תתרסק מול הליבה הזו."""
    if len(rep) == 3:
        return rep[0], rep[1], rep[2]
    return rep[0], None, rep[1]


def write_holders_csv(new_reports, rebuild=False, log=print):
    """פלט בדיקה פרטי (private/, gitignored): שורה לכל (חברה מדווחת, מחזיק),
    עם ת.ז/ח.פ בגלוי, אחוזים, סוג מחזיק ובעל השליטה במחזיק. נועד להיפתח
    באקסל ולעבור עליו ידנית — לוודא נכונות הזיהוי והאחוזים.

    לעולם לא עולה לאתר: זהו המקום היחיד שבו ת.ז נשמרת לצד השם באופן קריא.
    ממזג לתוך הקיים (כמו שאר הפלטים), אלא אם rebuild.
    """
    cols = ["companyHp", "companyName", "holderNo", "holderName",
            "idType", "idNumber", "hp", "holderType", "capPct", "votPct",
            "hasVote", "secTypes", "controller", "asOf"]
    rows = {}
    # מיזוג: קורא את הקיים אם לא rebuild
    if not rebuild and os.path.exists(HOLDERS_CSV):
        try:
            with open(HOLDERS_CSV, encoding="utf-8-sig", newline="") as f:
                for r in csv.DictReader(f):
                    rows[(r.get("companyHp"), r.get("holderNo"),
                          r.get("holderName"))] = r
        except Exception as e:
            log(f"   אזהרה: קריאת {HOLDERS_CSV} נכשלה ({e}) — נבנה מחדש")

    for rep in (new_reports or []):
        pub, company, holders = _unpack(rep)
        chp = (company or {}).get("hp") or (company or {}).get("hpRaw")
        cname = (company or {}).get("name")
        asof = pub[:10] if pub else ""
        for h in holders or []:
            key = (chp, str(h.get("holderNo")), h.get("name"))
            prev = rows.get(key)
            if prev and (prev.get("asOf") or "") > asof:
                continue      # הדוח האחרון גובר
            rows[key] = {
                "companyHp": chp, "companyName": cname,
                "holderNo": h.get("holderNo"), "holderName": h.get("name"),
                "idType": h.get("idType"), "idNumber": h.get("idNumber"),
                "hp": h.get("hp"), "holderType": h.get("holderType"),
                "capPct": h.get("capPct"), "votPct": h.get("votPct"),
                "hasVote": h.get("hasVote"),
                "secTypes": "; ".join(h.get("secTypes") or []),
                "controller": h.get("controller"), "asOf": asof,
            }

    os.makedirs(RAW_DIR, exist_ok=True)
    with open(HOLDERS_CSV, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for r in sorted(rows.values(),
                        key=lambda x: (str(x.get("companyName") or ""),
                                       int(x.get("holderNo") or 0))):
            w.writerow(r)
    log(f"   {len(rows):,} שורות מחזיקים -> {HOLDERS_CSV} (פרטי, לבדיקה)")


def build_holder_map(new_reports, rebuild=False, log=print):
    """שני פלטים ציבוריים (ת.ז. לא נכנסות לאף אחד מהם):

    holder-hp.json  - מפת שם מחזיק -> ח.פ. גיבוי בלבד, לשימוש כשאין דוח.
    t077-edges.json - הקצה עצמו: ח.פ חברה -> ח.פ מחזיקיה. זה המקור המדויק,
                      כי הוא לא עובר דרך שמות בכלל.

    ממזג לתוך הקיים במקום לסרוק הכל מחדש - לרנר של Actions אין ארכיון גולמי,
    ולכן סריקה מלאה הייתה מוחקת את מה שנצבר.

    new_reports: רשימת (publishDate, company, holders) מההרצה הנוכחית.
    rebuild:     True -> בונה מאפס (תמונת "אחרון לכל חברה").
                 False -> ממזג לתוך הקיים (הרצה יומית).
    """
    best, edges = {}, {}

    # 1. מה שכבר במפה
    if not rebuild and os.path.exists(MAP_PATH):
        with open(MAP_PATH, encoding="utf-8") as f:
            old = json.load(f)
        for rec in (old.get("holders") or {}).values():
            k = norm_name(rec.get("name"))
            if k:
                best[k] = rec
        log(f"   קיימים: {len(best):,} תאגידים")

    if not rebuild and os.path.exists(EDGES_PATH):
        with open(EDGES_PATH, encoding="utf-8") as f:
            edges = (json.load(f) or {}).get("companies") or {}
        log(f"   קיימים: {len(edges):,} חברות עם קצות בעלות")

    scanned = 0
    skipped = []
    for rep in (new_reports or []):
        pub, company, holders = _unpack(rep)
        scanned += 1
        _fold(best, pub or "", holders)
        _fold_edges(edges, pub or "", company, holders, skipped)

    holders = {v["hp"]: v for v in best.values()}
    os.makedirs(PUB_DIR, exist_ok=True)
    out = {"count": len(holders), "reportsFolded": scanned,
           "holders": holders,
           "byName": {k: v["hp"] for k, v in best.items()}}
    with open(MAP_PATH, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, separators=(",", ":"))
    log(f"   {len(holders):,} תאגידים ({scanned:,} דוחות מוזגו) -> {MAP_PATH}")

    n_edges = sum(len(v.get("holders") or []) for v in edges.values())
    n_ppl = sum(1 for v in edges.values()
                for h in (v.get("holders") or []) if h.get("pid"))
    with open(EDGES_PATH, "w", encoding="utf-8") as f:
        json.dump({"count": len(edges), "edgeCount": n_edges,
                   "personEdges": n_ppl, "hasSalt": bool(PID_SALT),
                   "companies": edges}, f,
                  ensure_ascii=False, separators=(",", ":"))
    log(f"   {len(edges):,} חברות, {n_edges:,} קצות בעלות "
        f"({n_ppl:,} יחידים) -> {EDGES_PATH}")
    if not PID_SALT:
        log("   !!! PID_SALT לא מוגדר - יחידים לא נכנסים לעץ.")
        log("       זו התנהגות מכוונת: גיבוב ת.ז. בלי מלח הפיך תוך שנייה,")
        log("       ולכן עדיף בלי יחידים מאשר עם ת.ז. חשופות בפועל.")
    if skipped:
        log(f"   !!! {len(skipped)} דוחות דולגו - מספר החברה המדווחת "
            f"עצמה לא עבר את שער ה-hp (לא 9 ספרות שמתחילות ב-5).")
        for s in skipped[:10]:
            log(f"       {s['nameRaw']!r}  hpRaw={s['hpRaw']!r}")
        log("       בדוק ידנית מול private/: חברה זרה/דואלית עם מספר "
            "רישום אחר (מידע אמיתי אבד) לעומת שגיאת פרסור.")

    # פלט בדיקה פרטי (private/, לא לאתר) — כל המחזיקים עם ת.ז/ח.פ בגלוי.
    write_holders_csv(new_reports, rebuild=rebuild, log=log)
    return out


def norm_name(s):
    """נרמול לשם - זהה לזה שבמאגר החברות"""
    if not s:
        return ""
    s = re.sub(r"[\u0591-\u05C7]", "", str(s))
    s = re.sub(r"[^\u05D0-\u05EA]", "", s)
    s = s.replace("בעמ", "")
    return s.replace("י", "").replace("ו", "")

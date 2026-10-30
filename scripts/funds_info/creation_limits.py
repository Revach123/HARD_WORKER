# -*- coding: utf-8 -*-
"""
creation_limits.py — הון כשר / Revach
מגבלות יצירה (טופס ק079) + עמלת יצירה, לקרנות סל וקרנות מחקות.

מקורות:
  1. maya.tase.co.il/api/v1/reports/etfs  (formId=ק079, חלונות של שנה מ-2018)
       -> maya.tase.co.il/api/v1/reports/{id}  (attachments)
       -> mayafiles.tase.co.il/<htm>           (cp1255, כמו ק155 ב-dnm.py)
     "דוח על שינוי מספר יחידות מזערי/מרבי לרכישה".
  2. maya.tase.co.il/api/v1/funds/all — מחירים, שם, סוג, מנהל.

מבנה הטופס (אומת מול 3 דוחות קסם, 9/2026; גרסה ישנה 2022 ר' ALIAS_MAP):
  UnitsTable1105  "מזערי" / "מרבי"  — איזה צד משתנה בדוח הזה
  Date            תאריך כניסת השינוי לתוקף (dd/mm/yyyy)
  לכל קרן בטבלה, בסדר הזה:
    Fund                                   "... (מספר קרן: NNNNNNN)"
    IsExistNumberOfMinimalUnits948Table    "קיימת" / "_________"  (מזערי)
    MinNumber                              מספר יחידות / "_________"
    IsExistNumberOfMinimalUnits948Table_1  "קיימת" / "_________"  (מרבי)
    MaxNumber                              מספר יחידות / "_________"
  דוח מטפל בדרך כלל בצד אחד בלבד. "_________" = הצד לא מדווח בדוח הזה —
  לא "אין מגבלה". לכן כל צד נלקח בנפרד מהדוח האחרון שעוסק בו
  (לפי תאריך תחילת תוקף, ואז מזהה דוח). ערך שאינו "קיימת" ואינו ריק
  (למשל "לא קיימת") נרשם כביטול המגבלה.
  הטופס עוסק ביצירה ("לרכישה") בלבד — אין בו מגבלות פדיון.

עמלות — עמודות סופיות + מקור:
  יצירה: מחירי הרכישה/פדיון של מנהל הקרן במאיה (לא מחיר המסחר בבורסה) כשהם שונים מהדוח השנתי (משקפים חיוב בפועל), אחרת הדוח השנתי.
  פדיון: הדוח השנתי; כשחסר — הנחה שווה ליצירה (בבדיקה 10/2026: 407/407 קרנות עם
  שני הנתונים — שוות).
עמלת יצירה נגזרת: 1 − redemptionPrice/purchasePrice מ-funds/all (ר' price_check). מחיר הפדיון
במאיה הוא ללא ניכוי עמלת פדיון (ר' דוחות קסם), ומחיר הקנייה = NAV + עמלת
יצירה. אומת: 1143700 -> 4050.48/4046.43 = 0.10%, תואם לתשקיף.
עמלת הפדיון אינה ניתנת לגזירה מהמחירים.

מצבים:
  --discover   שומר HTML ושדות של 3 דוחות אחרונים + רשומת funds/all
               ל-data/funds_info/_debug/ (לבדיקה, ללא CSV)
  (ברירת מחדל) -> data/funds_info/creation_limits.csv — שורה לכל קרן סל פעילה:
               עמלות ו"מגבלת שווי הזמנה" מהדוח השנתי (ק121, annual_terms.py)
               + מגבלות ביחידות מק079 (בפועל: קסם, ומקרה בודד בילין).
               מטמונים: k79_cache.json, k121_cache.json (דוח שפוענח לא יורד שוב)
"""
import csv, html, json, os, re, sys, time
from datetime import date

try:
    import requests
except ImportError:
    requests = None

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
OUT_CSV   = os.path.join(ROOT, "data", "funds_info", "creation_limits.csv")
CACHE     = os.path.join(ROOT, "data", "funds_info", "k79_cache.json")
DEBUG_DIR = os.path.join(ROOT, "data", "funds_info", "_debug")
CACHE_VERSION = 3

BASE       = "https://maya.tase.co.il"
LIST_URL   = BASE + "/api/v1/reports/etfs"
META_URL   = BASE + "/api/v1/reports/{id}"
FUNDS_URL  = BASE + "/api/v1/funds/all"
FILES_BASE = "https://mayafiles.tase.co.il/"
FORM_ID    = "ק079"
FIRST_YEAR = 2018

HEADERS = {
    "accept": "application/json, text/plain, */*",
    "accept-language": "he-IL",
    "content-type": "application/json",
    "user-agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36"),
    "referer": BASE + "/he/reports/etfs",
}
GET_HEADERS = {k: v for k, v in HEADERS.items() if k != "content-type"}

_MARKS = "‏‎‪‫‬‭‮ "


def _clean(s):
    s = html.unescape(re.sub(r"<[^>]+>", " ", s or ""))
    for m in _MARKS:
        s = s.replace(m, " ")
    return re.sub(r"\s+", " ", s).strip()


def _req(session, method, url, what, timeout=60, **kw):
    last = None
    for attempt in (1, 2, 3):
        try:
            r = session.request(method, url, timeout=timeout, **kw)
            _ = r.content
            return r
        except Exception as e:
            last = e
            if attempt < 3:
                print(f"  ⚠ {what}: נסיון {attempt} נכשל ({type(e).__name__}); ממתין {attempt * 2}s")
                time.sleep(attempt * 2)
    raise RuntimeError(f"{what} נכשל אחרי 3 נסיונות: {last}") from last


# ── 1. רשימת דוחות ────────────────────────────────────────────────────────────
def _list_window(session, frm, to, out, seen, max_pages=50, form_id=FORM_ID):
    for page in range(1, max_pages + 1):
        body = {"pageNumber": page, "fromDate": frm, "toDate": to,
                "noMeetings": False, "isPriority": False, "isSingle": False,
                "isIntendToTaseMember": False, "by": "company",
                "formId": form_id, "freeText": "", "limit": 20, "offset": (page - 1) * 20}
        r = _req(session, "POST", LIST_URL, f"רשימה {frm[:4]} עמוד {page}",
                 timeout=30, headers=HEADERS, data=json.dumps(body))
        if r.status_code != 200:
            raise RuntimeError(f"רשימה {frm[:10]}..{to[:10]} עמוד {page}: "
                               f"HTTP {r.status_code} {r.text[:400]}")
        data = r.json() or []
        new = 0
        for rep in data:
            rid = rep.get("id")
            if rid in seen:
                continue
            seen.add(rid); new += 1
            out.append({"id": rid,
                        "companies": [(c.get("name") or "").strip() for c in (rep.get("companies") or [])]})
        if not data or new == 0:
            break
        time.sleep(0.2)


def fetch_report_list(session, first_year=FIRST_YEAR, form_id=FORM_ID):
    today = date.today()
    out, seen = [], set()
    for y in range(today.year, first_year - 1, -1):
        frm = f"{y - 1}-12-31T22:00:00.000Z"
        to = (today.isoformat() if y == today.year else f"{y}-12-31") + "T21:00:00.000Z"
        try:
            _list_window(session, frm, to, out, seen, max_pages=60, form_id=form_id)
        except Exception as e:
            print(f"  ⚠ {e}")
    print(f"[{form_id}] {len(out)} דוחות")
    return out


# ── 2. הורדת HTML ─────────────────────────────────────────────────────────────
def fetch_report_html(session, report_id):
    meta = _req(session, "GET", META_URL.format(id=report_id), f"מטא-דאטה {report_id}",
                timeout=30, headers=GET_HEADERS)
    if meta.status_code != 200:
        raise RuntimeError(f"מטא-דאטה {report_id}: HTTP {meta.status_code}")
    atts = (meta.json() or {}).get("attachments") or []
    htm = next((a for a in atts if (a.get("fileType") or "").lower() in ("htm", "html")), None)
    if not htm or not htm.get("url"):
        raise RuntimeError(f"דוח {report_id}: אין attachment מסוג htm")
    r = _req(session, "GET", FILES_BASE + htm["url"].lstrip("/"), f"דוח {report_id}",
             timeout=60, headers=GET_HEADERS)
    if r.status_code != 200:
        raise RuntimeError(f"דוח {report_id}: HTTP {r.status_code}")
    return r.content.decode("cp1255", errors="replace")


# ── 3. פרסינג ─────────────────────────────────────────────────────────────────
_FIELD_RE = re.compile(r'<SPAN\b[^>]*FieldAlias="([^"]*)"[^>]*>(.*?)</SPAN>', re.S | re.I)
_FUND_RE  = re.compile(r"מספר\s*קרן:\s*(\d{6,8})")
BLANK = re.compile(r"^[_\s\-]*$")


def extract_fields(text):
    """[(alias, value, label_before)] לפי סדר במסמך (לשימוש ב-discover)."""
    out, prev_end = [], 0
    for m in _FIELD_RE.finditer(text):
        before = _clean(text[max(prev_end, m.start() - 600):m.start()])[-160:]
        out.append((m.group(1), _clean(m.group(2)), before))
        prev_end = m.end()
    return out


def _num(v):
    try:
        f = float(v.replace(",", "").strip())
        return int(f) if f == int(f) else f
    except ValueError:
        return None


def _side(exists, value):
    """None = הצד לא מדווח בדוח; אחרת {'limit': bool, 'units': int|None}."""
    exists, value = (exists or "").strip(), (value or "").strip()
    if BLANK.match(exists) and BLANK.match(value):
        return None
    if exists == "קיימת" or (BLANK.match(exists) and _num(value) is not None):
        return {"limit": True, "units": _num(value)}
    return {"limit": False, "units": None}          # למשל "לא קיימת" = ביטול


def _iso(d):
    m = re.match(r"(\d{1,2})/(\d{1,2})/(\d{4})", d or "")
    return f"{m.group(3)}-{int(m.group(2)):02d}-{int(m.group(1)):02d}" if m else ""


# גרסת טופס ישנה (2022 ואחורה) -> שמות הגרסה הנוכחית
ALIAS_MAP = {"UnitsNumberChange": "UnitsTable1105", "DateEvent": "Date",
             "IsExsist": "IsExistNumberOfMinimalUnits948Table", "MinNum": "MinNumber",
             "IsExsist2": "IsExistNumberOfMinimalUnits948Table_1", "MaxNum": "MaxNumber"}


def parse_report(text):
    head, rows, cur = {}, [], None
    for m in _FIELD_RE.finditer(text):
        alias, val = ALIAS_MAP.get(m.group(1), m.group(1)), _clean(m.group(2))
        if alias in ("UnitsTable1105", "Date", "FormSubject") and alias not in head:
            head[alias] = val
        elif alias == "Fund":
            fm = _FUND_RE.search(val)
            cur = {"fund": int(fm.group(1)), "name": re.sub(r"\s*\(מספר\s*קרן:.*$", "", val)} if fm else None
            if cur:
                rows.append(cur)
        elif cur is not None and alias in ("IsExistNumberOfMinimalUnits948Table", "MinNumber",
                                           "IsExistNumberOfMinimalUnits948Table_1", "MaxNumber"):
            cur[alias] = val
    out = []
    for r in rows:
        mn = _side(r.get("IsExistNumberOfMinimalUnits948Table"), r.get("MinNumber"))
        mx = _side(r.get("IsExistNumberOfMinimalUnits948Table_1"), r.get("MaxNumber"))
        if mn or mx:
            out.append({"fund": r["fund"], "name": r["name"], "min": mn, "max": mx})
    return {"subject": head.get("UnitsTable1105") or head.get("FormSubject", ""),
            "effective": _iso(head.get("Date")), "rows": out}


# ── 4. funds/all ──────────────────────────────────────────────────────────────
def fetch_all_funds(session):
    body = json.dumps({"pageSize": 5000, "pageNumber": 1}).encode("utf-8")
    h = dict(HEADERS, referer=BASE + "/he/funds/all")
    r = _req(session, "POST", FUNDS_URL, "funds/all", timeout=90, headers=h, data=body)
    r.raise_for_status()
    return {int(f["fundId"]): f for f in r.json() if f.get("fundId") is not None}


def price_check(fr, fee_pct):
    """האם מחירי מאיה (funds/all: מחירי מנהל הקרן ליצירה/פדיון, באגורות, מעוגלים ל-0.01 אג')
    תואמים בדיוק את העמלה מהדוח השנתי.
    נבדק 10/2026 על 407 קרנות סל: היחס הוא רכישה = פדיון ÷ (1 − עמלה), ולא × (1 + עמלה)
    (405 תואמות בדיוק; ×(1+f) לא תאם אף פעם לבדו). מהמחירים לבדם אי אפשר לדעת אם העמלה
    מתווספת למחיר הרכישה או מנוכה ממחיר הפדיון — שתיהן נותנות אותו יחס.
    -> (implied_pct_4dp, status)"""
    pur, red = fr.get("purchasePrice"), fr.get("redemptionPrice")
    if not (isinstance(pur, (int, float)) and isinstance(red, (int, float)) and red > 0 and pur > 0):
        return "", "אין מחירים"
    implied = round((pur - red) / pur * 100, 4)          # f = 1 − פדיון/רכישה
    if fee_pct is None:
        return implied, ""
    if pur == red:
        return implied, "מחיר רכישה = מחיר פדיון"
    expected = red / (1 - fee_pct / 100)
    diff = pur - expected
    tol = 0.0051 * (2 + fee_pct / 100)          # עיגול שני המחירים ל-0.01 אג'
    if abs(diff) <= tol:
        return implied, "מדויק"
    return implied, f"סטייה {diff:+.2f} אג' ({diff / red * 1e4:+.1f} נ\"ב)"


def creation_fee_pct(f):
    """עמלה משתמעת ממחירי מאיה: 1 − פדיון/רכישה (ר' price_check)."""
    p, r = f.get("purchasePrice"), f.get("redemptionPrice")
    if isinstance(p, (int, float)) and isinstance(r, (int, float)) and r > 0 and p > 0:
        return round((p - r) / p * 100, 2)
    return None


# ── main ──────────────────────────────────────────────────────────────────────
ANNUAL_FORM  = "ק121"
ANNUAL_CACHE = os.path.join(ROOT, "data", "funds_info", "k121_cache.json")


def _load_json(path, default):
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            c = json.load(f)
        if c.get("version") == default["version"]:
            return c
    return default


def _save_json(path, obj):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False)


def discover(session):
    os.makedirs(DEBUG_DIR, exist_ok=True)
    reports = sorted(fetch_report_list(session, first_year=date.today().year),
                     key=lambda r: r["id"], reverse=True)
    for rep in reports[:3]:
        text = fetch_report_html(session, rep["id"])
        with open(os.path.join(DEBUG_DIR, f"k79_{rep['id']}.htm"), "w", encoding="utf-8") as f:
            f.write(text)
        with open(os.path.join(DEBUG_DIR, f"k79_{rep['id']}_fields.txt"), "w", encoding="utf-8") as f:
            for alias, value, label in extract_fields(text):
                f.write(f"{alias}\t{value}\t<<{label[-80:]}>>\n")
        p = parse_report(text)
        print(f"[discover] {rep['id']} {rep['companies']} {p['subject']} {p['effective']}: "
              f"{len(p['rows'])} קרנות, לדוגמה {p['rows'][:2]}")


def k079_latest(session):
    """fund -> {"name", "min": (key, side, eff, rid), "max": ...}"""
    cache = _load_json(CACHE, {"version": CACHE_VERSION, "parsed": {}, "empty": []})
    reports = fetch_report_list(session)
    todo = [r for r in reports if str(r["id"]) not in cache["parsed"] and r["id"] not in cache["empty"]]
    print(f"[k079] {len(todo)} דוחות חדשים להורדה")
    for i, rep in enumerate(todo, 1):
        try:
            p = parse_report(fetch_report_html(session, rep["id"]))
        except Exception as e:
            print(f"  ⚠ {rep['id']}: {e}")
            continue
        if p["rows"]:
            p["companies"] = rep["companies"]
            cache["parsed"][str(rep["id"])] = p
        else:
            cache["empty"].append(rep["id"])
        if i % 25 == 0:
            print(f"  {i}/{len(todo)}")
        time.sleep(0.15)
    _save_json(CACHE, cache)
    if cache["empty"]:
        print(f"[k079] ⚠ {len(cache['empty'])} דוחות בלי שורות: {cache['empty'][:10]}")
    latest = {}
    for rid, rec in cache["parsed"].items():
        key = (rec["effective"], int(rid))
        for row in rec["rows"]:
            ent = latest.setdefault(row["fund"], {"name": row["name"]})
            for side in ("min", "max"):
                if row[side] and (side not in ent or key > ent[side][0]):
                    ent[side] = (key, row[side], rec["effective"], int(rid))
    return latest


ANNUAL_PARSER = 3


def _fund_from_htm(session, meta):
    """מספר הקרן מטופס ה-htm של הדוח (שדה Fund / "מספר קרן: N"), כשאינו בשם הקובץ או ב-PDF."""
    htm = next((a for a in (meta.get("attachments") or [])
                if (a.get("fileType") or "").lower() in ("htm", "html")), None)
    if not htm:
        return None
    r = _req(session, "GET", FILES_BASE + htm["url"].lstrip("/"), "htm", timeout=60, headers=GET_HEADERS)
    if r.status_code != 200:
        return None
    m = _FUND_RE.search(r.content.decode("cp1255", errors="replace"))
    return int(m.group(1)) if m else None


def annual_latest(session, etf_ids):
    """fund -> {creation_fee, redemption_fee, limit, limit_text, rid, date} מהדוח השנתי האחרון."""
    import annual_terms as at
    cache = _load_json(ANNUAL_CACHE, {"version": 1, "parsed": {}, "failed": []})
    reports = fetch_report_list(session, first_year=date.today().year - 1, form_id=ANNUAL_FORM)
    # חדשים + רשומות חלקיות שעוד לא נוסו בגרסת הפרסינג הנוכחית (pypdf+pdfium, htm)
    todo = [r for r in reports if str(r["id"]) not in cache["parsed"]
            or ((at.needs_retry(cache["parsed"][str(r["id"])])
                 or (cache["parsed"][str(r["id"])].get("creation_fee") or 0) >= 1)   # ".1%" שנקרא 1%
                and cache["parsed"][str(r["id"])].get("parser", 1) < ANNUAL_PARSER)]
    print(f"[ק121] {len(todo)} דוחות שנתיים להורדה (חדשים + ניסיון חוזר לחלקיים)")
    fails = 0
    for i, rep in enumerate(todo, 1):
        try:
            mr = _req(session, "GET", META_URL.format(id=rep["id"]), f"meta {rep['id']}",
                      timeout=30, headers=GET_HEADERS)
            if mr.status_code != 200:
                raise RuntimeError(f"meta HTTP {mr.status_code}")
            meta = mr.json()
            att = at.pdf_attachment(meta)
            if not att:
                raise RuntimeError("אין PDF")
            r = _req(session, "GET", FILES_BASE + att["url"].lstrip("/"), f"pdf {rep['id']}",
                     timeout=120, headers=GET_HEADERS)
            if r.status_code != 200:
                raise RuntimeError(f"HTTP {r.status_code}")
            texts = at.pdf_texts(r.content)
            rec = at.parse_annual_multi(texts)
            rec["fund"] = None
            for t in texts:
                rec["fund"] = rec["fund"] or at.fund_of(att.get("fileName"), t, etf_ids)
            if not rec["fund"]:
                rec["fund"] = _fund_from_htm(session, meta)
            rec["date"] = (meta.get("publishDate") or "")[:10]
            rec["parser"] = ANNUAL_PARSER
            cache["parsed"][str(rep["id"])] = rec
            fails = 0
        except Exception as e:
            print(f"  ⚠ ק121 {rep['id']}: {e}")
            fails += 1
            if fails >= 10:      # מאיה חוסמת את ה-runner — לא להמשיך להפציץ; ננסה בריצה הבאה
                print(f"[ק121] ⚠ {fails} כשלונות רצופים — עצירת ההורדות בריצה זו (המטמון נשמר)")
                break
        if i % 25 == 0:
            print(f"  {i}/{len(todo)}")
            _save_json(ANNUAL_CACHE, cache)
        time.sleep(0.15)
    _save_json(ANNUAL_CACHE, cache)
    latest = {}
    for rid, rec in cache["parsed"].items():
        fid = rec.get("fund")
        if fid and (fid not in latest or int(rid) > latest[fid]["rid"]):
            latest[fid] = dict(rec, rid=int(rid))
    return latest


# מנהלים שבדוח השנתי שלהם אין שורת "מגבלת שווי הזמנה" — ההצהרה בתשקיף העיקרי:
# "לגבי כל קרנות הסל המנוהלות על ידי מנהל הקרן לא נקבע מספר יחידות מזערי ומירבי
#  להזמנה שלא במסחר בבורסה"
PROSPECTUS_NO_LIMIT = {
    "הראל": "אין (תשקיף עיקרי 2.5.2024, P1588270)",
    "מגדל": "אין (תשקיף עיקרי 30.1.2025, P1643524)",
}


def limit_status(annual_rec, manager, mgr_statement=None):
    """mgr_statement: הצהרה אחידה שנמצאה בדוחות שנתיים אחרים של אותו מנהל ("אין"/"יש (ק079)")."""
    lim = (annual_rec or {}).get("limit") or "אין דוח שנתי"
    if lim in ("לא מצוין", "אין דוח שנתי"):
        for k, v in PROSPECTUS_NO_LIMIT.items():
            if k in (manager or ""):
                return v
        if mgr_statement:
            return f"{mgr_statement} (לפי דוחות אחרים של המנהל)"
    return lim


def build(session):
    funds = fetch_all_funds(session)
    etfs = {fid: f for fid, f in funds.items() if f.get("fundType") == "קרן סל"}
    k079 = k079_latest(session)
    annual = annual_latest(session, set(etfs))

    # הצהרה אחידה לכל מנהל: אם כל הדוחות שבהם נמצאה שורת מגבלה אומרים אותו דבר
    seen = {}
    for fid, an in annual.items():
        if fid in etfs and an.get("limit") in ("אין", "יש (ק079)"):
            seen.setdefault(etfs[fid].get("managerName"), set()).add(an["limit"])
    mgr_stmt = {m: next(iter(v)) for m, v in seen.items() if len(v) == 1}

    os.makedirs(os.path.dirname(OUT_CSV), exist_ok=True)
    stats = {"annual": 0, "k079": 0}
    checks = []    # (fid, manager, annual_fee, implied, status)
    with open(OUT_CSV, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["מספר קרן", "שם קרן", "מנהל",
                    "עמלת יצירה %", "עמלת פדיון %", "מקור עמלת יצירה", "מקור עמלת פדיון",
                    "עמלת יצירה % (דוח שנתי)", "עמלת פדיון % (דוח שנתי)", "עמלת יצירה % (נגזרת ממחירים)",
                    "מחיר רכישה (מאיה, אג')", "מחיר פדיון (מאיה, אג')", "תאריך מחירים",
                    "עמלה משתמעת % (4 ספרות)", "התאמת מחירים לדוח",
                    "מגבלת שווי הזמנה (דוח שנתי)",
                    "יחידות מזערי ליצירה", "שווי מזערי (ש\"ח)", "תוקף מזערי", "דוח מזערי",
                    "יחידות מרבי ליצירה", "שווי מרבי (ש\"ח)", "תוקף מרבי", "דוח מרבי",
                    "מחיר פדיון (ש\"ח)", "דוח שנתי", "תאריך דוח שנתי", "נוסח המגבלה"])
        for fid in sorted(etfs):
            fr, an, ent = etfs[fid], annual.get(fid) or {}, k079.get(fid) or {}
            stats["annual"] += bool(an); stats["k079"] += bool(ent)
            red = fr.get("redemptionPrice")
            red = red / 100 if isinstance(red, (int, float)) else None

            def side(sd):
                if sd not in ent:
                    return ["", "", "", ""]
                _, v, eff, rid = ent[sd]
                if not v["limit"]:
                    return ["אין", "", eff, rid]
                u = v["units"]
                return [u if u is not None else "", round(u * red) if (u is not None and red) else "", eff, rid]

            fee = creation_fee_pct(fr)
            a_c, a_r = an.get("creation_fee"), an.get("redemption_fee")
            # מחירי מאיה משקפים את החיוב בפועל, אלא כשהמחיר נמוך מדי לעיגול (נגזר=0)
            if fee and (a_c is None or abs(a_c - fee) > 0.02):
                c_final, c_src = fee, "מחירי רכישה/פדיון (מאיה)"
            elif a_c is not None:
                c_final, c_src = a_c, "דוח שנתי"
            else:
                c_final, c_src = fee, ("מחירי רכישה/פדיון (מאיה)" if fee is not None else "")
            if a_r is not None:
                r_final, r_src = a_r, "דוח שנתי"
            elif c_final is not None:
                r_final, r_src = c_final, "הנחה: שווה לעמלת היצירה"
            else:
                r_final, r_src = None, ""
            nz = lambda v: "" if v is None else v
            implied, pstat = price_check(fr, a_c)
            if a_c is not None:
                checks.append((fid, (fr.get("managerName") or "").strip(), a_c, implied, pstat))
            w.writerow([fid, fr.get("name", ""), fr.get("managerName", ""),
                        nz(c_final), nz(r_final), c_src, r_src,
                        nz(a_c), nz(a_r), nz(fee),
                        nz(fr.get("purchasePrice")), nz(fr.get("redemptionPrice")), fr.get("ratesAsOf", ""),
                        implied, pstat,
                        limit_status(an, fr.get("managerName"), mgr_stmt.get(fr.get("managerName"))),
                        *side("min"), *side("max"),
                        round(red, 2) if red else "", an.get("rid", ""), an.get("date", ""),
                        an.get("limit_text", "")])
    print(f"[build] {len(etfs)} קרנות סל; דוח שנתי ל-{stats['annual']}, ק079 ל-{stats['k079']} -> {OUT_CSV}")
    from collections import Counter
    st = Counter("מדויק" if c[4] == "מדויק" else ("אין מחירים" if c[4] == "אין מחירים" else "סטייה") for c in checks)
    print(f"[price-check] {len(checks)} קרנות עם עמלה מהדוח השנתי: {dict(st)}")
    by_mgr = {}
    for c in checks:
        by_mgr.setdefault(c[1], Counter())["מדויק" if c[4] == "מדויק" else "אחר"] += 1
    for m, v in sorted(by_mgr.items()):
        print(f"[price-check]   {m}: {dict(v)}")
    for c in [c for c in checks if c[4] not in ("מדויק",)][:80]:
        print(f"[price-check]   ✗ {c[0]} {c[1]} דוח={c[2]}% משתמע={c[3]}% {c[4]}")


if __name__ == "__main__":
    if requests is None:
        sys.exit("requires: pip install requests")
    s = requests.Session()
    discover(s) if "--discover" in sys.argv else build(s)

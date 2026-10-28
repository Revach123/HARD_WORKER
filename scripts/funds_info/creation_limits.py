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

מבנה הטופס (אומת מול 3 דוחות קסם, 9/2026):
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

עמלת יצירה: purchasePrice/redemptionPrice − 1 מ-funds/all. מחיר הפדיון
במאיה הוא ללא ניכוי עמלת פדיון (ר' דוחות קסם), ומחיר הקנייה = NAV + עמלת
יצירה. אומת: 1143700 -> 4050.48/4046.43 = 0.10%, תואם לתשקיף.
עמלת הפדיון אינה ניתנת לגזירה מהמחירים.

מצבים:
  --discover   שומר HTML ושדות של 3 דוחות אחרונים + רשומת funds/all
               ל-data/funds_info/_debug/ (לבדיקה, ללא CSV)
  (ברירת מחדל) -> data/funds_info/creation_limits.csv
               מטמון: data/funds_info/k79_cache.json (דוח שפוענח לא יורד שוב)
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
CACHE_VERSION = 2

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
def _list_window(session, frm, to, out, seen, max_pages=50):
    for page in range(1, max_pages + 1):
        body = {"pageNumber": page, "fromDate": frm, "toDate": to,
                "noMeetings": False, "isPriority": False, "isSingle": False,
                "isIntendToTaseMember": False, "by": "company",
                "formId": FORM_ID, "freeText": "", "limit": 20, "offset": (page - 1) * 20}
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


def fetch_report_list(session, first_year=FIRST_YEAR):
    today = date.today()
    out, seen = [], set()
    for y in range(today.year, first_year - 1, -1):
        frm = f"{y - 1}-12-31T22:00:00.000Z"
        to = (today.isoformat() if y == today.year else f"{y}-12-31") + "T21:00:00.000Z"
        try:
            _list_window(session, frm, to, out, seen)
        except Exception as e:
            print(f"  ⚠ {e}")
    print(f"[k079] {len(out)} דוחות")
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


def parse_report(text):
    head, rows, cur = {}, [], None
    for m in _FIELD_RE.finditer(text):
        alias, val = m.group(1), _clean(m.group(2))
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


def creation_fee_pct(f):
    p, r = f.get("purchasePrice"), f.get("redemptionPrice")
    if isinstance(p, (int, float)) and isinstance(r, (int, float)) and r > 0:
        return round((p / r - 1) * 100, 2)
    return None


# ── main ──────────────────────────────────────────────────────────────────────
def _load_cache():
    if os.path.exists(CACHE):
        with open(CACHE, encoding="utf-8") as f:
            c = json.load(f)
        if c.get("version") == CACHE_VERSION:
            return c
    return {"version": CACHE_VERSION, "parsed": {}, "empty": []}


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
    funds = fetch_all_funds(session)
    with open(os.path.join(DEBUG_DIR, "funds_all_sample.json"), "w", encoding="utf-8") as f:
        json.dump(funds.get(1143700) or next(iter(funds.values())), f, ensure_ascii=False, indent=1)


def build(session):
    cache = _load_cache()
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
    with open(CACHE, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False)
    if cache["empty"]:
        print(f"[k079] ⚠ {len(cache['empty'])} דוחות בלי שורות: {cache['empty'][:10]}")

    # לכל קרן ולכל צד — הדוח האחרון שעוסק בצד הזה
    latest = {}   # fund -> {"min": (key, side, eff, rid), "max": ..., "name": ...}
    for rid, rec in cache["parsed"].items():
        key = (rec["effective"], int(rid))
        for row in rec["rows"]:
            ent = latest.setdefault(row["fund"], {"name": row["name"]})
            for s in ("min", "max"):
                if row[s] and (s not in ent or key > ent[s][0]):
                    ent[s] = (key, row[s], rec["effective"], int(rid))

    funds = fetch_all_funds(session)
    os.makedirs(os.path.dirname(OUT_CSV), exist_ok=True)
    n_active = 0
    with open(OUT_CSV, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["מספר קרן", "שם קרן", "מנהל", "סוג", "פעילה במאיה",
                    "יחידות מזערי ליצירה", "שווי מזערי (ש\"ח)", "תוקף מזערי", "דוח מזערי",
                    "יחידות מרבי ליצירה", "שווי מרבי (ש\"ח)", "תוקף מרבי", "דוח מרבי",
                    "מחיר פדיון (ש\"ח)", "מחיר קנייה (ש\"ח)", "עמלת יצירה % (נגזרת)"])
        for fid in sorted(latest):
            ent, fr = latest[fid], funds.get(fid) or {}
            n_active += bool(fr)
            red = fr.get("redemptionPrice")
            red = red / 100 if isinstance(red, (int, float)) else None
            pur = fr.get("purchasePrice")
            pur = pur / 100 if isinstance(pur, (int, float)) else None

            def side(s):
                if s not in ent:
                    return ["", "", "", ""]
                _, v, eff, rid = ent[s]
                if not v["limit"]:
                    return ["אין", "", eff, rid]
                u = v["units"]
                return [u if u is not None else "", round(u * red) if (u is not None and red) else "", eff, rid]

            fee = creation_fee_pct(fr)
            w.writerow([fid, fr.get("name") or ent["name"], fr.get("managerName", ""),
                        fr.get("fundType", ""), "כן" if fr else "לא",
                        *side("min"), *side("max"),
                        round(red, 2) if red else "", round(pur, 2) if pur else "",
                        fee if fee is not None else ""])
    print(f"[k079] wrote {len(latest)} קרנות ({n_active} פעילות במאיה) -> {OUT_CSV}")


if __name__ == "__main__":
    if requests is None:
        sys.exit("requires: pip install requests")
    s = requests.Session()
    discover(s) if "--discover" in sys.argv else build(s)

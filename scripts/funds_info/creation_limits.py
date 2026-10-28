# -*- coding: utf-8 -*-
"""
creation_limits.py — הון כשר / Revach
מגבלות יצירה (ק79) + עמלת יצירה/פדיון לקרנות סל וקרנות מחקות.

מקורות:
  1. maya.tase.co.il/api/v1/reports/etfs  (formId=ק79, כל ההיסטוריה מ-2018)
       -> maya.tase.co.il/api/v1/reports/{id}  (attachments)
       -> mayafiles.tase.co.il/<htm>           (cp1255, כמו ק155 ב-dnm.py)
     "דוח על שינוי מספר יחידות מזערי/מרבי לרכישה". לכל קרן נלקח הדוח האחרון
     שמזכיר אותה. הדוח מדווח ביחידות (ע.נ.), לא בש"ח.
  2. maya.tase.co.il/api/v1/funds/all — מחיר פדיון (לתרגום יחידות לש"ח)
     ועמלת יצירה/פדיון, אם קיים שדה כזה ברשומה (מאותר אוטומטית, ר' _fee_key).

מצבים:
  python creation_limits.py --discover
      לא כותב CSV. שומר ל-data/funds_info/_debug/ את ה-HTML הגולמי של 3 דוחות
      ק79 אחרונים, רשימת כל ה-FieldAlias שלהם עם הטקסט שלפני כל שדה, ואת
      המפתחות של רשומת קרן סל אחת מ-funds/all. להרצה ראשונה — כדי לאמת את
      הפרסינג מול המבנה האמיתי של הטופס.
  python creation_limits.py
      משיכה מלאה -> data/funds_info/creation_limits.csv
      מטמון: data/funds_info/k79_cache.json (דוחות שכבר פוענחו לא יורדו שוב).

הפרסינג של ק79 בנוי על זיהוי לפי תווית ולא על שמות FieldAlias קשיחים,
כי שמות השדות של הטופס לא אומתו: לכל שדה נבדק הטקסט שלפניו —
"מזערי/מינימלי" -> min, "מרבי/מירבי/מקסימלי" -> max, ומספר קרן בן 6-8 ספרות
פותח שורה חדשה. דוח שלא פוענח נרשם ב-unparsed במטמון ולא נשבר.
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

BASE       = "https://maya.tase.co.il"
LIST_URL   = BASE + "/api/v1/reports/etfs"
META_URL   = BASE + "/api/v1/reports/{id}"
FUNDS_URL  = BASE + "/api/v1/funds/all"
FILES_BASE = "https://mayafiles.tase.co.il/"
FORM_IDS   = ["ק79", "ק079"]          # הכתיב במאיה לא אומת — נבדקים שניהם

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


def _req(session, method, url, what, **kw):
    last = None
    for attempt in (1, 2, 3):
        try:
            r = session.request(method, url, timeout=kw.pop("timeout", 60), **kw)
            _ = r.content
            return r
        except Exception as e:
            last = e
            if attempt < 3:
                print(f"  ⚠ {what}: נסיון {attempt} נכשל ({type(e).__name__}); ממתין {attempt * 2}s")
                time.sleep(attempt * 2)
    raise RuntimeError(f"{what} נכשל אחרי 3 נסיונות: {last}") from last


# ── 1. רשימת דוחות ק79 ────────────────────────────────────────────────────────
def _list_window(session, form_id, frm, to, out, seen, max_pages=50):
    for page in range(1, max_pages + 1):
        body = {"pageNumber": page, "fromDate": frm, "toDate": to,
                "noMeetings": False, "isPriority": False, "isSingle": False,
                "isIntendToTaseMember": False, "by": "company",
                "formId": form_id, "freeText": "", "limit": 20, "offset": (page - 1) * 20}
        r = _req(session, "POST", LIST_URL, f"רשימת {form_id} עמוד {page}",
                 headers=HEADERS, data=json.dumps(body), timeout=30)
        if r.status_code != 200:
            raise RuntimeError(f"רשימת {form_id} {frm[:10]}..{to[:10]} עמוד {page}: "
                               f"HTTP {r.status_code} {r.text[:400]}")
        data = r.json() or []
        new = 0
        for rep in data:
            rid = rep.get("id")
            if rid in seen:
                continue
            seen.add(rid); new += 1
            parts = (rep.get("title") or "").split("-")
            out.append({"id": rid,
                        "date": parts[1].strip() if len(parts) > 1 else "",
                        "companies": [(c.get("name") or "").strip() for c in (rep.get("companies") or [])]})
        if not data or new == 0:
            break
        time.sleep(0.2)


def fetch_report_list(session, form_id, years=None):
    """[{'id','date','companies'}] — חלונות של שנה (כמו dnm.py), מהשנה הנוכחית אחורה.
    years=None -> מ-2018. חלון שנכשל לא עוצר את השאר (מודפס)."""
    today = date.today()
    first = 2018 if years is None else today.year - years + 1
    out, seen, errors = [], set(), []
    for y in range(today.year, first - 1, -1):
        frm = f"{y - 1}-12-31T22:00:00.000Z"
        to = (today.isoformat() if y == today.year else f"{y}-12-31") + "T21:00:00.000Z"
        try:
            _list_window(session, form_id, frm, to, out, seen)
        except Exception as e:
            errors.append(str(e))
            print(f"  ⚠ {e}")
    if not out and errors:
        raise RuntimeError(errors[0])
    return out


def resolve_form_list(session):
    for fid in FORM_IDS:
        try:
            lst = fetch_report_list(session, fid)
        except Exception as e:
            print(f"[k79] formId={fid}: {e}")
            continue
        print(f"[k79] formId={fid}: {len(lst)} דוחות")
        if lst:
            return fid, lst
    raise RuntimeError("לא נמצאו דוחות ק79 באף אחד מהכתיבים: " + ", ".join(FORM_IDS))


# ── 2. הורדת HTML של דוח ─────────────────────────────────────────────────────
def fetch_report_html(session, report_id):
    meta = _req(session, "GET", META_URL.format(id=report_id), f"מטא-דאטה {report_id}",
                headers=GET_HEADERS, timeout=30)
    if meta.status_code != 200:
        raise RuntimeError(f"מטא-דאטה {report_id}: HTTP {meta.status_code}")
    atts = (meta.json() or {}).get("attachments") or []
    htm = next((a for a in atts if (a.get("fileType") or "").lower() in ("htm", "html")), None)
    if not htm or not htm.get("url"):
        raise RuntimeError(f"דוח {report_id}: אין attachment מסוג htm")
    r = _req(session, "GET", FILES_BASE + htm["url"].lstrip("/"), f"דוח {report_id}",
             headers=GET_HEADERS, timeout=60)
    if r.status_code != 200:
        raise RuntimeError(f"דוח {report_id}: HTTP {r.status_code}")
    return r.content.decode("cp1255", errors="replace")


# ── 3. פרסינג ────────────────────────────────────────────────────────────────
_FIELD_RE = re.compile(r'<SPAN\b[^>]*FieldAlias="([^"]*)"[^>]*>(.*?)</SPAN>', re.S | re.I)
_FUND_RE  = re.compile(r"(?<!\d)(\d{6,8})(?!\d)")
_NUM_RE   = re.compile(r"^-?[\d,]+(?:\.\d+)?$")
MIN_WORDS = ("מזערי", "מזערית", "מינימלי", "מינימום", "מיזערי")
MAX_WORDS = ("מרבי", "מירבי", "מרבית", "מירבית", "מקסימלי", "מקסימום")
FUND_WORDS = ("מספר קרן", "מס' קרן", "מספר בורסה", "מספר נייר", "מס' נייר")


def extract_fields(text):
    """[(alias, value, label_before)] לפי סדר במסמך."""
    out, prev_end = [], 0
    for m in _FIELD_RE.finditer(text):
        before = _clean(text[max(prev_end, m.start() - 600):m.start()])[-160:]
        out.append((m.group(1), _clean(m.group(2)), before))
        prev_end = m.end()
    return out


def _kind(alias, value, label):
    a, lab = alias.lower(), label
    if any(w in lab for w in FUND_WORDS) or re.search(r"fund|keren|security|nayar", a):
        if _FUND_RE.search(value):
            return "fund"
    if not _NUM_RE.match(value.replace(" ", "")):
        return None
    tail = lab[-60:]
    if re.search(r"min|mizari|mazari", a) or any(w in tail for w in MIN_WORDS):
        return "min"
    if re.search(r"max|merabi|mirbi", a) or any(w in tail for w in MAX_WORDS):
        return "max"
    return None


def _to_num(v):
    try:
        f = float(v.replace(",", "").replace(" ", ""))
        return int(f) if f == int(f) else f
    except ValueError:
        return None


def parse_report(text):
    """[{fund, min_units, max_units}] — שורה חדשה בכל מספר קרן."""
    rows, cur = [], None
    for alias, value, label in extract_fields(text):
        k = _kind(alias, value, label)
        if k == "fund":
            fid = int(_FUND_RE.search(value).group(1))
            if cur and cur["fund"] == fid:
                continue
            cur = {"fund": fid, "min_units": None, "max_units": None}
            rows.append(cur)
        elif k in ("min", "max") and cur is not None:
            key = k + "_units"
            if cur[key] is None:
                cur[key] = _to_num(value)
    return [r for r in rows if r["min_units"] is not None or r["max_units"] is not None]


# ── 4. funds/all: מחיר + עמלה ────────────────────────────────────────────────
def fetch_all_funds(session):
    body = json.dumps({"pageSize": 5000, "pageNumber": 1}).encode("utf-8")
    h = dict(HEADERS, referer=BASE + "/he/funds/all")
    r = _req(session, "POST", FUNDS_URL, "funds/all", headers=h, data=body, timeout=90)
    r.raise_for_status()
    data = r.json()
    if isinstance(data, dict):
        data = data.get("funds") or data.get("data") or data.get("items") or []
    return {int(f["fundId"] if "fundId" in f else f.get("id")): f for f in data
            if (f.get("fundId") or f.get("id"))}


_FEE_KEY_RE = re.compile(r"(creat|redemp|exit|entry|purchas|sell).*(fee|commission|rate|percent)", re.I)


def _fee_key(sample):
    """שם השדה של עמלת יצירה/פדיון ברשומת funds/all, אם קיים."""
    for k in sample:
        if _FEE_KEY_RE.search(k) and "price" not in k.lower():
            return k
    return None


# ── main ─────────────────────────────────────────────────────────────────────
def _load_cache():
    if os.path.exists(CACHE):
        with open(CACHE, encoding="utf-8") as f:
            return json.load(f)
    return {"parsed": {}, "unparsed": []}


def discover(session):
    os.makedirs(DEBUG_DIR, exist_ok=True)
    form_id, reports = resolve_form_list(session)
    reports.sort(key=lambda r: r["id"], reverse=True)
    for rep in reports[:3]:
        text = fetch_report_html(session, rep["id"])
        with open(os.path.join(DEBUG_DIR, f"k79_{rep['id']}.htm"), "w", encoding="utf-8") as f:
            f.write(text)
        with open(os.path.join(DEBUG_DIR, f"k79_{rep['id']}_fields.txt"), "w", encoding="utf-8") as f:
            for alias, value, label in extract_fields(text):
                f.write(f"{alias}\t{value}\t<<{label[-80:]}>>\n")
        print(f"[discover] {rep['id']} {rep['companies']}: parsed -> {parse_report(text)}")
    funds = fetch_all_funds(session)
    sample = funds.get(1143700) or next(iter(funds.values()))
    with open(os.path.join(DEBUG_DIR, "funds_all_sample.json"), "w", encoding="utf-8") as f:
        json.dump(sample, f, ensure_ascii=False, indent=1)
    print(f"[discover] funds/all keys: {sorted(sample)}")
    print(f"[discover] fee key guess: {_fee_key(sample)}")
    print(f"[discover] formId בשימוש: {form_id}. קבצים ב-{DEBUG_DIR}")


def build(session):
    cache = _load_cache()
    _, reports = resolve_form_list(session)
    todo = [r for r in reports
            if str(r["id"]) not in cache["parsed"] and r["id"] not in cache["unparsed"]]
    print(f"[k79] {len(reports)} דוחות, {len(todo)} חדשים להורדה")
    for i, rep in enumerate(todo, 1):
        try:
            rows = parse_report(fetch_report_html(session, rep["id"]))
        except Exception as e:
            print(f"  ⚠ {rep['id']}: {e}")
            continue
        if rows:
            cache["parsed"][str(rep["id"])] = {"date": rep["date"], "companies": rep["companies"], "rows": rows}
        else:
            cache["unparsed"].append(rep["id"])
        if i % 20 == 0:
            print(f"  {i}/{len(todo)}")
        time.sleep(0.15)
    with open(CACHE, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False)
    if cache["unparsed"]:
        print(f"[k79] ⚠ {len(cache['unparsed'])} דוחות בלי שורות מזוהות (ר' --discover)")

    latest = {}   # fund -> (report_id, date, row) — id גבוה = חדש יותר
    for rid, rec in cache["parsed"].items():
        for row in rec["rows"]:
            cur = latest.get(row["fund"])
            if cur is None or int(rid) > cur[0]:
                latest[row["fund"]] = (int(rid), rec["date"], row)

    funds = fetch_all_funds(session)
    fee_key = _fee_key(next(iter(funds.values()))) if funds else None
    print(f"[k79] {len(latest)} קרנות עם ק79; שדה עמלה ב-funds/all: {fee_key or 'לא נמצא'}")

    os.makedirs(os.path.dirname(OUT_CSV), exist_ok=True)
    with open(OUT_CSV, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["מספר קרן", "שם קרן", "יחידות מזערי ליצירה", "יחידות מרבי ליצירה",
                    "מחיר פדיון (ש\"ח)", "שווי מזערי (ש\"ח)", "שווי מרבי (ש\"ח)",
                    "עמלת יצירה/פדיון", "תאריך דוח ק79", "מזהה דוח"])
        for fid in sorted(latest):
            rid, rdate, row = latest[fid]
            f_rec = funds.get(fid) or {}
            price = f_rec.get("redemptionPrice")
            price = round(price / 100, 4) if isinstance(price, (int, float)) else None
            val = lambda u: round(u * price) if (u is not None and price) else ""
            w.writerow([fid, f_rec.get("fundName") or f_rec.get("name") or "",
                        row["min_units"] if row["min_units"] is not None else "",
                        row["max_units"] if row["max_units"] is not None else "",
                        price if price is not None else "", val(row["min_units"]), val(row["max_units"]),
                        f_rec.get(fee_key, "") if fee_key else "", rdate, rid])
    print(f"[k79] wrote {len(latest)} -> {OUT_CSV}")


if __name__ == "__main__":
    if requests is None:
        sys.exit("requires: pip install requests")
    s = requests.Session()
    discover(s) if "--discover" in sys.argv else build(s)

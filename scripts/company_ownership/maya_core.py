#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
maya_core.py — מנוע בעלות מבוסס JSON (ללא פרסור HTML).

מקורות:
  datawise  basic-securities/companies-list   → רשימת החברות (issuerId, ח.פ, ענף)
  maya      interested-parties/by_company      → מחזיקים + מספר נייר + סוג נייר + %
  maya      interested-parties/distribution    → פילוח מוסדי/בעלי-עניין/ציבור
  maya      board-and-management/by_company     → דירקטוריון והנהלה

נקי, מספרי, בעברית, יציב. משמש את update.py. אינו רץ לבד.

update.py הוא הקורא היחיד — הוא החליף את full-update.py ואת daily.py, שהיו
זהים בפועל (ה-JSON תמיד מחזיר מצב נוכחי; אין היסטוריה או דלתא לתחזק).
"""

import os
import re
import json
import time
import datetime
import requests
import pandas as pd
from concurrent.futures import ThreadPoolExecutor, as_completed

MAYA = "https://maya.tase.co.il/api/v1"
DATAWISE = "https://datawise.tase.co.il/v1"
# מפתח datawise. שים לב ל-`or` ולא ל-default של get(): ב-GitHub Actions,
# `KEY: ${{ secrets.MISSING }}` מגדיר את המשתנה למחרוזת ריקה ולא משאיר אותו
# חסר — ואז get(X, default) מחזיר "" והברירת מחדל לעולם לא נכנסת. התוצאה
# היא apikey ריק ו-403 שנראה כמו בעיית הרשאות במקום כמו סוד חסר.
# שם הסוד ב-repo הוא TASE_APIKEY; ה-workflow ממפה אותו ל-DATAWISE_APIKEY,
# ושניהם נבדקים כאן כדי שהרצה מקומית תעבוד בלי קשר לשם שהוגדר.
DATAWISE_KEY = (os.environ.get("DATAWISE_APIKEY")
                or os.environ.get("TASE_APIKEY")
                or "OpB1TDhiRrF5kbQtjx75Qgcm6Csh31to")
DW_HEADERS = {"accept": "application/json", "accept-language": "he-IL",
              "apikey": DATAWISE_KEY}
OUTPUT_DIR = os.environ.get("OUTPUT_DIR", ".")
WORKERS = int(os.environ.get("WORKERS", "8"))

MAYA_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "he-IL",
    "Origin": "https://maya.tase.co.il",
    "Referer": "https://maya.tase.co.il/he",
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/125.0.0.0 Safari/537.36"),
    "X-Maya-With": "allow",
}


def _out(name):
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    return os.path.join(OUTPUT_DIR, name)


# מקרא סוגי ניירות (securityCategory של maya = securityMainTypeCode של datawise)
SECURITY_TYPES = {1: "מניה", 2: "אופציה", 3: 'אג"ח להמרה', 4: 'אג"ח ממשלתי',
    5: 'אג"ח קונצרני', 6: "קבוצת תשואה", 7: 'יחידת מו"פ', 8: 'מק"מ', 9: "זכויות",
    10: "קרן נאמנות", 11: 'אג"ח-אופציה', 12: "יחידת השתתפות", 13: "אופצית רכישה",
    14: "אופציה דולרית", 15: "אופציה לאגח", 16: "אופציה CALL", 17: "אופציה PUT",
    18: "חוזה עתידי", 19: "אופציה CALL שב.", 20: "אופציה PUT שב.", 21: "חוזה עתידי שב.",
    32: "תעודות סל מניות", 33: "ת.בחסר מניות", 34: "א.כיסוי רכש מנ.", 35: "א.כיסוי מכר מנ.",
    36: 'תעודות סל לאג"ח', 37: 'ת.בחסר לאג"ח', 38: "א.כיסוי רכש אגח", 39: "א.כיסוי מכר אגח",
    40: "תעודת פקדון", 41: "קרן הייטק", 42: "קרן סל במניות", 43: "קרן סל באגח",
    44: "קרן חוץ מניות", 45: "קרן חוץ אגח", 46: "קרן השקעה", 90: "זיהוי הנפקה"}


def fetch_security_legend():
    """מחזיר {securityFullTypeCode(4 ספרות): (סוג רחב, תיאור מפורט)} מ-datawise."""
    try:
        r = requests.get(f"{DATAWISE}/basic-securities/securities-types",
                         headers=DW_HEADERS, timeout=60)
        r.raise_for_status()
        res = r.json()["securitiesTypes"]["result"]
        return {str(x.get("securityFullTypeCode")).zfill(4):
                (x.get("securityMainTypeDesc"), x.get("securityTypeDesc"))
                for x in res if x.get("securityFullTypeCode") is not None}
    except Exception:
        return {}


def _trade_dates(n=5):
    """n ימי המסחר האחרונים (מ-tase-schedules), מהחדש לישן, כרשימת (y,m,d)."""
    today = datetime.date.today()
    to = (today + datetime.timedelta(days=1)).isoformat()
    r = requests.get(f"{DATAWISE}/tase-schedules?fromDate=2021-05-25&toDate={to}",
                     headers=DW_HEADERS, timeout=60)
    r.raise_for_status()
    res = r.json()["tase-schedules"]["result"]
    days = sorted({x["date"][:10] for x in res
                   if x.get("isTradeDate") and x["date"][:10] <= today.isoformat()},
                  reverse=True)
    return [(d[:4], d[5:7], d[8:10]) for d in days[:n]]


def fetch_securities_map():
    """מיפוי securityId → (סוג רחב, תיאור מפורט). מדפיס אבחון; {} אם נכשל."""
    try:
        dates = _trade_dates()
    except Exception as e:
        print(f"  אזהרה: tase-schedules נכשל ({e}) — securityType יישאר ריק.")
        return {}

    legend = fetch_security_legend()
    res = None
    for (y, m, d) in dates:                 # fallback: מנסה כמה ימי מסחר אחרונים
        try:
            r = requests.get(
                f"{DATAWISE}/basic-securities/trade-securities-list/{y}/{m}/{d}",
                headers=DW_HEADERS, timeout=90)
            r.raise_for_status()
            res = r.json()["tradeSecuritiesList"]["result"]
            if res:
                print(f"  רשימת ניירות ליום {y}-{m}-{d}: {len(res)} ניירות")
                break
        except Exception as e:
            print(f"  trade-securities-list {y}-{m}-{d} נכשל ({e})")
    if not res:
        print("  אזהרה: אין רשימת ניירות — securityType יישאר ריק.")
        return {}

    out = {}
    for x in res:
        sid = x.get("securityId")
        full = x.get("securityFullTypeCode")
        if sid is None or full is None:
            continue
        full = str(full).zfill(4)
        main, detail = legend.get(full, (None, None))
        if main is None:
            main = SECURITY_TYPES.get(int(full[:2]))
        out[int(sid)] = (main, detail)
    print(f"  מופו {len(out)} ניירות לסוג.")
    return out


_QUOTES = "\"'\u05f3\u05f4\u2018\u2019\u201c\u201d`"
_SUFFIX = {"בעמ", "ltd", "ltd.", "limited", "inc", "inc.", "corp", "corp.",
           "plc", "co", "co.", "sa", "nv", "ag", "gmbh"}


def _norm(name):
    """נרמול שם לצורך התאמה בלבד (לא לתצוגה).

    מסיר גרשיים, מפצל מקפים/פיסוק לרווחים, ומוריד סיומת תאגידית.
    שלושת אלה נדרשים כי מאיה ו-datawise כותבות את אותו שם אחרת:
    מאיה מחזירה 'דלק-קבוצה' (מקף, בלי בע"מ) ואילו datawise מחזירה
    'דלק קבוצה בע"מ'. בלי הפיצול, 'דלק-קבוצה' הוא טוקן יחיד — ולכן גם
    ההתאמה המדויקת נכשלת וגם התאמת ההכלה לא יכולה לרוץ (היא דורשת שם
    רב-מילים). התוצאה בפועל: המחזיק לא זוהה כחברה, סווג כ'יחיד', ולא
    נוצר קצה אם→בת — כלומר כרטיס בלי עץ בעלות.
    """
    s = str(name or "")
    for q in _QUOTES:
        s = s.replace(q, "")
    s = re.sub(r"[־\-–—/,.()\[\]]+", " ", s)      # מקף עברי ולועזי, סוגריים, פסיקים
    s = re.sub(r"\s+", " ", s).strip().lower()
    toks = [t for t in s.split() if t not in _SUFFIX]
    return " ".join(toks) if toks else s


def _norm_tokens(name):
    """נרמול חסין-סדר: ממיין את מילות השם, כדי שהיפוך פרטי/משפחה בין
    endpoints שונים (board = 'פרטי משפחה' לעומת interested-parties =
    'משפחה פרטי') לא ישבור התאמת שמות."""
    n = _norm(name)
    return " ".join(sorted(n.split())) if n else ""


def _norm_heb(s):
    """נרמול זהה ל-norm_name ב-scripts/company_ownership/t077_core.py —
    אותיות עבריות בלבד, בלי ניקוד, בלי 'בעמ', בלי י/ו. חייב להישאר זהה
    כדי להתאים למפתחות ב-byName של holder-hp.json (קובץ עצמאי, בלי import
    בין הסקריפטים)."""
    if not s:
        return ""
    s = re.sub(r"[\u0591-\u05C7]", "", str(s))
    s = re.sub(r"[^\u05D0-\u05EA]", "", s)
    s = s.replace("בעמ", "")
    return s.replace("י", "").replace("ו", "")


def _load_t077_edges():
    """קצות בעלות מוצהרים מ-data/holdings/t077-edges.json (נבנה ע"י
    scripts/company_ownership/t077_*.py).

    זה מקור האמת לזהות המחזיק: הן ח.פ החברה המדווחת והן ח.פ המחזיק מוצהרים
    באותו דוח ת077, ולכן אין כאן התאמת שמות בכלל. מאיה נשארת המקור לאחוזים
    (ת077 אינו מפורסר לאחוזי הצבעה), והחיבור בין השניים נעשה בתוך רשימת
    המחזיקים של אותה חברה בלבד - התאמה על פני יחידות ספורות מאותו דיווח,
    ולא ניחוש מול כל רשימת החברות בשוק.

    מחזיר {ח.פ_חברה: {"byName": {שם_מנורמל: ח.פ}, "holders": [...]}}.
    """
    path = _out("t077-edges.json")
    try:
        with open(path, encoding="utf-8") as f:
            comps = (json.load(f) or {}).get("companies") or {}
    except FileNotFoundError:
        print(f"  t077-edges.json לא נמצא ב-{path} — נופל להתאמת שמות בלבד")
        return {}
    except Exception as e:
        print(f"  אזהרה: טעינת t077-edges.json נכשלה ({e})")
        return {}
    out = {}
    for chp, rec in comps.items():
        hs = rec.get("holders") or []
        by_name = {}
        pct = {}          # מזהה מחזיק -> {vot, cap} מת077 עצמו
        for h in hs:
            k = _norm_heb(h.get("name"))
            # מזהה הצומת: ח.פ ישראלי / מספר תאגיד זר / pid (אדם).
            hid = h.get("hp") or h.get("foreignId") or h.get("pid")
            if hid and (h.get("vot") is not None or h.get("cap") is not None):
                pct[hid] = {"vot": h.get("vot"), "cap": h.get("cap")}
            if not k:
                continue
            # סוג הצומת נקבע לפי kind מת077. חברה זרה (company-foreign)
            # היא חברה לכל דבר בתצוגה — פשוט המזהה שלה foreignId ולא hp.
            kind = h.get("kind")
            if h.get("hp"):
                by_name[k] = (h["hp"], "company")
            elif h.get("foreignId") or kind == "company-foreign":
                by_name[k] = (h.get("foreignId") or hid, "company")
            elif h.get("pid"):
                by_name[k] = (h["pid"], "person")
        out[str(chp)] = {"byName": by_name, "holders": hs, "pct": pct}
    n = sum(len(v["holders"]) for v in out.values())
    n_ppl = sum(1 for v in out.values() for h in v["holders"] if h.get("pid"))
    print(f"  קצות ת077: {len(out)} חברות, {n} מחזיקים מוצהרים "
          f"({n_ppl} יחידים)")
    return out


def _load_private_holders():
    """מחזיר (byName, names): byName = שם מנורמל -> ח.פ, names = ח.פ -> שם תצוגה.
    מקור: data/holdings/holder-hp.json, שנבנה ע"י scripts/company_ownership/t077_*.py.
    סוגר את הפער שבו holder_to_hp מזהה רק מחזיקים שהם חברות נסחרות — אם הקובץ
    עוד לא רץ, מתעלמים בשקט (שני מילונים ריקים)."""
    path = _out("holder-hp.json")
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f) or {}
        by_name = data.get("byName") or {}
        holders = data.get("holders") or {}
        names = {hp: rec.get("name") for hp, rec in holders.items() if rec.get("name")}
        print(f"  מחזיקים פרטיים (ת077): {len(by_name)} שמות מ-{path}")
        return by_name, names
    except FileNotFoundError:
        print(f"  holder-hp.json (ת077) לא נמצא ב-{path} — מדלג על מחזיקים פרטיים")
        return {}, {}
    except Exception as e:
        print(f"  אזהרה: טעינת holder-hp.json נכשלה ({e})")
        return {}, {}


# ── שכבת רשת ────────────────────────────────────────────────────────
def new_session():
    s = requests.Session()
    s.headers.update(MAYA_HEADERS)
    try:
        s.get("https://maya.tase.co.il/he", timeout=30)     # warm-up ל-Incapsula
    except Exception:
        pass
    return s


def _get(s, url, retries=4):
    for a in range(retries):
        try:
            r = s.get(url, timeout=60)
            if r.status_code == 200:
                return r.json()
            if r.status_code in (204, 404):
                return None
        except requests.exceptions.RequestException:
            pass
        time.sleep(1.5 * (2 ** a))
    return None


def fetch_company_list():
    """רשימת כל החברות מ-datawise: issuerId(=companyId), corporateId(ח.פ), ענף, דואלי."""
    r = requests.get(f"{DATAWISE}/basic-securities/companies-list",
                     headers={"accept": "application/json", "accept-language": "he-IL",
                              "apikey": DATAWISE_KEY}, timeout=60)
    if r.status_code in (401, 403):
        # ה-403 הזה כמעט תמיד אומר "מפתח", לא "חסימה". מדפיסים את האבחנה
        # במקום להשאיר traceback של raise_for_status שנראה כמו בעיית רשת.
        raise SystemExit(
            f"datawise החזיר {r.status_code} ל-companies-list.\n"
            f"  אורך המפתח בשימוש: {len(DATAWISE_KEY)} תווים"
            f"{' — ריק!' if not DATAWISE_KEY else ''}\n"
            f"  מקור: DATAWISE_APIKEY={'קיים' if os.environ.get('DATAWISE_APIKEY') else 'ריק/חסר'}, "
            f"TASE_APIKEY={'קיים' if os.environ.get('TASE_APIKEY') else 'ריק/חסר'}\n"
            f"  ב-Actions: ודא שהסוד TASE_APIKEY מוגדר ושה-workflow ממפה אותו.\n"
            f"  אם המפתח תקין ועובד מקומית אך לא מהרנר — זו חסימת IP, לא מפתח.")
    r.raise_for_status()
    res = r.json()["companiesList"]["result"]
    out = []
    for c in res:
        if c.get("issuerId") is None:
            continue
        out.append({"companyId": c.get("issuerId"),
                    "companyName": c.get("companyName"),
                    "companyFullName": c.get("companyFullName"),
                    "corporateId": c.get("corporateId"),
                    "taseSector": c.get("taseSector"),
                    "isDual": c.get("isDual")})
    return out


# ── עיבוד חברה בודדת ────────────────────────────────────────────────
def build_company(cid, name, s):
    """מחזיר (holders_df, summary_row, board_rows) מ-3 קריאות JSON."""
    ip = _get(s, f"{MAYA}/interested-parties/by_company?companyId={cid}")
    dist = _get(s, f"{MAYA}/interested-parties/distribution/by_company?companyId={cid}")
    board = _get(s, f"{MAYA}/board-and-management/by_company?companyId={cid}")

    inst_names = {_norm(x.get("key")) for x in (dist or {}).get("institutional", [])}
    board_list = (board or {}).get("boardAndManagements") or []
    officer_keys = {_norm_tokens(b.get("fullName")) for b in board_list}
    ip_parties = (ip or {}).get("interestedParties", [])

    rows = []
    for p in ip_parties:
        nm = _norm(p.get("interestedPartyName"))
        cat = ("נושא משרה" if _norm_tokens(p.get("interestedPartyName")) in officer_keys
               else "מוסדי" if nm in inst_names else "בעל עניין")
        rows.append({
            "companyId": cid, "companyName": name,
            "interestedPartyId": p.get("interestedPartyId"),
            "holderName": p.get("interestedPartyName"),
            "securityId": p.get("securityId"),
            "securityName": p.get("securityName"),
            "securityCategory": p.get("securityCategory"),
            "symbol": p.get("symbol"),
            "balance": p.get("balance"),
            "capital": p.get("capital"),
            "voting": p.get("voting"),
            "marketCap": p.get("marketCap"),
            "holderCategory": cat,
            "balanceDate": p.get("balanceDate"),
            "remark": p.get("remark"),
        })
    holders_df = pd.DataFrame(rows)

    g = {x.get("key"): x.get("value") for x in (dist or {}).get("general", [])}
    ctrl = {x.get("key"): x.get("value") for x in (dist or {}).get("controllersInterest", [])}
    summary = {"companyId": cid, "companyName": name,
               "בעלי עניין": g.get("החזקות בעלי עניין"),
               "מוסדיים": g.get("החזקות מוסדיים וקרנות גידור"),
               "ציבור": g.get("החזקות ציבור"),
               "בעל שליטה": next(iter(ctrl.values()), None)}

    board_rows, ceo_rows, ceo_names = [], [], []
    for b in board_list:
        positions = [p.get("positionName") or "" for p in b.get("positions", [])]
        pos = "; ".join(positions)
        bh = b.get("holdings", []) or []
        hold = "; ".join(f'{h.get("securityName")}:{h.get("capital")}%' for h in bh)
        board_rows.append({"companyId": cid, "companyName": name,
                           "fullName": b.get("fullName"), "positions": pos,
                           "financialExpert": b.get("financialExpert"),
                           "inspectionCommittee": b.get("inspectionCommittee"),
                           "holdings": hold})
        # המנכ"ל בלבד (לא סמנכ"ל/משנה) — נרשם גם אם אין לו החזקות
        if any(_is_ceo(p) for p in positions):
            full = b.get("fullName")
            ceo_names.append(full)
            # מקור A (board): החזקות מדווחות בשורת נושא-המשרה — תואם-לאדם, ללא הצלבה
            for h in bh:
                ceo_rows.append({"companyId": cid, "companyName": name,
                                 "ceoName": full, "positions": pos, "source": "board",
                                 "securityId": h.get("securityId"),
                                 "securityName": h.get("securityName"),
                                 "balance": h.get("balance"),
                                 "capital": h.get("capital"), "voting": h.get("vote"),
                                 "balanceDate": h.get("balanceDate")})
            # מקור B (interested-parties): התאמה חסינת-סדר (היפוך פרטי/משפחה בין
            # ה-endpoints). תופס בעל-שליטה שמדווח רק כאן וגם אופציות/RSU של עובד.
            key = _norm_tokens(full)
            if key:
                for p in ip_parties:
                    if _norm_tokens(p.get("interestedPartyName")) == key:
                        ceo_rows.append({"companyId": cid, "companyName": name,
                                         "ceoName": full, "positions": pos, "source": "ip",
                                         "securityId": p.get("securityId"),
                                         "securityName": p.get("securityName"),
                                         "balance": p.get("balance"),
                                         "capital": p.get("capital"),
                                         "voting": p.get("voting"),
                                         "balanceDate": p.get("balanceDate")})
    # שורת מנכ"ל לכל חברה — גם אם לא זוהה מנכ"ל (שם ריק) וגם אם אין החזקות
    company_ceos = [(cid, name, cn) for cn in (ceo_names or [None])]
    return holders_df, summary, board_rows, ceo_rows, company_ceos


def _is_ceo(position):
    """מזהה מנכ"ל בלבד — לא סמנכ"ל, לא משנה למנכ"ל, לא סגן."""
    p = str(position)
    if any(x in p for x in ("סמנכ", "משנה", "סגן", "Deputy", "Vice", "Assistant")):
        return False
    return ("מנכ" in p or "מנהל כללי" in p
            or p.strip() == "CEO" or "Chief Executive" in p)


# ── תזמור: לולאה מקבילית + כתיבת פלטים ──────────────────────────────
def build_all(companies):
    s = new_session()
    all_h, summaries, boards, ceos, comp_ceos, failed = [], [], [], [], [], 0

    def work(c):
        return build_company(c["companyId"], c["companyName"], s)

    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futs = {ex.submit(work, c): c for c in companies}
        for k, fut in enumerate(as_completed(futs), 1):
            c = futs[fut]
            try:
                hdf, summ, brows, crows, cceos = fut.result()
                if not hdf.empty:
                    all_h.append(hdf)
                summaries.append(summ)
                boards.extend(brows)
                ceos.extend(crows)
                comp_ceos.extend(cceos)
            except Exception as e:
                failed += 1
                print(f"  {c['companyName']} ({c['companyId']}): נכשל ({e})")
            if k % 50 == 0:
                print(f"  ...{k}/{len(companies)}")

    # מיפוי סוג הנייר לפי securityId (securityCategory אינו סוג נייר אמין!)
    secmap = fetch_securities_map()

    def _t(x, i):
        return (secmap.get(int(x)) or (None, None))[i] if pd.notna(x) else None

    holdings = pd.concat(all_h, ignore_index=True) if all_h else pd.DataFrame()
    if not holdings.empty:
        pos = holdings.columns.get_loc("securityName") + 1
        holdings.insert(pos, "securityType", holdings["securityId"].map(lambda x: _t(x, 0)))
        holdings.insert(pos + 1, "securityTypeDetail",
                        holdings["securityId"].map(lambda x: _t(x, 1)))
        holdings.to_csv(_out("market_holdings.csv"), index=False, encoding="utf-8-sig")
        _write_json(holdings, summaries, _out("holdings.json"))
    pd.DataFrame(summaries).to_csv(_out("ownership_summary.csv"),
                                   index=False, encoding="utf-8-sig")
    pd.DataFrame(boards).to_csv(_out("board_and_management.csv"),
                                index=False, encoding="utf-8-sig")

    # החזקות המנכ"ל בכל חברה, לפי סוג נייר (securityFullTypeCode → securityType)
    ceo_df = pd.DataFrame(ceos)
    if not ceo_df.empty:
        ceo_df["securityType"] = ceo_df["securityId"].map(lambda x: _t(x, 0))
        ceo_df["securityTypeDetail"] = ceo_df["securityId"].map(lambda x: _t(x, 1))
        ceo_df.to_csv(_out("ceo_holdings_detail.csv"), index=False, encoding="utf-8-sig")
    ceo_summary = _ceo_summary(ceo_df, comp_ceos)
    ceo_summary.to_csv(_out("ceo_holdings.csv"), index=False, encoding="utf-8-sig")

    # טבלת מבנה שליטה מחושבת מראש — פלט לדף control.html ולחיפוש הרישום
    cid2hp = {c["companyId"]: c.get("corporateId") for c in companies}
    _control_json(holdings, ceo_summary, cid2hp, companies, _out("control.json"))

    n_ident = int(ceo_summary["ceoIdentified"].sum()) if not ceo_summary.empty else 0
    n_zero = int((ceo_summary["ceoIdentified"] & (ceo_summary["capitalPct"] == 0)
                  & (ceo_summary["votingPct"] == 0)).sum()) if not ceo_summary.empty else 0
    print(f"\n✓ {len(holdings)} שורות החזקה | {len(summaries)} חברות | "
          f"{n_ident} מנכ\"לים מזוהים ({n_zero} ללא אחזקות) | "
          f"{len(ceo_summary) - n_ident} ללא מנכ\"ל מזוהה | {failed} נכשלו")
    print("  → market_holdings.csv, holdings.json, ownership_summary.csv,")
    print("    board_and_management.csv, ceo_holdings.csv (מסוכם), ceo_holdings_detail.csv,")
    print("    control.json (טבלת מבנה שליטה)")


def _ceo_summary(ceo_df, company_ceos):
    """שורה לכל (חברה, מנכ"ל) — כולל חברות שלמנכ"ל אין החזקות (0). company_ceos =
    רשימת (companyId, companyName, ceoName) לכל חברה בבורסה.

    האחזקות מגיעות משני מקורות (board / interested-parties). כדי לא לספור פעמיים את
    אותה אחזקה המדווחת בשני הערוצים, לכל מנכ"ל בוחרים את המקור עם ה-% הגבוה
    (בערוץ בעלי-העניין מדווח בעל-שליטה את מלוא אחזקתו). זכויות לא-סחירות
    (אופציות/RSU/מענקים) מאוחדות משני המקורות."""
    agg = {}
    if not ceo_df.empty:
        df = ceo_df.copy()
        if "source" not in df.columns:
            df["source"] = "board"
        df["_cap"] = pd.to_numeric(df["capital"], errors="coerce")
        df["_vot"] = pd.to_numeric(df["voting"], errors="coerce")
        # אחזקת הון = דיווח %הון/%הצבעה חיובי. securityType אינו אמין לכך — לעתים
        # אינו ממופה גם למניות שליטה (securityId שאינו ברשימת הנסחרים היומית).
        # אופציות/RSU/מענקים מגיעים עם %=0/ריק ו-balance בלבד → זכויות לא-הוניות.
        df["_equity"] = (df["_cap"].fillna(0) > 0) | (df["_vot"].fillna(0) > 0)

        def _equity_totals(sub):
            t = (sub[sub["_equity"]].sort_values("balanceDate")
                                    .drop_duplicates("securityId", keep="last"))
            if (t["securityId"] != 0).any():
                t = t[t["securityId"] != 0]
            return round(t["_cap"].fillna(0).sum(), 2), round(t["_vot"].fillna(0).sum(), 2)

        for (cid, ceo), g in df.groupby(["companyId", "ceoName"]):
            cap_b, vot_b = _equity_totals(g[g["source"] == "board"])
            cap_i, vot_i = _equity_totals(g[g["source"] == "ip"])
            # המקור עם ה-% הגבוה מנצח — מונע כפל-ספירה של אותה אחזקה בשני ערוצים
            cap, vot = ((cap_i, vot_i) if (cap_i, vot_i) > (cap_b, vot_b)
                        else (cap_b, vot_b))
            other = g[~g["_equity"]].drop_duplicates("securityName")
            other_desc = "; ".join(
                f'{r.securityName}({int(r.balance):,})' if pd.notna(r.balance)
                else str(r.securityName) for r in other.itertuples())
            agg[(cid, ceo)] = (cap, vot, other_desc)

    rows = []
    pos_map = {}
    if not ceo_df.empty and "positions" in ceo_df.columns:
        for r in ceo_df.itertuples():
            key = (r.companyId, r.ceoName)
            if key not in pos_map and pd.notna(getattr(r, "positions", None)):
                pos_map[key] = str(r.positions)
    for cid, cname, ceo in company_ceos:
        cap, vot, other = agg.get((cid, ceo), (0.0, 0.0, ""))
        rows.append({"companyId": cid, "companyName": cname, "ceoName": ceo,
                     "ceoIdentified": ceo is not None, "position": pos_map.get((cid, ceo), ""),
                     "capitalPct": cap, "votingPct": vot, "otherRights": other})
    return (pd.DataFrame(rows)
            .sort_values("votingPct", ascending=False).reset_index(drop=True))


def _ceo_rank(position):
    """דירוג מנכ"ל ראשי — נמוך = ראשי יותר. 'מנהל כללי' הוא הראשי המובהק;
    מנכ"ל של חטיבה/חברת-בת הוא משני."""
    p = str(position or "")
    if "מנהל כללי" in p:
        return 0                                   # ראשי מובהק
    if "משותף" in p:
        return 2                                   # מנכ"ל משותף (co-CEO)
    if any(x in p for x in ("חטיבת", "חברת הבת", "חברת בת", "החברות הבנות",
                            "פעילות", "אזור", "חטיבה")):
        return 3                                   # מנכ"ל חטיבה/חברת-בת
    return 1                                       # מנכ"ל רגיל (ללא סייג)


def _control_json(holdings, ceo_summary, cid2hp, companies, path):
    """טבלת מבנה שליטה מחושבת מראש ל-control.html, מהנתונים שכבר בזיכרון.
    cid2hp: companyId → corporateId(ח.פ), לקישור לרישום ולבניית עץ אם→בת.
    companies: רשימת החברות (שם קצר + שם מלא + ח.פ) לזיהוי מחזיקים שהם חברות.
    פרטיים = בעלי עניין (לא נושאי משרה) עם הצבעה ≥ 5% — מספר והצבעה בלבד ·
    מוסדיים = גופים מוסדיים — מספר והצבעה · סך שליטה = הצבעת פרטיים + מוסדיים ·
    מנכ"ל = השורה עם ההון הגבוה ביותר."""
    if holdings.empty:
        with open(path, "w", encoding="utf-8") as f:
            json.dump([], f)
        return
    df = holdings.copy()
    for col in ("companyName", "holderName", "holderCategory"):
        if col in df.columns:
            df[col] = df[col].map(lambda s: " ".join(str(s).split()))
    df["_cap"] = pd.to_numeric(df["capital"], errors="coerce").fillna(0)
    df["_vot"] = pd.to_numeric(df["voting"], errors="coerce").fillna(0)
    # ניכוי כפילויות (חברה, מחזיק, נייר) — התאריך האחרון, ואז סכימה לכל מחזיק.
    # ההצבעה נלקחת מהשדה הגולמי כפי שמאיה מדווחת — ללא תיקון או נרמול.
    df = df.sort_values("balanceDate").drop_duplicates(
        ["companyId", "holderName", "securityId"], keep="last")
    holder = (df.groupby(["companyId", "companyName", "holderName"], sort=False)
                .agg(cap=("_cap", "sum"), vot=("_vot", "sum"),
                     cat=("holderCategory", "first")).reset_index())

    # מנכ"ל ראשי לכל חברה — לפי דירוג התואר (מנהל כללי > רגיל > משותף > חטיבה),
    # ואז תואר קצר, ואז הון גבוה. מנכ"ל מזוהה תמיד קודם ללא-מזוהה.
    ceo_map = {}
    if not ceo_summary.empty:
        cs = ceo_summary.copy()
        cs["_pos"] = cs["position"] if "position" in cs.columns else ""
        cs["_rank"] = cs["_pos"].map(_ceo_rank)
        cs["_plen"] = cs["_pos"].map(lambda p: len(str(p)))
        cs["_ident"] = cs["ceoIdentified"].map(lambda x: 0 if bool(x) else 1)
        cs = cs.sort_values(["_ident", "_rank", "_plen", "capitalPct"],
                            ascending=[True, True, True, False])
        for r in cs.itertuples():
            k = str(r.companyId)
            if k not in ceo_map:
                ceo_map[k] = {
                    "name": " ".join(str(r.ceoName).split()) if pd.notna(r.ceoName) else "",
                    "cap": round(float(r.capitalPct), 2),
                    "vot": round(float(r.votingPct), 2),
                    "other": " ".join(str(r.otherRights).split()) if pd.notna(r.otherRights) and str(r.otherRights) != "nan" else "",
                    "identified": bool(r.ceoIdentified)}

    # ── קצוות עץ אם→בת: מחזיק שהוא עצמו חברה מהרשימה = קצה שליטה ──────────────
    # אינדקס שמות: שם קצר + שם משפטי מלא (חסין-סדר) → ח.פ, ושם תצוגה קנוני לכל ח.פ.
    name2hp, hp2name = {}, {}
    for c in companies:
        hp = c.get("corporateId")
        if not hp:
            continue
        hp2name.setdefault(hp, " ".join(str(c.get("companyName") or "").split()))
        for nm in (c.get("companyName"), c.get("companyFullName")):
            k = _norm_tokens(nm)
            if k:
                name2hp.setdefault(k, hp)

    private_by_name, private_names = _load_private_holders()
    for hp, nm in private_names.items():
        hp2name.setdefault(hp, nm)   # שם ציבורי (אם יש) גובר על שם מדוח ת077

    t077 = _load_t077_edges()
    for rec in t077.values():
        for h in rec["holders"]:
            if h.get("hp") and h.get("name"):
                hp2name.setdefault(h["hp"], " ".join(str(h["name"]).split()))

    node_kind = {}          # מזהה -> "company" | "person"
    for rec in t077.values():
        for h in rec["holders"]:
            # חברה זרה: מזהה foreignId, סוג company. אחרת hp/pid כרגיל.
            nid = h.get("hp") or h.get("foreignId") or h.get("pid")
            if nid and h.get("name"):
                hp2name.setdefault(nid, " ".join(str(h["name"]).split()))
                if h.get("hp") or h.get("foreignId") or h.get("kind") == "company-foreign":
                    node_kind.setdefault(nid, "company")
                else:
                    node_kind.setdefault(nid, "person")

    resolved_by = {"ת077 של החברה": 0, "התאמה מדויקת": 0,
                   "מפת ת077 גלובלית": 0, "לא זוהה": 0}

    def holder_to_node(holder_name, child_hp=None):
        """מחזיר (מזהה, סוג) או (None, None).

        סוג "company" -> המזהה הוא ח.פ אמיתי, ניתן לחיפוש ברשם.
        סוג "person"  -> המזהה הוא pid אטום. הת.ז. אינה כאן ואינה בשום
                         מקום תחת data/ - היא נשארת ב-private/ בלבד.

        סדר עדיפות, מהוודאי לחלש:
        1. ת077 של אותה חברה — הזיהוי מוצהר בדוח. השם משמש רק כדי לחבר את
           שורת מאיה (אחוז, בלי מזהה) למחזיק בדוח (מזהה, בלי אחוז), בתוך
           רשימה של יחידות בודדות מאותו דיווח.
        2. התאמה מדויקת מול רשימת החברות הנסחרות — תאגידים בלבד, לחברות
           שאין להן דוח ת077.
        3. מפת השמות הגלובלית של ת077 — תאגידים בלבד.

        שים לב שיחידים מזוהים *רק* דרך שלב 1. אין להם מפה גלובלית לפי שם
        בכוונה: שמות של אנשים חוזרים על עצמם, ומיזוג שני אנשים שונים בעלי
        אותו שם הוא בדיוק סוג השגיאה שהעץ הזה לא יכול להרשות לעצמו.
        """
        rec = t077.get(str(child_hp)) if child_hp else None
        if rec:
            hit = rec["byName"].get(_norm_heb(holder_name))
            if hit:
                resolved_by["ת077 של החברה"] += 1
                return hit
        k = _norm_tokens(holder_name)
        if k:
            hp = name2hp.get(k)
            if hp:
                resolved_by["התאמה מדויקת"] += 1
                return hp, "company"
        hp = private_by_name.get(_norm_heb(holder_name))
        resolved_by["מפת ת077 גלובלית" if hp else "לא זוהה"] += 1
        return (hp, "company") if hp else (None, None)

    children, parents = {}, {}   # מזהה → {מזהה_קשור: vot}
    for r in holder.itertuples(index=False):
        child_hp = cid2hp.get(r.companyId)
        if not child_hp:
            continue
        parent_id, kind = holder_to_node(r.holderName, child_hp)
        if not parent_id or parent_id == child_hp:
            continue
        node_kind.setdefault(parent_id, kind)
        hp2name.setdefault(parent_id, " ".join(str(r.holderName).split()))
        vot = round(float(r.vot), 2)
        children.setdefault(parent_id, {})[child_hp] = max(children.get(parent_id, {}).get(child_hp, 0), vot)
        parents.setdefault(child_hp, {})[parent_id] = max(parents.get(child_hp, {}).get(parent_id, 0), vot)

    # קצה שהוצהר בת077: עכשיו ת077 עצמו נושא את האחוז. אם מאיה כבר קבעה
    # אחוז לאותו קצה (התאמת שם הצליחה) — לוקחים את הגבוה מבין השניים, כמו
    # שמאיה כבר עושה בין חשבונות שונים (max), כדי לא להמעיט בשליטה. אם מאיה
    # פספסה (השם לא הותאם) — ת077 ממלא. זה מה שסוגר את ~86% ה-vot=null.
    # None נשאר רק אם לשניהם אין אחוז. ההעדפה הזו זמנית עד סקריפט ההשוואה.
    n_only = n_t077pct = n_merged = 0
    for chp, rec in t077.items():
        pctmap = rec.get("pct") or {}
        for h in rec["holders"]:
            # מזהה הצומת: ח.פ / מספר תאגיד זר / pid. חברה זרה נכנסת גם היא.
            nid = h.get("hp") or h.get("foreignId") or h.get("pid")
            if not nid or nid == chp:
                continue
            t_vot = (pctmap.get(nid) or {}).get("vot")
            existing = parents.get(chp, {}).get(nid, "MISSING")
            if existing != "MISSING":
                # מאיה כבר קבעה — משלבים את הגבוה אם לת077 יש אחוז
                if t_vot is not None:
                    merged = t_vot if existing is None else max(existing, t_vot)
                    if merged != existing:
                        parents[chp][nid] = merged
                        children.setdefault(nid, {})[chp] = merged
                        n_merged += 1
                continue
            # קצה חדש שמאיה פספסה לגמרי
            if t_vot is not None:
                n_t077pct += 1
            else:
                n_only += 1
            parents.setdefault(chp, {})[nid] = t_vot
            children.setdefault(nid, {})[chp] = t_vot
    if n_t077pct:
        print(f"  {n_t077pct} קצות עם אחוז מת077 (זהות ואחוז מאותו דוח)")
    if n_merged:
        print(f"  {n_merged} קצות שאחוז ת077 עדכן/השלים את מאיה")
    if n_only:
        print(f"  {n_only} קצות מת077 בלבד (זהות ודאית, ללא אחוז)")

    # ── מאיזה שלב הגיע כל זיהוי ────────────────────────────────────────────
    # זו המדידה שמחליטה אם עוד צריך את שני שלבי הגיבוי מבוססי-השם. אם
    # "התאמה מדויקת" ו"מפת ת077 גלובלית" קרובים לאפס — אפשר למחוק גם אותם
    # ולהישאר עם ת077 בלבד. אם הם נושאים משקל אמיתי, הם מכסים את החברות
    # שאין להן דוח ת077 בטווח שנסרק.
    tot = sum(resolved_by.values()) or 1
    print("\n  זיהוי מחזיקים לפי מקור:")
    for k, v in resolved_by.items():
        print(f"    {k:22} {v:6,}  ({v/tot*100:5.1f}%)")

    # מזהי צמתים זרים (חברה זרה) — foreignId שאינו ח.פ ישראלי. מזוהים כאן
    # כדי להבחין אותם גם ב-hp (רשם ישראלי) וגם ב-pid (אדם).
    foreign_ids = set()
    for rec in t077.values():
        for h in rec["holders"]:
            if h.get("foreignId"):
                foreign_ids.add(h["foreignId"])

    def _edges(d, hp):
        # מפתח נפרד לפי סוג הצומת:
        #   hp        - תאגיד ישראלי, ניתן לחיפוש ברשם
        #   foreignId - תאגיד זר, חברה בתצוגה אך אין לו רשומה ברשם הישראלי
        #   pid       - אדם, מזהה אטום, אין רשומה בשום מרשם
        # ה-frontend מסתמך על ההבחנה כדי לא לנסות לפתוח כרטיס רשם למי שאין לו.
        out = []
        for k, v in sorted(d.get(hp, {}).items(),
                           key=lambda kv: (kv[1] is not None, kv[1] or 0),
                           reverse=True):
            e = {"name": hp2name.get(k, ""), "vot": v}
            if k in foreign_ids:
                e["foreignId"] = k          # חברה זרה
            elif node_kind.get(k) == "person":
                e["pid"] = k
            else:
                e["hp"] = k
            out.append(e)
        return out

    # פילוח הבעלות לפאי. מקור ראשי: קצוות ת077 (עקבי עם הכרטיסיות בעץ).
    # אבל לחברות דואליות/זרות אין דוח ת077 (הן פטורות), ולכן כשת077 ריק
    # נופלים למאיה — המקור היחיד שיש לחברות האלה. הפילוח מבחין בין
    # מוסדיים, בעלי עניין שהם חברות, ובעלי עניין שהם יחידים.
    _INST_HT = ("מוסדי", "קרן", "קופת גמל", "קופות גמל", "גמל", "פנסי",
                "נאמנות", "ביטוח", "השתלמות", "תעודות סל", "סל")

    def _is_inst_ht(ht):
        s = str(ht or "")
        return any(k in s for k in _INST_HT)

    def _split_from_t077(hp):
        """פילוח מקצוות ת077. מחזיר None אם אין דוח ת077 לחברה."""
        rec = t077.get(str(hp))
        if not rec or not rec.get("holders"):
            return None
        d = {"capInst": 0.0, "capCorp": 0.0, "capIndiv": 0.0,
             "votInst": 0.0, "votCorp": 0.0, "votIndiv": 0.0}
        for h in rec["holders"]:
            v, c = h.get("vot"), h.get("cap")
            if _is_inst_ht(h.get("holderType")):
                bkt = "Inst"
            elif h.get("kind") in ("company", "company-foreign", "partnership"):
                bkt = "Corp"
            else:
                bkt = "Indiv"        # אדם פרטי
            if v is not None: d["vot" + bkt] += v
            if c is not None: d["cap" + bkt] += c
        return {k: round(val, 2) for k, val in d.items()}

    def _split_from_maya(g):
        """נפילה לאחור: פילוח מנתוני מאיה, לחברות בלי ת077 (דואליות/זרות).
        cat של מאיה: 'מוסדי' / 'בעל עניין' / 'יחיד'. חברה מול יחיד בבעלי
        העניין נקבע לפי node_kind של המחזיק (אם זוהה כתאגיד)."""
        d = {"capInst": 0.0, "capCorp": 0.0, "capIndiv": 0.0,
             "votInst": 0.0, "votCorp": 0.0, "votIndiv": 0.0}
        for r in g.itertuples(index=False):
            v = float(getattr(r, "vot", 0) or 0)
            c = float(getattr(r, "cap", 0) or 0)
            cat = getattr(r, "cat", "")
            if cat == "מוסדי":
                bkt = "Inst"
            else:
                # בעל עניין/יחיד — חברה אם זוהתה כתאגיד, אחרת יחיד
                nid, knd = holder_to_node(getattr(r, "holderName", ""), None)
                bkt = "Corp" if knd == "company" else "Indiv"
            d["vot" + bkt] += v
            d["cap" + bkt] += c
        return {k: round(val, 2) for k, val in d.items()}

    SIGNIF = 5.0  # סף הצבעה ל"בעל עניין משמעותי" — לא כולל נושאי משרה
    out = []
    for (cid, cname), g in holder.groupby(["companyId", "companyName"], sort=False):
        # פרטיים משמעותיים: בעלי עניין (לא נושאי משרה) עם הצבעה ≥ 5%
        priv = g[(g["cat"] == "בעל עניין") & (g["vot"] >= SIGNIF)]
        inst = g[g["cat"] == "מוסדי"]
        top = g.sort_values(["vot", "cap"], ascending=False).head(1)
        t = top.iloc[0] if len(top) else None
        priv_vot = round(float(priv["vot"].sum()), 2)
        inst_vot = round(float(inst["vot"].sum()), 2)
        # רשימת מוסדיים לפאי הטאב "מוסדיים" — שם + הצבעה, יורד. עקבי עם
        # instVot (סכום אותם מחזיקים). מקור: אותן שורות מוסדיות שכבר בזיכרון.
        inst_list = [{"name": " ".join(str(r.holderName).split()),
                      "pct": round(float(r.vot), 2)}
                     for r in inst.sort_values("vot", ascending=False)
                                  .itertuples(index=False)
                     if float(r.vot) > 0]
        hp = cid2hp.get(cid)
        # topCat היה בינארי (מוסדי/יחיד), ולכן כל מחזיק שאינו מוסדי הוצג
        # כ"יחיד" — גם כשהוא חברה מזוהה עם ח.פ (למשל דלק-קבוצה בכרטיס
        # ישראכרט). כאן משתמשים באותה הכרעה שבונה את קצות העץ.
        top_cat = ""
        if t is not None:
            if t["cat"] == "מוסדי":
                top_cat = "מוסדי"
            else:
                _, _kind = holder_to_node(t["holderName"], hp)
                top_cat = "חברה" if _kind == "company" else "יחיד"
        sp = _split_from_t077(hp)
        split_src = "t077"
        if sp is None:
            sp = _split_from_maya(g)      # דואלית/זרה — אין ת077, נופלים למאיה
            split_src = "maya"
        out.append({
            "company": " ".join(str(cname).split()),
            "hp": hp,
            "parents": _edges(parents, hp),
            "children": _edges(children, hp),
            "topName": (" ".join(str(t["holderName"]).split()) if t is not None else ""),
            "topCat": top_cat,
            "topCap": round(float(t["cap"]), 2) if t is not None else 0,
            "topVot": round(float(t["vot"]), 2) if t is not None else 0,
            "privN": int(len(priv)), "privVot": priv_vot,
            "instN": int(len(inst)), "instVot": inst_vot,
            "inst": inst_list,
            "combined": round(priv_vot + inst_vot, 2),
            # פילוח לפאי: מוסדי / בעל עניין תאגידי / בעל עניין יחיד, הון+הצבעה.
            # מ-ת077 אם קיים, אחרת ממאיה (דואליות). splitSource מציין מהיכן.
            "capInst": sp["capInst"], "capCorp": sp["capCorp"], "capIndiv": sp["capIndiv"],
            "votInst": sp["votInst"], "votCorp": sp["votCorp"], "votIndiv": sp["votIndiv"],
            "splitSource": split_src,
            "ceo": ceo_map.get(str(cid)),
        })
    out.sort(key=lambda d: d["combined"], reverse=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, separators=(",", ":"))


def _write_json(holdings, summaries, path):
    sm = {s["companyId"]: s for s in summaries}
    recs = []
    for cid, grp in holdings.groupby("companyId"):
        hs = grp.drop(columns=["companyId", "companyName"]).astype(object)
        hs = hs.where(pd.notna(hs), None)
        recs.append({"companyId": int(cid),
                     "companyName": grp["companyName"].iloc[0],
                     "summary": {k: v for k, v in sm.get(cid, {}).items()
                                 if k not in ("companyId", "companyName")},
                     "holders": hs.to_dict("records")})
    with open(path, "w", encoding="utf-8") as f:
        json.dump(recs, f, ensure_ascii=False, separators=(",", ":"))

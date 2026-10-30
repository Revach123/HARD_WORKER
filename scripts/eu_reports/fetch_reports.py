#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
scripts/eu_reports/fetch_reports.py   (הועבר מ-Revach scripts/ownership_eu - ריצה ארוכה, בריפו ציבורי)

המקבילה האירופאית ל-scripts/ownership_us/proxy_parse.py. באירופה אין 13F/Form 4/13D-G מרכזיים, ולכן המקור
העיקרי הוא הדוח השנתי (ESEF / UKSEF) של כל חברה רשומה - שם יש פרק "בעלי מניות מהותיים" (בבריטניה: הודעות
לפי DTR 5 - 3% ומעלה), אחזקות הדירקטורים, וכמות המניות המונפקות.

  1. יקום: מאגר ניי"ע העולמי (Revach123/INDICES data/world_securities/securities.jsonl) - מניות פעילות,
           מנפיק לפי issuer_country (COUNTRIES, ברירת מחדל GB), ייחודי לפי LEI.
  2. filings.xbrl.org: הדוח השנתי האחרון לכל LEI (api/entities/{lei}/filings) והורדת ה-XHTML.
  3. חיתוך: ממירים לטקסט (תאי טבלה מופרדים " | ") ומוצאים חלונות סביב כותרות הבעלות. הטקסט נשמר
           לשלב ה-LLM ב-HARD_WORKER (scripts/eu_reports) - כאן אין חילוץ מספרים.

פלט (data/eu_reports/; Revach מושך את reports.json לבניית הדף):
  reports.json        - לכל LEI: שם, ISIN, מדינה, תקופת הדוח, קישור, סטטוס, גודל הסעיף
  sections.json.gz    - lei -> טקסט סעיפי הבעלות
  scripts/eu_reports/queue.json.gz - התור של extract_eu_reports.py, בפורמט התור האמריקאי {lei: {n, u, t, r, k}}

filings.xbrl.org מחזיר 429 אחרי כ-10 בקשות רצופות - קצב איטי (XBRL_GAP שניות) וניסיון חוזר.
env: WORLD_DATA_DIR, COUNTRIES (GB,IE,...), LEIS (רשימה, עוקף), PRIORITY_LEIS, DEBUG_LEIS, LIMIT, BUDGET_MIN, XBRL_GAP, REFRESH=1
"""

import gzip
import io
import zipfile
import json
import os
import re
import sys
import threading
import time
import unicodedata
import urllib.error
import urllib.request
from datetime import date

from universe_rules import lei_name_mismatch  # noqa: E402 - משותף עם six_holders/build_six.py
import lxml.html
from lxml import etree

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
WORLD_DIR = os.environ.get("WORLD_DATA_DIR", "_indices/data/world_securities")
OUT_DIR = os.environ.get("OUT_DIR") or os.path.join(ROOT, "data", "eu_reports")
QUEUE_PATH = os.path.join(ROOT, "scripts", "eu_reports", "queue.json.gz")  # התור של extract_eu_reports.py
BASE = "https://filings.xbrl.org"
UA = "Revach ownership research (azzlevy@gmail.com)"
# מדינות המנפיק. "EUROPE" = האיחוד + EEA + בריטניה ואיי התעלה (ESEF/UKSEF ב-filings.xbrl.org; שווייץ אינה ב-ESEF)
EUROPE = {"AT", "BE", "BG", "HR", "CY", "CZ", "DK", "EE", "FI", "FR", "DE", "GR", "HU", "IE", "IT", "LV", "LT", "LU",
          "MT", "NL", "PL", "PT", "RO", "SK", "SI", "ES", "SE", "NO", "IS", "LI", "GB", "JE", "GG", "IM"}
COUNTRIES = set()
for _c in os.environ.get("COUNTRIES", "GB").split(","):
    _c = _c.strip().upper()
    COUNTRIES |= EUROPE if _c == "EUROPE" else ({_c} if _c else set())
LEIS = [x.strip() for x in os.environ.get("LEIS", "").split(",") if x.strip()]
PRIORITY = [x.strip() for x in os.environ.get("PRIORITY_LEIS", "").split(",") if x.strip()]  # בראש התור
LIMIT = int(os.environ.get("LIMIT") or 0)
BUDGET_MIN = float(os.environ.get("BUDGET_MIN") or 150)
GAP = float(os.environ.get("XBRL_GAP") or 1.5)
REFRESH = os.environ.get("REFRESH") == "1"
DEBUG_LEIS = {x.strip() for x in os.environ.get("DEBUG_LEIS", "").split(",") if x.strip()}  # שמירת כל הטקסט (כיול)
MAX_REPORT_BYTES = 400 * 1024 * 1024
PARSER_VERSION = 4  # 3: עוגני שליטה; 4: עוגנים בשפות האיחוד + העדפת דוח באנגלית

SECTION_MAX = 45000      # תווים לחברה (כל החלונות יחד)
# (שורות לפני, שורות אחרי, תווים) לפי משקל העוגן. בדוחות שהומרו מ-PDF הטבלה יכולה להופיע ~150 שורות
# אחרי הכותרת ("set out in the table above" כשהטבלה בטור הבא) - ולכן חלון רחב לפרק בעלי המניות.
WINDOWS = {3: (40, 170, 14000), 2: (5, 90, 8000), 1: (2, 12, 1500)}

# עוגנים: פרק בעלי המניות המהותיים (משקל 3), אחזקות דירקטורים (2), הון מונפק (1)
PATTERNS = [
    (3, re.compile(r"substantial\s+(share\s*)?(holdings?|interests)|major\s+(share)?holders?|"
                   r"major\s+shareholdings?|significant\s+(share)?holders?|significant\s+shareholdings?|"
                   r"interests\s+in\s+voting\s+rights|notifiable\s+interests|major\s+interests\s+in|"
                   r"\bDTR\s*5\b|rule\s+5\s+of\s+the\s+(DTR|disclosure)|"
                   r"notified\s+(to\s+)?(the\s+(company|group)\s+)?(of|under|in\s+accordance)|"
                   # שליטה: בעל שליטה + הסכם יחסים (Listing Rules), גילוי לפי סעיף 10 לדירקטיבת ההשתלטות
                   r"controlling\s+shareholders?|relationship\s+agreement|"
                   r"(article\s+10\s+of\s+the\s+(eu\s+)?takeover|takeover\s+directive|structures?\s+relevant\s+to\s+takeover|"
                   r"information\s+relevant\s+to\s+takeover|disclosures?\s+(required\s+)?(under|pursuant\s+to)\s+.{0,40}takeover)|"
                   r"special\s+(control\s+)?rights|right\s+to\s+appoint\s+(a\s+)?(majority|directors)|"
                   r"shareholder\s+structure|ownership\s+structure|largest\s+shareholders|principal\s+shareholders|"
                   # דוחות בשפה המקומית (ESEF באיחוד מוגש לעתים רק בשפת המדינה)
                   r"aktionärsstruktur|stimmrechtsmitteilung|bedeutende\s+beteiligung|beteiligungen\s+am\s+kapital|"
                   r"übernahmerechtliche\s+angaben|beherrschende[rn]?\s+(aktionär|gesellschafter)|"          # DE/AT
                   r"répartition\s+du\s+capital|actionnariat|franchissements?\s+de\s+seuils?|actionnaires?\s+principaux|"
                   r"contrôle\s+de\s+la\s+société|droits\s+de\s+vote\s+double|"                                 # FR/BE/LU
                   r"assetti\s+proprietari|partecipazioni\s+rilevanti|azionisti\s+rilevanti|azionariato|"
                   r"controllo\s+(di\s+diritto|di\s+fatto)|"                                                         # IT
                   r"participaciones\s+significativas|estructura\s+accionarial|accionistas\s+significativos|"     # ES
                   r"estrutura\s+acionista|participações\s+qualificadas|"                                          # PT
                   r"substantiële\s+deelnemingen|aandeelhoudersstructuur|grootaandeelhouders|"                       # NL
                   r"största\s+aktieägar|ägarstruktur|storaktionær|største\s+aksjonær|aksjonærstruktur|"            # SE/DK/NO
                   r"suurimmat\s+osakkeenomistajat|omistusrakenne|"                                                  # FI
                   r"znaczne\s+pakiety\s+akcji|struktura\s+akcjonariatu|akcjonariusze\s+posiadający|"            # PL
                   r"μέτοχοι|μετοχική\s+σύνθεση|akcionářsk|részvényesi\s+struktúr|acționari\s+semnificativi", re.I)),
    (2, re.compile(r"directors['’]?\s+(share\s*)?interests|directors['’]?\s+(beneficial\s+)?shareholdings?|"
                   r"interests\s+of\s+(the\s+)?directors|directors['’]?\s+interests\s+in\s+shares", re.I)),
    (1, re.compile(r"issued\s+share\s+capital|total\s+(number\s+of\s+)?voting\s+rights|"
                   r"(ordinary\s+)?shares\s+in\s+issue|grundkapital|capital\s+social|capitale\s+sociale|"
                   r"aktiekapital|osakepääoma|kapitał\s+zakładowy|geplaatst\s+kapitaal", re.I)),
]
# אזכור חולף של "בעלי מניות גדולים" בפרק הממשל (פגישות, דיאלוג) - לא עוגן
NOISE_RE = re.compile(r"engag|\bmeet|\bmet\b|dialogue|consult|views|feedback|value\s+creat|shareholder\s+value|"
                      r"remuneration|incentive|bonus|salary|guideline|requirement", re.I)
# ביטויים שמעידים על רשימת הודעות החזקה (מחזקים את ניקוד החלון)
DTR_RE = re.compile(r"\bDTR|transparency\s+rules|notif|voting\s+rights|3%\s+or\s+more|% of (total )?(voting|issued)", re.I)
TOC_RE = re.compile(r"^.{3,80}\s\d{1,3}$")
PCT_RE = re.compile(r"\d\s*%|\bper\s+cent\b", re.I)
BLOCK_TAGS = {"div", "p", "br", "tr", "li", "h1", "h2", "h3", "h4", "h5", "h6", "table", "section", "article"}
PUA_RE = re.compile(r"[\ue000-\uf8ff]")
WS_RE = re.compile(r"[ \t ​  ]+")


def log(*a):
    print(*a, flush=True)


class Gate:
    """בקשה אחת כל GAP שניות לשרת (כל ה-threads יחד)."""

    def __init__(self, gap):
        self.gap, self.next_at, self.lock = gap, 0.0, threading.Lock()

    def wait(self):
        with self.lock:
            now = time.monotonic()
            if now < self.next_at:
                time.sleep(self.next_at - now)
                now = self.next_at
            self.next_at = now + self.gap


GATE = Gate(GAP)


def get(url, retries=6, timeout=180, limit=None):
    last = None
    for attempt in range(retries):
        GATE.wait()
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read(limit + 1 if limit else -1)
        except urllib.error.HTTPError as e:
            last = e
            if e.code == 404:
                return None
            if e.code in (403, 429) or e.code >= 500:
                time.sleep(20 * (attempt + 1))
                continue
            raise
        except Exception as e:  # noqa: BLE001 - רשת: כשל זמני => ניסיון חוזר
            last = e
            time.sleep(5 * (attempt + 1))
    raise RuntimeError(f"GET failed: {url}: {last!r}")


# ---------------------------------------------------------------- universe

def load_universe():
    """lei -> {n, isin, cc, venues} למניות פעילות של מנפיקים מהמדינות המבוקשות."""
    out = {}
    path = os.path.join(WORLD_DIR, "securities.jsonl")
    with open(path, encoding="utf-8") as f:
        for line in f:
            r = json.loads(line)
            lei = r.get("lei")
            if not lei or r.get("category") != "stocks" or not r.get("last_seen_active"):
                continue
            if r.get("security_type") not in ("Common Stock", "Preferred Stock", "Other Equity"):
                continue
            # LEI שגוי מהרגולטור: שם המנפיק לפי ה-LEI ושם המנפיק לפי הבורסה בלי מילה משותפת
            # (CH0002277314 היא Schlatter Industries AG, וה-LEI שלה הוא של Goldman Sachs International Bank)
            if lei_name_mismatch(r):
                continue
            cc = (r.get("issuer_country") or "").upper()
            if LEIS:
                if lei not in LEIS:
                    continue
            elif cc not in COUNTRIES:
                continue
            cur = out.get(lei)
            v = r.get("n_venues") or 0
            # נייר מייצג: מניה רגילה עם הכי הרבה זירות מסחר
            better = (cur is None or (r.get("security_type") == "Common Stock") > (cur["type"] == "Common Stock")
                      or (r.get("security_type") == cur["type"] and v > cur["venues"]))
            if better:
                out[lei] = {"n": r.get("issuer_name") or r.get("name"), "isin": r.get("isin"), "cc": cc,
                            "venues": v, "type": r.get("security_type"),
                            "venues_total": (cur or {}).get("venues_total", 0)}
            out[lei]["venues_total"] = out[lei].get("venues_total", 0) + v
    return out


# ---------------------------------------------------------------- report -> text

def _local(tag):
    return tag.split("}")[-1].split(":")[-1].lower() if isinstance(tag, str) else ""


def _parse(raw):
    """XHTML (ESEF) -> עץ. קודם XML עם huge_tree (דוחות של 50-130MB נקטעו ב-HTML parser לשורה אחת),
    ואם יצא כמעט ריק - HTML parser (גם הוא עם huge_tree, ופענוח UTF-8 מפורש)."""
    try:
        root = etree.fromstring(raw, etree.XMLParser(huge_tree=True, recover=True, resolve_entities=False))
        if root is not None and len("".join(root.itertext())) > 5000:
            return root
    except (etree.XMLSyntaxError, ValueError):
        pass
    return lxml.html.fromstring(raw, parser=lxml.html.HTMLParser(huge_tree=True, encoding="utf-8"))


def report_lines(raw):
    """XHTML (ESEF) -> שורות טקסט. תאי טבלה מופרדים " | "."""
    raw = re.sub(rb"data:[a-z]+/[a-z0-9.+-]+;base64,[A-Za-z0-9+/=\s]+", b"", raw)  # תמונות מוטמעות
    root = _parse(raw)
    # script/style ו-ix:header (עובדות XBRL מוסתרות - לא חלק מהטקסט הנראה)
    bad = [el for el in root.iter() if _local(el.tag) in ("script", "style", "head")
           or (_local(el.tag) == "header" and ("inlineXBRL" in el.tag or el.tag.lower().startswith("ix:")))]
    for el in bad:
        if el.getparent() is not None:
            el.getparent().remove(el)
    for el in root.iter():
        tag = _local(el.tag)
        if tag in ("td", "th"):
            el.tail = " | " + (el.tail or "")
        elif tag in BLOCK_TAGS:
            el.tail = "\n" + (el.tail or "")
            if tag != "br":
                el.text = "\n" + (el.text or "")
    text = "".join(t for t in root.itertext() if isinstance(t, str))
    # תווי Private Use (גופני PDF: "the\ue004number", "Sharpe\ue01d 100") => רווח; ליגטורות ("notiﬁed") => NFKC.
    # חשוב לבדיקת ה-grounding ב-HARD_WORKER: הציטוט של Gemini מושווה מילה-במילה לטקסט הזה.
    text = unicodedata.normalize("NFKC", PUA_RE.sub(" ", text))
    lines = []
    for ln in text.split("\n"):
        ln = WS_RE.sub(" ", ln).strip(" |")
        if ln:
            lines.append(ln)
    return lines


SHORT_REPORT = 3000   # שורות - פחות מזה בדוח שנתי = כנראה רק אחד מכמה מסמכים בחבילה


def package_lines(package_url, report_url):
    """כל מסמכי ה-XHTML בחבילת ה-ESEF (zip), חוץ מזה שכבר נקרא ומה-viewer -> [lines, ...]."""
    try:
        raw = get(package_url, limit=MAX_REPORT_BYTES)
        if not raw or len(raw) > MAX_REPORT_BYTES:
            return []
        z = zipfile.ZipFile(io.BytesIO(raw))
    except Exception as e:  # noqa: BLE001
        log(f"    package failed {package_url}: {e!r}")
        return []
    main = report_url.rsplit("/", 1)[-1]
    out = []
    for name in sorted(z.namelist()):
        base = name.rsplit("/", 1)[-1]
        if not re.search(r"\.x?html?$", base, re.I) or base == main or "viewer" in base.lower() or "/reports/" not in "/" + name:
            continue
        try:
            out.append(report_lines(z.read(name)))
        except Exception as e:  # noqa: BLE001
            log(f"    package doc failed {name}: {e!r}")
    return out


def find_sections(lines):
    """חלונות טקסט סביב פרקי הבעלות -> (טקסט, רשימת עוגנים שנבחרו)."""
    cands = []
    for i, ln in enumerate(lines):
        if len(ln) > 250:
            continue  # פסקה ארוכה - לא כותרת/משפט פתיחה
        for w, rx in PATTERNS:
            if not rx.search(ln):
                continue
            if w >= 2 and NOISE_RE.search(ln):
                break
            before, after, cap = WINDOWS[w]
            lo, hi = max(0, i - before), min(len(lines), i + after)
            win = "\n".join(lines[lo:hi])[:cap]
            pct = len(PCT_RE.findall(win))
            dtr = len(DTR_RE.findall(win))
            # בלי אחוזים בחלון / שורת תוכן עניינים ("Substantial shareholdings 112") => ניקוד נמוך
            score = (w * 10 + min(pct, 20) + (3 * min(dtr, 5) if w == 3 else 0)
                     - (15 if pct == 0 else 0) - (15 if TOC_RE.search(ln) else 0))
            cands.append((score, lo, hi, w, ln[:120]))
            break
    if not cands:
        return "", []
    cands.sort(key=lambda c: (-c[3], -c[0]))  # קודם פרק בעלי המניות, אחר כך דירקטורים, אחר כך הון
    chosen, used = [], 0
    for score, lo, hi, w, head in cands:
        if score < 10:
            continue
        hit = next((k for k, c in enumerate(chosen) if lo < c[2] and hi > c[1]), None)
        if hit is not None:  # חפיפה - מרחיבים חלון קיים
            s2, l2, h2, w2, hd2 = chosen[hit]
            add = len("\n".join(lines[min(lo, l2):max(hi, h2)])) - len("\n".join(lines[l2:h2]))
            if used + add <= SECTION_MAX:
                chosen[hit] = (max(s2, score), min(lo, l2), max(hi, h2), max(w, w2), hd2)
                used += add
            continue
        size = min(len("\n".join(lines[lo:hi])), WINDOWS[w][2])
        if used + size > SECTION_MAX:
            continue
        chosen.append((score, lo, hi, w, head))
        used += size
    chosen.sort(key=lambda c: c[1])
    parts = ["\n".join(lines[lo:hi]) for _, lo, hi, _, _ in chosen]
    text = "\n\n[...]\n\n".join(parts)[:SECTION_MAX + 5000]
    return text, [{"w": w, "head": head, "score": s} for s, _, _, w, head in chosen]


# ---------------------------------------------------------------- per company

def latest_filing(lei):
    b = get(f"{BASE}/api/entities/{lei}/filings")
    if not b:
        return None
    rows = (json.loads(b).get("data") or [])
    rows = [r for r in rows if (r.get("attributes") or {}).get("report_url")]
    if not rows:
        return None
    # השנה האחרונה; אם יש כמה גרסאות שפה לאותה שנה (ASML: "...-1-en") - עדיפות לאנגלית
    en = re.compile(r"[-_]en([-_./]|$)", re.I)
    rows.sort(key=lambda r: (r["attributes"].get("period_end") or "",
                             bool(en.search(r["attributes"].get("report_url") or "")),
                             r["attributes"].get("date_added") or ""))
    r = rows[-1]
    a = r["attributes"]
    return {"id": r.get("id"), "period_end": a.get("period_end"), "date_added": (a.get("date_added") or "")[:10],
            "report_url": BASE + a["report_url"], "viewer_url": BASE + a["viewer_url"] if a.get("viewer_url") else None,
            "json_url": BASE + a["json_url"] if a.get("json_url") else None,
            "package_url": BASE + a["package_url"] if a.get("package_url") else None, "n_filings": len(rows)}


def process(lei, meta, old):
    rec = {"n": meta["n"], "isin": meta["isin"], "cc": meta["cc"], "pv": PARSER_VERSION,
           "checked": date.today().isoformat()}
    fil = latest_filing(lei)
    if not fil:
        rec["status"] = "no_filing"
        return rec, None
    rec["filing"] = fil
    if (not REFRESH and old and old.get("pv") == PARSER_VERSION and (old.get("filing") or {}).get("id") == fil["id"]
            and old.get("status") in ("ok", "no_section")):
        return {**old, "checked": rec["checked"]}, "__keep__"
    raw = get(fil["report_url"], limit=MAX_REPORT_BYTES)
    if raw is None:
        rec["status"] = "report_404"
        return rec, None
    if len(raw) > MAX_REPORT_BYTES:
        rec["status"] = "report_too_big"
        return rec, None
    rec["report_bytes"] = len(raw)
    lines = report_lines(raw)
    if len(lines) < SHORT_REPORT and fil.get("package_url"):
        # דוח שמחולק לכמה קבצי XHTML (TBC Bank: הראשון 651 שורות) - כל המסמכים בחבילה
        extra = package_lines(fil["package_url"], fil["report_url"])
        rec["package_tried"] = True
        if extra:
            rec["package_docs"] = len(extra)
            for more in extra:
                lines += more
    rec["report_lines"] = len(lines)
    if lei in DEBUG_LEIS:
        os.makedirs(os.path.join(OUT_DIR, "debug"), exist_ok=True)
        with gzip.open(os.path.join(OUT_DIR, "debug", f"{lei}.txt.gz"), "wt", encoding="utf-8") as f:
            f.write("\n".join(lines))
    text, heads = find_sections(lines)
    rec["heads"] = heads[:12]
    rec["section_chars"] = len(text)
    rec["status"] = "ok" if text and any(h["w"] == 3 for h in heads) else ("partial" if text else "no_section")
    return rec, text


# ---------------------------------------------------------------- main

def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    uni = load_universe()
    order = sorted(uni, key=lambda l: -uni[l]["venues_total"])  # "גודל" משוער: מס' זירות מסחר
    rank = {l: i + 1 for i, l in enumerate(order)}
    order = [l for l in PRIORITY if l in uni] + [l for l in order if l not in PRIORITY]
    rpath = os.path.join(OUT_DIR, "reports.json")
    spath = os.path.join(OUT_DIR, "sections.json.gz")
    reports = load_json(rpath, {}).get("rows", {})
    try:
        with gzip.open(spath, "rt", encoding="utf-8") as f:
            sections = json.load(f)
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        sections = {}
    def short(r):  # דוח קצר בלי סעיף - אולי דוח מרובה מסמכים שנקרא חלקית (לפני package_lines)
        return r.get("status") in ("no_section", "partial") and (r.get("report_lines") or 0) < SHORT_REPORT \
            and "package_tried" not in r

    todo = [l for l in order if REFRESH or LEIS or l not in reports
            or reports[l].get("pv") != PARSER_VERSION or reports[l].get("checked", "") < date.today().isoformat()[:7]
            or short(reports[l])]
    if LIMIT:
        todo = todo[:LIMIT]
    log(f"universe={len(uni)} ({','.join(sorted(COUNTRIES)) if not LEIS else 'LEIS'}) cached={len(reports)} todo={len(todo)}")
    deadline = time.monotonic() + BUDGET_MIN * 60
    done = 0
    for lei in todo:
        if time.monotonic() > deadline:
            log("time budget reached; rest next run")
            if os.environ.get("MORE_FLAG"):  # ה-workflow מפעיל את הריצה הבאה כשהקובץ קיים
                open(os.environ["MORE_FLAG"], "w").write(str(len(todo) - done))
            break
        try:
            rec, text = process(lei, uni[lei], reports.get(lei))
        except Exception as e:  # noqa: BLE001
            rec, text = {"n": uni[lei]["n"], "isin": uni[lei]["isin"], "cc": uni[lei]["cc"], "pv": PARSER_VERSION,
                         "checked": date.today().isoformat(), "status": "error",
                         "why": f"{type(e).__name__}:{str(e)[:120]}"}, None
        rec["k"] = rank[lei]
        reports[lei] = rec
        if text and text != "__keep__":
            sections[lei] = text
        elif text is None and rec["status"] not in ("error",):
            sections.pop(lei, None)
        done += 1
        log(f"  [{done}/{len(todo)}] {rec['status']:<12} {rec.get('section_chars', 0):>6}ch "
            f"{(rec.get('filing') or {}).get('period_end', '-')} {lei} {rec['n']}")
        if done % 50 == 0:
            save(rpath, spath, reports, sections)
    save(rpath, spath, reports, sections)
    by = {}
    for r in reports.values():
        by[r["status"]] = by.get(r["status"], 0) + 1
    log(f"by status: {by}")


def save(rpath, spath, reports, sections):
    by = {}
    for r in reports.values():
        by[r["status"]] = by.get(r["status"], 0) + 1
    with open(rpath, "w", encoding="utf-8") as f:
        json.dump({"generated_at": date.today().isoformat(), "parser_version": PARSER_VERSION, "by_status": by,
                   "rows": dict(sorted(reports.items(), key=lambda kv: kv[1].get("k", 10**6)))},
                  f, ensure_ascii=False, indent=0)
    with gzip.open(spath, "wt", encoding="utf-8") as f:
        json.dump(dict(sorted(sections.items())), f, ensure_ascii=False, separators=(",", ":"))
    q = {}
    for lei, t in sections.items():
        r = reports.get(lei) or {}
        if r.get("status") not in ("ok", "partial") or len(t) < 300:
            continue
        q[lei] = {"n": r.get("n"), "u": (r.get("filing") or {}).get("report_url"), "t": t,
                  "r": f"{r['status']}:{(r.get('filing') or {}).get('period_end')}", "k": r.get("k", 10**6),
                  "cc": r.get("cc"), "isin": r.get("isin")}
    with gzip.open(QUEUE_PATH, "wt", encoding="utf-8") as f:
        json.dump(q, f, ensure_ascii=False, separators=(",", ":"))


if __name__ == "__main__":
    sys.exit(main())

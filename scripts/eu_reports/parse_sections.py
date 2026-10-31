#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
parse_sections.py

קורא דטרמיניסטי (בלי מודל שפה) של רשימות בעלי המניות המהותיים בסעיפים שחתך fetch_reports.py מהדוחות
השנתיים - לכל המדינות. ממלא את הדף מיד, בלי לחכות למכסת Gemini; Gemini (extract_eu_reports.py) גובר עליו
בכל חברה שעיבד (build_eu_page ב-Revach מעדיף את תוצאות המודל).

מה נקרא: אחרי כותרת/משפט של בעלי מניות מהותיים (substantial / significant / major shareholders, DTR 5,
notified ..., וכותרות בשפות האיחוד) - שורות של מחזיק עם אחוז:
  * בשורה אחת:  "Spaldy Investments Limited 355,723,914 52.88% Direct" / "• FitzWalter Capital 12.53%;"
  * בכמה שורות (טבלה מ-PDF):  "BlackRock, Inc." / "29 June 2021" / "9.90%"
כל ערך הוא מחרוזת כפי שכתובה; הציטוט (evidence) הוא השורות עצמן - כך שהוא עובר את בדיקת ה-grounding.
פסילה: סכום האחוזים מעל 100 / שם שנראה כמו כותרת או משפט / אחוז מחוץ לתחום.

קלט:  data/eu_reports/sections.json.gz, data/eu_reports/reports.json
פלט:  scripts/eu_reports/results_eu_reports_parser.jsonl (נכתב מחדש בכל הרצה), באותה סכמה כמו Gemini.
"""

import gzip
import json
import os
import re
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
D = os.path.join(ROOT, "data", "eu_reports")
OUT = os.path.join(HERE, "results_eu_reports_parser.jsonl")
SCHEMA_VERSION = 3

ANCHOR = re.compile(
    r"substantial\s+(share\s*)?(holdings?|interests)|major\s+(share)?holders?|major\s+shareholdings?|"
    r"significant\s+(share)?holders?|significant\s+shareholdings?|interests\s+in\s+voting\s+rights|"
    r"notifiable\s+interests|major\s+interests\s+in|\bDTR\s*5\b|rule\s+5\s+of\s+the|notified\s+(to\s+)?(the\s+)?"
    r"(company|group)?\s*(of|under|between)|following\s+changes|holdings?\s+of\s+3\s?%|largest\s+shareholders|principal\s+shareholders|"
    r"shareholder\s+structure|ownership\s+structure|aktionärsstruktur|stimmrechtsmitteilung|bedeutende\s+beteiligung|"
    r"répartition\s+du\s+capital|actionnariat|franchissements?\s+de\s+seuils?|partecipazioni\s+rilevanti|"
    r"azionisti\s+rilevanti|participaciones\s+significativas|estructura\s+accionarial|participações\s+qualificadas|"
    r"substantiële\s+deelnemingen|grootaandeelhouders|största\s+aktieägar|storaktionær|største\s+aksjonær|"
    r"suurimmat\s+osakkeenomistajat|znaczne\s+pakiety|akcjonariusze\s+posiadający|"
    # כותרות נפוצות נוספות בשפות האיחוד (טבלאות המחזיקים בדוחות שאינם בריטיים)
    r"struktura\s+akcjonariatu|znaczni\s+akcjonariusze|główni\s+akcjonariusze|aandeelhoudersstructuur|"
    r"belangrijkste\s+aandeelhouders|meldingen?\s+(van\s+)?(zeggenschap|substantiële)|"
    r"största\s+ägare|största\s+aktieägarna|ägarstruktur|ägarförhållanden|huvudägare|"
    r"største\s+aktionærer|aktionærforhold|ejerforhold|største\s+aksjonærer|aksjonærstruktur|"
    r"suurimmat\s+osakkaat|omistusrakenne|merkittävät\s+osakkeenomistajat|"
    r"principali\s+azionisti|assetto\s+proprietario|azionariato|"
    r"principales\s+accionistas|accionistas\s+significativos|estructura\s+del\s+capital|"
    r"principaux\s+actionnaires|structure\s+de\s+l['’]actionnariat|"
    r"großaktionäre|wesentliche\s+beteiligungen|aktionäre\s+mit|"
    r"principais\s+accionistas|estrutura\s+acionista|"
    r"μετοχική\s+σύνθεση|μέτοχοι\s+με|κύριοι\s+μέτοχοι|"
    r"holding\s+of\s+3\s?%\s+or\s+more|holdings?\s+of\s+(3|5)\s?%", re.I)
# שורה שהיא כותרת עמודה / טקסט כללי - לא שם מחזיק
NOT_NAME = re.compile(
    r"^(name|shareholder|holder|person|investor|director|total|date|number|percentage|nature|notes?|source)\b|"
    r"\b(shares?\s+held|voting\s+rights|of\s+(issued|total)|share\s+capital|notification|notified|disclosed|"
    r"as\s+at|direct\s+or\s+indirect|per\s*cent|percentage|annual\s+report|page\s+\d|the\s+company|the\s+group|"
    r"aktien|actions|azioni|acciones|aandelen|aktier|osakkeet|akcji)\b", re.I)
WS = re.compile(r"\s+")
PCT = r"(?:<\s*)?\d{1,2}(?:[.,]\d{1,3})?\s?%"
SHARES = r"\d{1,3}(?:[,.   ]\d{3})+\d?"          # כולל ספרת הערת שוליים דבוקה ("3,668,756,7652")
INLINE = re.compile(
    rf"^(?:[•●▪\-–]\s*)?(?P<name>[A-ZÀ-ÖØ-Þ][^%\d|]{{2,90}}?)(?:\s*\(?\d\)?)?\s*[:|,]?\s+"
    rf"(?:(?P<sh>{SHARES})\s*\|?\s+)?(?P<pct>{PCT})(?=\s|;|,|\.|$)")
PCT_LINE = re.compile(rf"^\(?(?P<pct>{PCT})\)?\s*(?:[;,.]|\d)?$")
INLINE_DEC = re.compile(
    rf"^(?P<name>[A-ZÀ-ÖØ-Þ][^%\d|]{{2,90}}?)(?:\s*\*+)?\s+(?:(?P<sh>{SHARES})\s+)?(?P<pct>\d{{1,2}}[.,]\d{{1,3}})"
    r"(?:\s+(?:\d{1,2}[./ ]\w+[./ ]\d{4}|\d{1,2}\s+\w+\s+\d{4}))?\s*$")
DEC_LINE = re.compile(r"^(?P<pct>\d{1,2}(?:\.\d{1,3}|,\d{2}))$")  # "5.78" בטבלה עם כותרת "%"; "3,500" = אלפים
SHARES_LINE = re.compile(rf"^(?P<sh>{SHARES})(?:\s*\d)?$")
DATEISH = re.compile(r"^\d{1,2}(?:st|nd|rd|th)?\s+[A-Za-zÀ-ÿ]+\.?(?:\s+\d{4})?$|^[A-Za-zÀ-ÿ]+\s+\d{4}$|^\d{4}$|"
                     r"^\d{1,2}[./-]\d{1,2}[./-]\d{2,4}$|^(direct|indirect|direct\s+and\s+indirect)$", re.I)
FOOT_LINE = re.compile(r"^\(?\d{1,2}\)?$")
PROSE = re.compile(r"^[A-Za-z].{60,}$")
AS_OF = re.compile(r"\b(?:as\s+at|as\s+of|at)\s+(\d{1,2}\s+[A-Z][a-z]+\s+\d{4})", re.I)


def norm(s):
    return WS.sub(" ", s or "").strip()


# טבלאות תגמול / מדיניות שמופיעות ליד אחזקות הדירקטורים ("Under the proposed Policy 50%")
NOISE_NAME = re.compile(r"^(under|minimum|maximum|threshold|target|stretch|bonus|award|executive|non-executive|"
                        r"in\s+line|on-target|vesting|salary|pension)\b|\b(policy|award|bonus|ltip|rsp|psp|vesting|"
                        r"salary|threshold|performance|expectation|representing|remuneration|incentive|opportunity|"
                        r"maximum|minimum)\b", re.I)
SENTENCE_TAIL = re.compile(r"\s*(of|will|is|are|was|were|for|to|in|on|at|by|and|which|that|if|when|or\s+more\s+of\s+the\s+\w+\s+(salary|bonus))\b(?!\s+(the\s+)?(issued|voting|ordinary|share|total))", re.I)
CONT_WORD = re.compile(r"(management|capital|group|holdings?|securities|investors?|partners|partnership|asset|"
                       r"limited|ltd|llp|llc|plc|inc|sachs|associates|advisors|advisers|fund|funds|investments?|"
                       r"trust|corporation|company|co\.|n\.v\.|s\.a\.|ag|se|gmbh|services|international)\b", re.I)
TABLE_HEAD = re.compile(r"^(%\s+of\s+(total|issued|the)|percentage\s+of|person\s+interested|name\s+of\s+(share)?holder|"
                        r"(major\s+|substantial\s+)?shareholders?$|shares?\s+held|ordinary\s+shares\s+held|"
                        r"number\s+of\s+(ordinary\s+)?shares\s*(held)?$|voting\s+rights$|%\s+of\s+voting)", re.I)
OTHERS = re.compile(
    r"^(pozostali|pozostałe|razem|ogółem|övriga|övrigt|totalt|summa|øvrige|i\s+alt|muut|yhteensä|autres|total|"
    r"altri|altro|mercato|totale|otros|resto|total|sonstige|streubesitz|gesamt|outros|overige|totaal|λοιποί|σύνολο|"
    r"free\s*float|flottant|publiczność|egne\s+aktier|egna\s+aktier|actions\s+propres|auto-?détention|"
    r"azioni\s+proprie|autocartera|eigene\s+aktien|omat\s+osakkeet|akcje\s+własne|eigen\s+aandelen|ações\s+próprias|"
    # סיכומי ביניים, עובדים וחברי מועצה כקבוצה, טבלאות שווי (תחילת השם)
    r"sub-?total[ei]?|sous-total|zwischensumme|salariés|conseil\s+de|membres\s+du|dont\b|"
    r"accionistas\s+(internacionales|nacionales|minoritarios)|substansvärde\w*|redovisning\w*|epra\b|difesa|"
    r"autocontr[oô]le|auto-?détention|administration\s+et|régions|consulting\s+et)\b",
    re.I)
# פילוח גיאוגרפי ("Sverige 55%" / "USA 22%") - שם שהוא רק מדינה
COUNTRY_ONLY = re.compile(
    r"^(sverige|sweden|usa|us|united\s+states|storbritannien|uk|united\s+kingdom|norge|norway|luxemburg|luxembourg|"
    r"tyskland|germany|deutschland|finland|suomi|danmark|denmark|frankrike|france|allemagne|italia|italy|españa|spain|"
    r"nederland|netherlands|belgique|belgium|schweiz|switzerland|suisse|österreich|austria|polska|poland|irland|ireland|"
    r"övriga\s+världen|rest\s+of\s+(the\s+)?world|europe|europa|asia|nordics?|norden(\s+exkl\.?\s+\w+)?|greece|"
    r"internacionales|nacionales|privatpersoner|private\s+individuals|personnes\s+physiques|persone\s+fisiche)\W*$", re.I)
# קטגוריית מחזיקים - רק כשזה כל השם ("Households" / "Public"), לא "Public Investment Corporation"
CATEGORY = re.compile(
    r"^(households|companies|kotitaloudet|yritykset|nominees?(\s+registered)?|hallintarekisteröidyt|"
    r"financial\s+institutions|public(\s+sector)?|foreign(\s+investors)?|ulkomaiset|employees|anställda|ansatte|"
    r"dipendenti|empleados|fonder|funds|fondi|fonds|directors|board|management|institutional(\s+investors)?|"
    r"private\s+investors|retail(\s+investors)?|particuliers|investisseurs(\s+institutionnels)?|"
    r"swedish\s+\w+|international\s+\w+)\W*$", re.I)
# כותרת עמודה שנדבקה לשם: "Azionista % Dompé Holdings" / "Aandeelhouder % AXA SA" / "Azionista Partecipazione Materiale X"
HEAD_PREFIX = re.compile(r"^(nombre|azionista|azionisti|aandeelhouder|aandeelhouders|shareholders?|akcjonariusz|aktieägare|"
                         r"aktionær|osakkeenomistaja|actionnaires?|accionistas?|aktionär|acionista)"
                         r"(\s+(%|partecipazione(\s+\w+)?|directas|indirectas|total))*\s+", re.I)
FOOT_TAIL = re.compile(r"\s*\(?\d{1,2}\)?$")
SMALL = {"of", "and", "the", "de", "du", "la", "le", "des", "van", "von", "der", "den", "di", "del", "y", "et", "&",
         "und", "och", "og", "for", "in", "on", "to", "a", "an", "group", "inc", "ltd", "llc", "plc", "sa", "ag", "nv",
         "se", "spa", "s.a.", "n.v.", "a/s", "ab", "asa", "oyj", "gmbh", "lp", "l.p."}


def name_ok(n):
    """שם מחזיק: מתחיל באות גדולה, רוב המילים באותיות גדולות (לא משפט), בלי הערת שוליים בסוף."""
    n = norm(n).strip(" •-–:;,")
    # "BlackRock, Inc. 2" -> "BlackRock, Inc.": הערות שוליים אחת-אחת (תבנית עם חזרה מקוננת "(…)+$" קורסת
    # אקספוננציאלית על שורת טבלה עם עשרות מספרים - זה מה שתקע את הריצה ל-3.5 שעות)
    while True:
        m = FOOT_TAIL.search(n)
        if not m or m.start() == 0:
            break
        n = n[:m.start()]
    n = re.sub(r",?\s+(held|holds|owns|owned)(\s+an?)?$", "", n, flags=re.I)
    n = re.sub(r"^(direct\s*(/|and|&)\s*indirect|direct|indirect)\s+", "", n, flags=re.I)  # עמודת "Nature of holding"
    n = re.sub(r"^(\d{1,2}\s+)?(january|february|march|april|may|june|july|august|september|october|november|december)\s+\d{4}\s+",
               "", n, flags=re.I)                                # "December 2025 FIL Limited"
    n = re.sub(r"\s+(below|above|under|over|less\s+than|more\s+than)$", "", n, flags=re.I)  # "Aberdeen Group plc Below 5%"
    n = re.sub(r"^(shareholdings?|shareholders?|name)\s*%?\s+", "", n, flags=re.I)  # כותרת שנדבקה לשם
    if re.fullmatch(r"(limited|ltd\.?|plc|inc\.?|llc|lp|llp|sa|ag|nv|bv|treasury\s+shares?|direct|indirect|below|above|"
                    r"funds?|total|others?|public|male|female|men|women|other\s+shareholders?)", n, re.I):
        return None                                              # שם שנחתך / מניות באוצר
    if re.search(r"\bof\s+which\b|\bheld\s+an?\b", n, re.I):
        return None                                              # "Power Limited, of which Ceres held a 24.2%"
    n = re.sub(r"\s+\d{1,3}(,\d{1,3})*,?$", "", n)              # "Soros Fund Management 2,491," (מניות שנחתכו)
    if re.search(r"\d{1,3},\d{3}|\d\.\d", n) or not n.split() or n.split()[-1].lower() in {"of", "and", "the", "to", "a", "an", "for", "in", "on", "&", "by", "with", "et", "und", "och"}:
        return None                                              # שתי שורות טבלה שהתמזגו / "ROCE to"
    n = re.sub(r"\s*\.{3,}.*$", "", n)                               # "Fosun ......................"
    n = re.sub(r"^(hausse|baisse|par)\s+", "", n, flags=re.I)          # "Hausse The Capital Group" / "Par UBS Group AG"
    n = re.sub(r"\s+\d{2,4}[A-Z]?$", "", n)                            # "UBS Group AG 225C" (מספר סעיף)
    if re.match(r"^(chapitre|chapter|kapitel|capitolo|million|mio|mn)\b", n, re.I) or re.search(r"total\s+shareholder", n, re.I):
        return None
    n = HEAD_PREFIX.sub("", n)
    if COUNTRY_ONLY.match(n) or re.fullmatch(r"(hausse|baisse)", n, re.I) or re.search(r"\ba\s+cédé\b", n, re.I):
        return None
    if OTHERS.search(n) or CATEGORY.match(n) or re.match(r"^(directas|indirectas)\b", n, re.I):
        return None
    if re.search(r"^other\s+shareholders|ethnic|\basian\b|african|caribbean|^white\b|prefer\s+not|"
                 r"=|\bindex\b|\bdirectors\b|securities\s+lending|\bcfds?\b|\bswaps?\b|instruments?$", n, re.I):
        return None                                              # מקרא גרף / "The Executive Directors’"
    if NOISE_NAME.search(n) or (")" in n and "(" not in n):
        return None
    if len(n) < 3 or len(n) > 90 or NOT_NAME.search(n) or len(n.split()) > 10:
        return None
    if not re.match(r"[A-ZÀ-ÖØ-Þ]", n) or not re.search(r"[A-Za-zÀ-ÿ]{2}", n):
        return None
    words = [w for w in re.findall(r"[A-Za-zÀ-ÿ][\w.&'’/-]*", n) if w.lower() not in SMALL]
    if words and sum(1 for w in words if w[0].isupper()) / len(words) < 0.6:
        return None                                            # "I spoke with our largest shareholders" / "At threshold performance"
    return n


def shares_clean(sh):
    """"3,668,756,7652" -> "3,668,756,765" (ספרת הערה דבוקה לקבוצה האחרונה); אחרת כפי שהוא."""
    if not sh:
        return None
    parts = re.split(r"[,.   ]", sh)
    if len(parts) > 1 and len(parts[-1]) == 4:
        return sh[:-1]
    return sh


def pct_val(p):
    try:
        return float(re.sub(r"[<\s%]", "", p).replace(",", "."))
    except ValueError:
        return None


def parse_block(lines, start, header_has_pct):
    """מ-start עד סוף הרשימה -> [(name, shares, pct, evidence_lines)]."""
    out, buf, vals, ev = [], [], {}, []
    prose_after = 0

    def flush():
        nonlocal buf, vals, ev
        if buf and vals.get("pct"):
            n = name_ok(" ".join(buf))
            if n:
                out.append((n, shares_clean(vals.get("sh")), vals["pct"], " ".join(ev)))
        buf, vals, ev = [], {}, []

    scanned = 0
    for ln in lines[start:start + 160]:
        t = norm(ln)
        if not t or t == "[...]":
            continue
        scanned += 1
        if TABLE_HEAD.match(t):
            scanned = 0           # כותרת טבלת מחזיקים (גם אחרי עמוד טקסט בפריסת עמודות, כמו Metro / Barclays)
        far = not out and scanned > 40   # שורה ראשונה רחוקה מהעוגן ומכותרת - טבלה אחרת (תגמול, פילוח)
        m = INLINE.match(t) or (header_has_pct and INLINE_DEC.match(t))
        # "Ordinarily 50% of the annual bonus will be..." - משפט שממשיך אחרי האחוז, לא שורת מחזיק
        if m and SENTENCE_TAIL.match(t[m.end():]):
            m = None
        if m and name_ok(m.group("name")) and not far:
            n, e = name_ok(m.group("name")), t
            # שם שנשבר לשתי שורות: "Schroder Investment" / "Management LLP 71,022,015 5.09%"
            if buf and (CONT_WORD.match(n) or re.search(r"(&|\band|\bof|,)$", buf[-1])) and name_ok(" ".join(buf[-2:] + [n])):
                n, e = name_ok(" ".join(buf[-2:] + [n])), " ".join(buf[-2:] + [t])
            buf, vals, ev = [], {}, []
            out.append((n, shares_clean(m.group("sh")), norm(m.group("pct")), e))
            prose_after = 0
            continue
        mp = PCT_LINE.match(t) or (header_has_pct and DEC_LINE.match(t))
        if mp and buf and far:
            buf, vals, ev = [], {}, []
            continue
        if mp and buf:
            vals["pct"] = norm(mp.group("pct"))
            ev.append(t)
            flush()
            continue
        ms = SHARES_LINE.match(t)
        if ms and buf:
            vals["sh"] = ms.group("sh")
            ev.append(t)
            continue
        if (DATEISH.match(t) or FOOT_LINE.match(t)) and buf:
            ev.append(t)          # תאריך / "direct" / הערת שוליים ("1") בין השם למספרים
            continue
        if PROSE.match(t) and len(t.split()) >= 9:
            if out:
                prose_after += 1
                if prose_after >= 2:
                    break
            buf, vals, ev = [], {}, []
            continue
        if not vals and (name_ok(t) or (buf and name_ok(" ".join(buf + [t])))):  # "Bharti" / "Televentures UK" / "Limited"
            buf.append(t)
            ev.append(t)
            if len(buf) > 4:          # יותר מדי שורות "שם" רצופות - לא טבלה
                buf, ev = buf[-1:], ev[-1:]
        else:
            buf, vals, ev = [], {}, []
    flush()
    return out


DISCLOSURE = re.compile(r"\bDTRs?\s?\d*\b|notifiable\s+interests|notified|notification|rule\s+5|transparency|disclosure\s+guidance|"
                        r"3\s?%\s+or\b|following\s+(holdings|interests|changes)", re.I)


def valid(rows):
    vals = [pct_val(r[2]) for r in rows]
    return bool(rows) and all(v is not None and 0 < v <= 100 for v in vals) and sum(vals) <= 100.5


def parse_section(text):
    """בלוק לכל עוגן (כותרת קצרה, או משפט גילוי - DTR / notified); כל בלוק נבדק בנפרד, ואז הבלוקים
    התקינים מאוחדים (מאוחר גובר - "following changes were notified"). איחוד שעובר 100% -> הבלוק הגדול ביותר."""
    lines = text.split("\n")
    blocks, asof = [], None
    for i, ln in enumerate(lines):
        am = ANCHOR.search(ln)
        if not am:
            continue
        # כותרת קצרה ("Major shareholders") או משפט גילוי (DTR / notified); אזכור בתוך פסקה
        # ("discussed with major shareholders," / "a significant shareholder of Metro Bank, through his...") - לא עוגן
        if not DISCLOSURE.search(ln) and (len(ln) > 60 or am.start() > 40 or ln.rstrip().endswith(",")):
            continue
        head = " ".join(lines[i:i + 12])
        header_has_pct = "%" in head or re.search(r"per\s*cent|percentage|prozent|pourcentage", head, re.I)
        block = parse_block(lines, i, bool(header_has_pct))
        if not valid(block):
            continue
        blocks.append(block)
        if not asof:
            m = AS_OF.search(" ".join(lines[max(0, i - 2):i + 6]))
            asof = m.group(1) if m else None
    if not blocks:
        return None, asof
    found = {}
    for block in blocks:
        for n, sh, pct, ev in block:
            found[n.lower()] = (n, sh, pct, ev)
    holders = list(found.values())
    if not valid(holders):
        holders = max(blocks, key=len)
    return holders, asof


def to_date(s):
    for fmt in ("%d %B %Y", "%d %b %Y"):
        try:
            return datetime.strptime(s, fmt).date().isoformat()
        except (ValueError, TypeError):
            pass
    return None


def main():
    with gzip.open(os.path.join(D, "sections.json.gz"), "rt", encoding="utf-8") as f:
        sections = json.load(f)
    reports = json.load(open(os.path.join(D, "reports.json"), encoding="utf-8"))["rows"]
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    n_ok = 0
    with open(OUT, "w", encoding="utf-8") as f:
        for lei, text in sections.items():
            r = reports.get(lei) or {}
            holders, asof = parse_section(text)
            if not holders:
                continue
            n_ok += 1
            rec = {"lei": lei, "model": "parser", "ts": now, "schema": SCHEMA_VERSION,
                   "url": (r.get("filing") or {}).get("report_url"), "n": r.get("n"), "cc": r.get("cc"),
                   "isin": r.get("isin"), "as_of_date": to_date(asof), "shares_outstanding": [], "classes": [],
                   "holders": [{"name": n, "role": "substantial_holder", "class": None, "shares_text": sh,
                                "percent_text": pct, "voting_percent_text": None, "as_of_text": asof, "evidence": ev}
                               for n, sh, pct, ev in holders],
                   "structure": None, "control": None, "no_data_reason": None,
                   "grounding": {"holders_in": len(holders), "holders_kept": len(holders), "holders_dropped": 0,
                                 "fields_nulled": 0}}
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    print(f"parser: {n_ok} of {len(sections)} sections with a substantial-holders list -> {OUT}")


if __name__ == "__main__":
    main()

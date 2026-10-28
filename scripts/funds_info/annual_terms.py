# -*- coding: utf-8 -*-
"""
annual_terms.py — הון כשר / Revach
מהדוח השנתי של כל קרן סל (טופס ק121 במאיה, PDF): עמלת יצירה, עמלת פדיון,
והצהרת "מגבלת שווי הזמנה".

ניסוחים שנמצאו (דוח אחד לכל מנהל, 2026):
  מיטב/אי.בי.אי/אנליסט/ילין: "שיעור עמלת פדיון: ... בשיעור של 0.30%" + שורה מקבילה ליצירה
  מור:                        "שיעור עמלת יצירה: 0.10% משווי היצירה"
  הראל/מגדל:                  "עמלת היצירה ... 0.3% ממחיר היחידה" / "עמלת הפדיון ... 0.3% ממחיר הפדיון"
  קסם:                        "שיעור עמלת פדיון ויצירה: 0.2%"
  מגבלה: מיטב/אי.בי.אי/אנליסט/ילין/מור — "מגבלת שווי הזמנה: לא קיימת ...";
         קסם — "קיימת מגבלת שווי הזמנה ... ראו ... ק79"; הראל/מגדל — אין שורה כזו.
pypdf מחלץ לעתים שורות עבריות בסדר מקטעים הפוך (למשל ". 0.10% ... שיעור עמלת יצירה:"),
ולכן כל חיפוש הוא "השורה מכילה X וגם אחוז", בלי הנחה על הסדר.
"""
import io, json, os, re, time

from pypdf import PdfReader

PCT = re.compile(r"(\d*\.\d+|\d+)\s*%")
FUND7 = re.compile(r"(?<!\d)(1\d{6})(?!\d)")


def _lines(text):
    return [re.sub(r"\s+", " ", ln).strip() for ln in text.splitlines() if ln.strip()]


FEE_MAX = 3.0   # עמלת יצירה/פדיון גבוהה מזה = נקלט אחוז משורה אחרת


def _pct_after(lines, i, span=2):
    for j in range(i, min(i + span + 1, len(lines))):
        for m in PCT.finditer(lines[j]):
            v = float(m.group(1))
            if v <= FEE_MAX:
                return v
    return None


# שורת עמלה אמיתית — לא "ללא ניכוי עמלת פדיון" / "ללא תוספת עמלת יצירה" / כותרת "שישולמו"
def _fee_line(ln, kind):
    if "ללא" in ln or "שישולמו" in ln:
        return False
    if kind == "c":
        return bool(re.search(r"עמלת\s*ה?יצירה|פדיון\s*ויצירה|יצירה\s*(או|ו)\s*פדיון", ln))
    return bool(re.search(r"עמלת\s*ה?פדיון|פדיון\s*ויצירה|יצירה\s*(או|ו)\s*פדיון", ln))


def parse_annual(text):
    lines = _lines(text)
    creation = redemption = None
    limit, limit_text = "", ""
    for i, ln in enumerate(lines):
        if creation is None and _fee_line(ln, "c"):
            creation = _pct_after(lines, i)
        if redemption is None and _fee_line(ln, "r"):
            redemption = _pct_after(lines, i)
        if not limit and "מגבלת שווי הזמנה" in ln:
            block = " ".join(lines[i:i + 4])
            limit_text = block[:300]
            if "לא קיימת" in block:
                limit = "אין"
            elif re.search(r"ק\s*0?79", block) or "קיימת מגבלת" in block:
                limit = "יש (ק079)"
            else:
                limit = "?"
    return {"creation_fee": creation, "redemption_fee": redemption,
            "limit": limit or "לא מצוין", "limit_text": limit_text}


def fund_of(attachment_name, text, known_ids):
    m = re.search(r"res_(\d{6,8})_", attachment_name or "")
    if m and int(m.group(1)) in known_ids:
        return int(m.group(1))
    for x in FUND7.findall(text[:4000]):
        if int(x) in known_ids:
            return int(x)
    return int(m.group(1)) if m else None


def pdf_attachment(meta):
    atts = [a for a in (meta.get("attachments") or []) if (a.get("fileType") or "").lower().startswith("pdf")]
    return max(atts, key=lambda a: a.get("fileSize") or 0, default=None)


def pdf_to_text(content):
    return "\n".join((p.extract_text() or "") for p in PdfReader(io.BytesIO(content)).pages)


def pdf_texts(content):
    """שני חילוצים: pypdf, ו-pdfium (מילים שלמות אך בסדר הפוך בשורה — מתוקן כאן).
    בחלק מה-PDF-ים ("מונגש") pypdf משבש את סדר האותיות, ו-pdfium לא."""
    out = [pdf_to_text(content)]
    try:
        import pypdfium2 as pdfium
        doc = pdfium.PdfDocument(content)
        raw = "\n".join(doc[i].get_textpage().get_text_range() for i in range(len(doc)))
        out.append("\n".join(" ".join(reversed(ln.split())) for ln in raw.splitlines()))
    except Exception as e:
        print(f"  pdfium: {e}")
    return out


def parse_annual_multi(texts):
    """מריץ parse_annual על כל חילוץ ומאחד: ערך שנמצא גובר על חסר."""
    best = {"creation_fee": None, "redemption_fee": None, "limit": "לא מצוין", "limit_text": ""}
    for t in texts:
        r = parse_annual(t)
        for k in ("creation_fee", "redemption_fee"):
            if best[k] is None and r[k] is not None:
                best[k] = r[k]
        if best["limit"] == "לא מצוין" and r["limit"] != "לא מצוין":
            best["limit"], best["limit_text"] = r["limit"], r["limit_text"]
    return best


def needs_retry(rec):
    return (not rec.get("fund") or rec.get("limit") == "לא מצוין"
            or rec.get("creation_fee") is None or rec.get("redemption_fee") is None
            or (rec.get("creation_fee") or 0) > FEE_MAX)

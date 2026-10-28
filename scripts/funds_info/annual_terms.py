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

PCT = re.compile(r"(\d+(?:\.\d+)?)\s*%")
FUND7 = re.compile(r"(?<!\d)(1\d{6})(?!\d)")


def _lines(text):
    return [re.sub(r"\s+", " ", ln).strip() for ln in text.splitlines() if ln.strip()]


def _pct_after(lines, i, span=2):
    for j in range(i, min(i + span + 1, len(lines))):
        m = PCT.search(lines[j])
        if m:
            return float(m.group(1))
    return None


def parse_annual(text):
    lines = _lines(text)
    creation = redemption = None
    limit, limit_text = "", ""
    for i, ln in enumerate(lines):
        if creation is None and re.search(r"עמלת\s*ה?יצירה|פדיון\s*ויצירה", ln) and "שישולמו" not in ln:
            creation = _pct_after(lines, i)
        if redemption is None and re.search(r"עמלת\s*ה?פדיון", ln) and "שישולמו" not in ln:
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

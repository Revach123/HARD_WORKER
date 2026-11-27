# -*- coding: utf-8 -*-
"""
work_pack.py - חבילת עבודה לכל דוח (בלי AI): הסוכן מקבל אותה מוכנה ולא מוריד / מחפש בעצמו.

לכל דוח (report_id מתוך selection_plan.json - הדוח השנתי + קבצים נוספים):
  packs/<report_id>/manifest.json   - קבצים, מספר עמודים, ומיקום החלקים הייעודיים:
        reg11 (תקנה 11), reg12 (תקנה 12), reg26 (דירקטורים - להחרגה), charts (תרשימי החזקות),
        fs_notes (ביאור חברות מוחזקות / מאוחדות / כלולות / ישויות הקבוצה), exhibit21 (רשימת חברות בנות באנגלית)
  packs/<report_id>/pages/<file>_p<N>.txt  - טקסט עמודי החלקים (+-1), במצב layout ובמצב רגיל
  packs/<report_id>/text/<file>.txt.gz      - כל הטקסט של הקובץ (עמודים מופרדים ב-\\f) - לחיפוש
  packs/<report_id>/images/<file>_p<N>_<k>.png - התמונות המקוריות המוטמעות בעמודי תרשים (חדות יותר מרינדור)
  packs/<report_id>/render/<file>_p<N>.jpg      - רינדור עמודי התרשים ב-200dpi

הרצה:  python work_pack.py --reports 1730567,1728200 [--pdf-dir DIR] [--out packs]
"""
import argparse
import gzip
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unicodedata

BIDI = re.compile("[‎‏‪-‮⁦-⁩]")


def norm(t):
    t = BIDI.sub("", unicodedata.normalize("NFKC", t or ""))
    # גופן שבו נ ממופה ל-"ð" (ווישור, אאורה) - רק בתוך / ליד אותיות עבריות
    return re.sub(r"ð(?=[\u05d0-\u05ea\s\"'״])|(?<=[\u05d0-\u05ea])ð", "נ", t)


def both(t):
    """טקסט + השורות בהיפוך (עברית בסדר חזותי בחלק מהקבצים)."""
    return t + "\n" + "\n".join(line[::-1] for line in t.splitlines())


def pdftotext_pages(path, layout):
    args = ["pdftotext", "-q"] + (["-layout"] if layout else []) + [path, "-"]
    out = subprocess.run(args, capture_output=True, timeout=600).stdout.decode("utf-8", "replace")
    return [norm(p) for p in out.split("\f")]


ENT = re.compile(r'בע"?מ|בע״מ|שותפות|\bLtd\b|\bLimited\b|\bLLC\b|\bInc\b|\bB\.?V\b|\bGmbH\b|\bS\.?A\b|\bL\.?P\b|'
                 r'\bS\.?R\.?L\b|\bsp\.? z|\bPLC\b|\bCorp|\bAG\b|\behf\b|\bd\.o\.o', re.I)
REG11 = re.compile(r"Regulation[ \t]+11\b|תקנה[ \t]*[:\-–]?[ \t]*11(?!\d)|(?<![\d.])11[ \t]*:?[ \t]*תקנה|רשימת\s+(?:ה)?השקעות\s+בחברות|השקעות\s+בחברות[- ]בת\s+ובחברות\s+כלולות", re.I)
REG12 = re.compile(r"תקנה[ \t]*[:\-–]?[ \t]*12(?!\d)|(?<![\d.])12[ \t]*:?[ \t]*תקנה|שינויים\s+בהשקעות\s+בחברות", re.I)
REG26 = re.compile(r"תקנה[ \t]*[:\-–]?[ \t]*26(?!\d)|(?<![\d.])26[ \t]*:?[ \t]*תקנה|הדירקטורים\s+של\s+התאגיד", re.I)
# "תרשים חברות מוחזקות עיקריות" (לאומי), "תרשים ההחזקות", "תרשים מבנה" - כל "תרשים" שאחריו מילה על החזקות / חברות / הקבוצה
CHART = re.compile(r"(?:תרשים|מבנה)\s+(?:של\s+)?(?:ה)?(?:[אה]חזקות|החזקות|אחזקות|הקבוצה|התאגיד|חברות|השקעות)|תרשים\s+מבנה|"
                   r"Organi[sz]ational\s+(?:Structure|chart)|Holdings?\s+structure|Group\s+structure|Structure\s+chart|"
                   r"chart\s+of\s+(?:the\s+)?(?:holdings?|group|corporate)", re.I)
# תרשים שממשיך בעמוד הבא ("פירוט בעמוד הבא" - אשטרום, שיכון ובינוי)
CONT = re.compile(r"(?:פירוט|המשך|ראו|ראה|יפורט)\s+(?:ה?תרשים\s+)?ב?עמוד\s+הבא|בעמוד\s+הבא|continued\s+on\s+(?:the\s+)?next\s+page", re.I)
NOTE = re.compile(r"זכויות\s+בישויות\s+אחרות|(?:לשותפות|לחברה|לקבוצה)\s+החזקות\s+ב(?:מספר\s+)?(?:תאגידים|חברות)|טבלת\s+(?:ה)?[אה]חזקות|נספח\s+(?:ה)?[אה]חזקות|רשימת\s+(?:ה)?תאגידים\s+(?:ה)?מוחזקים|"
                  r"רשימת\s+(?:ה)?(?:חברות|שותפויות)(?:\s+ו(?:ה)?(?:חברות|שותפויות))?\s+(?:ה)?מוחזקות|רשימת\s+(?:ה)?חברות\s+(?:ה|ב)?קבוצה|חברות\s+הקבוצה|חברות\s+מאוחדות|חברות\s+בנות\s+מהותיות|חברות\s+מוחזקות|ישויות\s+הקבוצה|השקע(?:ה|ות)\s+בחברות\s+(?:מוחזקות|כלולות|בנות)|"
                  r"חברות\s+כלולות|פירוט\s+החברות|Principal\s+subsidiaries|Significant\s+subsidiaries|Investments?\s+in\s+(?:subsidiaries|investees|associates)", re.I)
EXH = re.compile(r"Exhibit\s+(?:21|8)(?:\.1)?\b[^\n]{0,80}|List\s+of\s+(?:Significant\s+)?Subsidiaries|Subsidiaries\s+of\s+the\s+(?:Company|Registrant)", re.I)
PCT = re.compile(r"\d[\d.,]*\s*%|%\s*\d")
# רשימת ההחזקות מחוץ לתקנה 11 (תקנה 11 מפנה אליה): "27. חברות בנות וקשורות" בפרק א' (יוניברסל), "נספח ב' - השקעות
# בחברות הבת מהותיות" (אפריקה מגורים), "שיעור החזקות החברה בחברות בנות"
HOLD_LIST = re.compile(r"חברות\s+(?:ה)?(?:בנות|בת)\s+ו(?:ה)?(?:חברות\s+)?(?:ה)?(?:קשורות|כלולות)|שיעור\s+(?:ה)?החזקות\s+(?:ה)?חברה\s+בחברות|"
                       r"השקעות\s+בחברות\s+(?:ה)?(?:בת|בנות)\s+(?:ה)?מהותיות")
# הפניה מעמוד תקנה 11: "ראו נספח ב'" / "ראו סעיף 26 לפרק א'"
REF_APPX = re.compile(r"(?:ראו|ראה|מצורף|ב)\s*נספח\s+([א-ת])(?=['׳\s.,)])")
REF_SEC = re.compile(r"(?:ראו|ראה)\s+סעיף\s+(\d{1,2})\s*(?:ל|ב)?פרק\s+א")
# תת-כותרת ממוספרת של תרשים ("20.2 תרשים אחזקות מג'יק") - עמוד תרשים גם כשהתרשים באמצע עמוד מלא טקסט
CHART_SUB = re.compile(r"(?:^|\n)[ \t]*\.?\d{1,2}(?:\.\d{1,2}){0,3}\.?\s*(?:להלן[ \t]+)?(?:" + CHART.pattern + r")", re.I)
# כותרת "החזקות החברה" / "מבנה ההחזקות" בראש העמוד + הרבה אחוזים = תרשים בטקסט וקטורי (נתנאל: "4. החזקות החברה (100%)")
CHART_HOLD = re.compile(r"(?:^|\n)[ \t]*\.?\d{0,2}\.?[ \t]*(?:ה)?[אה]חזקות[ \t]+(?:ה)?(?:חברה|קבוצה|תאגיד)")
# טבלאות החזקות נוספות: "שם החברה / סכום ההשקעה" (השקעות בפרק א'), טבלת רכישות ("החברה/הפעילות הנרכשת")
TABLES = re.compile(r"שם\s+(?:ה)?חברה\s*/?\s*(?:ה)?סכום\s+(?:ה)?השקעה|(?:החברה|הפעילות|החברות)\s*/\s*(?:ה)?(?:פעילות|חברה)\s+(?:ה)?נרכשת|"
                    r"חברות\s+שנרכשו|טבלת\s+(?:ה)?רכישות|Subsidiaries\s+acquired")
# כותרת ביאור (גם כשהשמות בטבלה קצרים, בלי בע"מ - אברא "ביאור 9 - זכויות בישויות אחרות")
NOTE_HEAD = re.compile(r"ביאור\s*[-–]?\s*\d{1,2}\s*[-–:]?\s*(?:" + NOTE.pattern + r")", re.I)
# תת-כותרת "2. חברות בת" בביאור הכללי (אברא ביאור 1א2) - שורה שכולה הכותרת
SUBS_LINE = re.compile(r"^[ \t.\d()א-ו]{0,6}חברות\s+(?:ה)?(?:בת|בנות)(?:\s+ו(?:חברות\s+)?(?:קשורות|כלולות))?[ \t]*:?[ \t]*$", re.M)
# ביאורים בדוח באנגלית (10-K / 20-F): הביאור הכללי, צירופי עסקים, השקעות, איחוד
NOTE_EN = re.compile(r"(?:\bNOTE\s+\d{1,2}|^[ \t]*\d{1,2}\.)\s*[-–:.]?\s*[^\n]{0,60}?(?:ORGANI[SZ]ATION|DESCRIPTION\s+OF\s+(?:THE\s+)?BUSINESS|"
                     r"BUSINESS\s+COMBINATIONS?|ACQUISITIONS?|PRINCIPLES\s+OF\s+CONSOLIDATION|SUBSIDIARIES|INVESTMENTS?\s+IN)[^\n]{0,40}$", re.I | re.M)


def image_list(path, first, last):
    """תמונות מוטמעות בעמודים (pdfimages -list): [(page, num, w, h)]."""
    try:
        out = subprocess.run(["pdfimages", "-list", "-f", str(first), "-l", str(last), path],
                             capture_output=True, timeout=120).stdout.decode("utf-8", "replace")
    except subprocess.TimeoutExpired:
        return []
    res = []
    for line in out.splitlines()[2:]:
        f = line.split()
        if len(f) > 4 and f[0].isdigit():
            res.append((int(f[0]), int(f[1]), int(f[3]), int(f[4])))
    return res


def vector_chart(raw):
    """עמוד שנראה כמו תרשים החזקות וקטורי: לפחות 15 שורות לא ריקות, רובן קצרות (תיבות), ולפחות 3 אחוזים / מספרים עשרוניים."""
    lines = [x.strip() for x in raw.splitlines() if x.strip()]
    if len(lines) < 15:
        return False
    short = sum(1 for x in lines if len(x) <= 14)
    nums = len(re.findall(r"\d\s*%|%\s*\d|\b\d{1,3}\.\d{1,2}\b", raw))
    words = sum(1 for x in lines if re.search(r"[א-תA-Za-z]{2,}", x))           # תיבות = שמות; טבלה כספית = מספרים
    pct = len(re.findall(r"%", raw))
    big = len(re.findall(r"\d{1,3}(?:,\d{3})+", raw))                           # סכומים (1,234,567) - טבלה כספית
    return short / len(lines) >= 0.65 and nums >= 3 and words / len(lines) >= 0.55 and pct >= 2 and big <= 3


def locate(pages):
    """pages: [(raw, layout)] -> {section: [page numbers 1-based]}."""
    n = len(pages)
    sec = {k: set() for k in ("reg11", "reg12", "reg26", "charts", "fs_notes", "exhibit21", "tables")}
    for i, (raw, lay) in enumerate(pages):
        t = both(raw + "\n" + lay)
        ent = len(ENT.findall(t))
        pct = len(PCT.findall(t))
        head = "\n".join(raw.strip().splitlines()[:12])            # כותרת העמוד
        headb = both(head)
        # עמוד שבראשו תקנה 12 / 13 ("שינויים בהשקעות בחברות בת ובחברות כלולות", "הכנסות של חברות בת וחברות כלולות") אינו
        # תקנה 11, אלא אם גם תקנה 11 בראשו במפורש (ניו-מד עמ' 471)
        not11 = bool(REG12.search(headb) or re.search(r"תקנה[ \t]*[:\-–]?[ \t]*13(?!\d)", headb)) and \
            not re.search(r"תקנה[ \t]*[:\-–]?[ \t]*11(?!\d)", headb)
        if not11:
            pass
        elif REG11.search(t) and (ent >= 3 or pct >= 3):
            sec["reg11"].add(i)
        elif REG11.search(headb) and not REG12.search(headb):
            # כותרת תקנה 11 בראש העמוד, והטבלה תמונה / טקסט מפורק (ישרוטל עמ' 161) - העמוד וההמשך שלו
            sec["reg11"].add(i)
            for j in range(i + 1, min(n, i + 4)):
                if len(pages[j][0].strip()) < 700 or len(ENT.findall(both(pages[j][0]))) >= 4:
                    sec["reg11"].add(j)
                else:
                    break
        if REG12.search(headb) or (REG12.search(t) and ent >= 2):
            sec["reg12"].add(i)
        if REG26.search(headb):
            sec["reg26"].add(i)
        # כותרת תרשים בראש העמוד - עמוד תרשים גם כשיש בו עוד טקסט (דיסקונט עמ' 303: "תרשים מבנה האחזקות של בנק דיסקונט"
        # ואחריו פרק על השליטה בבנק; התרשים יכול להיות וקטורי - בלי תמונה מוטמעת)
        if CHART.search(t) and (ent >= 3 or len(raw.strip()) < 400 or CHART.search(headb)):
            sec["charts"].add(i)
        nums = len(re.findall(r"(?<![\d.,])(?:100(?:\.0+)?|[1-9]\d(?:\.\d+)?)(?![\d,])", t))   # טבלת אחוזים בלי % ליד כל מספר
        if NOTE.search(t) and ent >= 5 and (pct >= 3 or ("%" in t and nums >= 8)):
            sec["fs_notes"].add(i)
        elif NOTE.search(headb) and ent >= 3:
            sec["fs_notes"].add(i)      # כותרת הביאור / הנספח בראש העמוד (ישרוטל "זכויות בישויות אחרות", סקופ "רשימת חברות הקבוצה")
        if EXH.search(t) and ent >= 8:
            sec["exhibit21"].add(i)
        if CHART_SUB.search(raw) or (CHART_HOLD.search(head) and pct >= 5):
            sec["charts"].add(i)
        if TABLES.search(t) and (ent >= 2 or pct >= 2 or len(re.findall(r"20\d\d", t)) >= 4):
            sec["tables"].add(i)
        if HOLD_LIST.search(headb) and (ent >= 2 or pct >= 3) and not not11:
            sec["reg11"].add(i)
        if NOTE_HEAD.search(t) or (SUBS_LINE.search(raw) and "ביאור" in t and ent >= 3) or (NOTE_EN.search(raw) and ent >= 2):
            sec["fs_notes"].add(i)
    # הפניה מתקנה 11 לנספח / לסעיף בפרק א': העמוד שבכותרתו הנספח / הסעיף
    for i in sorted(sec["reg11"]):
        t = both(pages[i][0])
        for m in REF_APPX.finditer(t):
            hdr = re.compile(r"^\s*נספח\s+" + m.group(1) + r"\b", re.M)
            for j in range(n):
                if j != i and hdr.search("\n".join(pages[j][0].strip().splitlines()[:4])) and len(ENT.findall(both(pages[j][0]))) >= 3:
                    sec["reg11"].add(j)
        for m in REF_SEC.finditer(t):
            num = m.group(1)
            hdr = re.compile(r"(?:^|\n)\s*\.?\s*" + num + r"\s*\.?\s*[א-ת]")
            for j in range(min(i, n)):
                if hdr.search("\n".join(pages[j][0].strip().splitlines()[:6])) and len(ENT.findall(both(pages[j][0]))) >= 3:
                    sec["reg11"].add(j)
    # המשך טבלה: עמוד אחרי תקנה 11 / ביאור / exhibit עם הרבה ישויות - עד 4 עמודים
    for k in ("reg11", "fs_notes", "exhibit21", "tables"):
        for i in sorted(sec[k]):
            for j in range(i + 1, min(n, i + 5)):
                raw, lay = pages[j]
                t = both(raw + "\n" + lay)
                h8 = both("\n".join(raw.strip().splitlines()[:8]))
                if len(ENT.findall(t)) >= 4 and not (k == "reg11" and (REG12.search(h8) or REG26.search(h8) or
                                                                       re.search(r"^\s*נספח\s+[א-ת]", h8, re.M))):
                    sec[k].add(j)
                else:
                    break
    # תרשים: עמוד הכותרת + עמודים סמוכים כמעט בלי טקסט (תמונה) + העמוד שאחרי (הערות התרשים)
    for i in sorted(sec["charts"]):
        for j in (i - 1, i + 1, i + 2):
            if 0 <= j < n and (len(pages[j][0].strip()) < 400 or CHART.search(both(pages[j][0]))):
                sec["charts"].add(j)
    return {k: sorted(p + 1 for p in v) for k, v in sec.items()}


SEC_FORM = re.compile(r"\b(?:20|10)[- ]?[FK]\b|\bF[- ]?20\b|\b6[- ]?K\b|Form\s+(?:20|10|6)", re.I)
TRANSL = re.compile(r"translat|תרגום|מתורגם", re.I)


def doc_kind(title, man):
    """סוג המסמך שבחבילה, לסוכן:
      "translation"  - תרגום (בדרך כלל חלקי) ולא הדוח המקורי (או.פי.סי: "Unofficial English translation of certain sections")
      "sec_notice"   - הודעה בלבד על הגשה ל-SEC (20-F / 10-K / 6-K), בלי רשימת החברות - הרשימה מ-EDGAR (Exhibit 8 / 21)
      "sec_filing"   - הטופס עצמו (20-F / 10-K) - רשימת החברות ב-Exhibit 8 / 21, לרוב רק הפניה אליו
      "periodic"     - דוח תקופתי רגיל"""
    t = title or ""
    pages = sum(f.get("pages") or 0 for f in man.get("files") or [])
    if TRANSL.search(t):
        return "translation"
    if SEC_FORM.search(t):
        return "sec_notice" if pages <= 15 else "sec_filing"
    return "periodic"



IMG_MAX_PNG = 1_500_000      # PNG גדול מזה (תמונת שער / צילום) -> JPEG איכותי; תרשים קטן נשאר PNG חד
IMG_MAX_SIDE = 4000


def save_image(src, dst_base, seen):
    """שמירת תמונה מוטמעת בחבילה. מחזיר את הנתיב שנשמר. זהה לתמונה שכבר נשמרה (לפי hash) - מחזיר את הקודמת.
    PNG כבד -> JPEG q90 (ובצד הארוך עד 4000 פיקסל): חבילה אחת שקלה 105MB בגלל צילומי שער ב-PNG של 34MB."""
    import hashlib
    from PIL import Image
    h = hashlib.md5(open(src, "rb").read()).hexdigest()
    if h in seen:
        return seen[h]
    if os.path.getsize(src) <= IMG_MAX_PNG:
        dst = dst_base + ".png"
        shutil.copy(src, dst)
    else:
        im = Image.open(src)
        if im.mode not in ("RGB", "L"):
            im = im.convert("RGB")
        if max(im.size) > IMG_MAX_SIDE:
            im.thumbnail((IMG_MAX_SIDE, IMG_MAX_SIDE), Image.LANCZOS)
        dst = dst_base + ".jpg"
        im.save(dst, "JPEG", quality=90, optimize=True)
    seen[h] = dst
    return dst


BUILDER_VERSION = 8   # להעלות בכל שיפור בזיהוי - חבילות בלי תוצאה נבנות מחדש (work_pack_queue --rebuild-old)


def build_pack(rid, files, out_dir, extra=None):
    """files: [(name, local_pdf_path)]. extra: {file_name: [pages]} - עמודים שסוכן ביקש (pack-requests): רינדור 200dpi."""
    root = os.path.join(out_dir, str(rid))
    for d in ("pages", "text", "images", "render"):
        os.makedirs(os.path.join(root, d), exist_ok=True)
    seen_imgs = {}   # תמונה זהה (רקע / לוגו שחוזר בכל עמוד) נשמרת פעם אחת
    man = {"report_id": str(rid), "files": [], "built": time.strftime("%Y-%m-%d %H:%M"), "builder": BUILDER_VERSION}
    for name, path in files:
        raw = pdftotext_pages(path, False)
        lay = pdftotext_pages(path, True)
        n = min(len(raw), len(lay))
        pages = list(zip(raw[:n], lay[:n]))
        loc = locate(pages)
        # עמודים עם תמונה גדולה בתחילת הדוח (תרשים שאין לידו כותרת שזוהתה) - מועמדי תרשים
        imgs = image_list(path, 1, n)
        big = {p for p, _, w, h in imgs if w >= 300 and h >= 200}
        for p in big:
            raw_p = pages[p - 1][0]
            # כותרת תרשים + תמונה גדולה = תרשים, גם כשבעמוד יש עוד טקסט (נספח דסק"ש במגה אור: 923 תווים, הסעיף הבא
            # מתחיל באותו עמוד). בלי כותרת - רק עמוד כמעט ריק בתחילת הדוח
            if CHART.search(both(raw_p)) or (len(raw_p.strip()) < 600 and p <= 40):
                if p not in loc["charts"]:
                    loc["charts"].append(p)
        # תרשים וקטורי (בלי תמונה ובלי כותרת): הרבה שורות קצרות עם אחוזים (דלק עמ' 7 - "דלק / פטרוליום / 1 %")
        for p in range(1, min(n, 80) + 1):
            if p not in loc["charts"] and vector_chart(pages[p - 1][0]):
                loc["charts"].append(p)
        for p in list(loc["charts"]):
            if p < n and CONT.search(both(pages[p - 1][0])) and (p + 1) not in loc["charts"]:
                loc["charts"].append(p + 1)
        # עמוד עם תמונה גדולה צמוד לעמוד תרשים - המשך התרשים (אשטרום עמ' 10: התרשים ממשיך בעמוד הבא, בלי כותרת)
        for p in sorted(big):
            if p not in loc["charts"] and ((p - 1) in loc["charts"] or (p + 1) in loc["charts"]) and len(pages[p - 1][0].strip()) < 1500:
                loc["charts"].append(p)
        loc["charts"] = sorted(set(loc["charts"]))
        with gzip.open(os.path.join(root, "text", f"{name}.txt.gz"), "wt", encoding="utf-8") as f:
            f.write("\f".join(raw[:n]))
        keep = sorted({q for v in loc.values() for p in v for q in (p - 1, p, p + 1) if 1 <= q <= n})
        for p in keep:
            with open(os.path.join(root, "pages", f"{name}_p{p}.txt"), "w", encoding="utf-8") as f:
                f.write("=== layout ===\n" + pages[p - 1][1] + "\n=== raw ===\n" + pages[p - 1][0])
        chart_imgs = []
        for p in loc["charts"]:
            with tempfile.TemporaryDirectory() as td:
                subprocess.run(["pdfimages", "-png", "-f", str(p), "-l", str(p), path, os.path.join(td, "i")],
                               capture_output=True, timeout=120)
                k = 0
                for fn in sorted(os.listdir(td)):
                    fp = os.path.join(td, fn)
                    try:
                        from PIL import Image
                        w, h = Image.open(fp).size
                    except Exception:
                        continue
                    if w >= 300 and h >= 150:
                        dst = save_image(fp, os.path.join(root, "images", f"{name}_p{p}_{k}"), seen_imgs)
                        chart_imgs.append({"page": p, "file": os.path.basename(dst), "size": [w, h]})
                        k += 1
            subprocess.run(["pdftoppm", "-f", str(p), "-l", str(p), "-r", "200", "-jpeg", "-jpegopt", "quality=88", "-singlefile", path,
                            os.path.join(root, "render", f"{name}_p{p}")], capture_output=True, timeout=180)
        # עמודי הטבלאות (תקנה 11/12, ביאור המוחזקות, Exhibit 21) גם כתמונה, ברזולוציה קלה: לקריאה כשעמודות הטקסט מתפרקות
        # (נספח דלק / דוראל), ולתמונה לשאלות. הסוכן לא יכול להוריד את ה-PDF בעצמו (מאיה חוסמת - 403)
        table_pages = sorted({int(p) for k in ("reg11", "reg12", "fs_notes", "exhibit21", "tables") for p in loc.get(k) or []}
                             - set(loc["charts"]))[:60]
        for p in table_pages:
            subprocess.run(["pdftoppm", "-f", str(p), "-l", str(p), "-r", "110", "-jpeg", "-jpegopt", "quality=80", "-singlefile", path,
                            os.path.join(root, "render", f"{name}_p{p}")], capture_output=True, timeout=180)
        req = sorted({int(p) for p in (extra or {}).get(name) or [] if 1 <= int(p) <= n})
        for p in req:
            subprocess.run(["pdftoppm", "-f", str(p), "-l", str(p), "-r", "200", "-jpeg", "-jpegopt", "quality=88", "-singlefile", path,
                            os.path.join(root, "render", f"{name}_p{p}")], capture_output=True, timeout=180)
            with open(os.path.join(root, "pages", f"{name}_p{p}.txt"), "w", encoding="utf-8") as f:
                f.write("=== layout ===\n" + pages[p - 1][1] + "\n=== raw ===\n" + pages[p - 1][0])
        man["files"].append({"name": name, "pages": n, "sections": loc, "chart_images": chart_imgs,
                             "table_renders": table_pages, "requested_pages": req,
                             "text_chars": sum(len(r.strip()) for r in raw[:n])})
    with open(os.path.join(root, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(man, f, ensure_ascii=False, indent=1)
    return man


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--reports", required=True, help="report_id מופרדים בפסיק (מתוך selection_plan.json)")
    ap.add_argument("--plan", default="selection_plan.json")
    ap.add_argument("--pdf-dir", default="", help="קבצי PDF קיימים בשם <report_id>_<i>.pdf (בלי הורדה)")
    ap.add_argument("--out", default="packs")
    a = ap.parse_args()
    plan = json.load(open(a.plan, encoding="utf-8"))
    by = {str(e["snapshot"]["report_id"]): (cid, e) for cid, e in plan.items() if e.get("snapshot")}
    for rid in [r.strip() for r in a.reports.split(",") if r.strip()]:
        cid, e = by[rid]
        amend = e["snapshot"].get("amendments") or []
        urls = [e["snapshot"]["pdf_url"]] + list(e["snapshot"].get("extra_pdfs") or []) + [x["pdf_url"] for x in amend]
        files = []
        with tempfile.TemporaryDirectory() as td:
            for i, u in enumerate(urls):
                local = os.path.join(a.pdf_dir, f"{rid}_{i}.pdf") if a.pdf_dir else ""
                if not (local and os.path.exists(local)):
                    import maya_reports_client as mrc
                    local = os.path.join(td, f"{rid}_{i}.pdf")
                    for attempt in range(6):
                        try:
                            open(local, "wb").write(mrc.download_report_file(u))
                            break
                        except Exception as ex:
                            print(f"  {rid}_{i}: הורדה נכשלה ({type(ex).__name__}) - ניסיון {attempt + 1}")
                            time.sleep(15)
                    time.sleep(5)
                files.append((f"{rid}_{i}", local))
            man = build_pack(rid, [(n, p) for n, p in files if os.path.exists(p)], a.out)
        man.update({"title": e["snapshot"].get("title"), "doc_kind": doc_kind(e["snapshot"].get("title"), man)})
        man.update({"company": e.get("company_name"), "company_id": cid, "publish_date": e["snapshot"].get("publish_date"),
                    "urls": {f"{rid}_{i}": u for i, u in enumerate(urls)}})
        if amend:   # תיקון דוח שפורסם אחרי הדוח: הקבצים האחרונים בחבילה (הסוכן - "יום הדוח" = מועד פרסום התיקון)
            k0 = len(urls) - len(amend)
            man["amendments"] = [dict(x, file=f"{rid}_{k0 + j}") for j, x in enumerate(amend)]
        with open(os.path.join(a.out, rid, "manifest.json"), "w", encoding="utf-8") as f:
            json.dump(man, f, ensure_ascii=False, indent=1)
        print(f"PACK {rid} ({e.get('company_name')}): " + "; ".join(
            f"{f['name']} {f['pages']}p " + " ".join(f"{k}={v}" for k, v in f["sections"].items() if v) +
            f" images={len(f['chart_images'])}" for f in man["files"]))


if __name__ == "__main__":
    sys.exit(main())

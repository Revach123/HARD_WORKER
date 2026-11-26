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
    return BIDI.sub("", unicodedata.normalize("NFKC", t or ""))


def both(t):
    """טקסט + השורות בהיפוך (עברית בסדר חזותי בחלק מהקבצים)."""
    return t + "\n" + "\n".join(line[::-1] for line in t.splitlines())


def pdftotext_pages(path, layout):
    args = ["pdftotext", "-q"] + (["-layout"] if layout else []) + [path, "-"]
    out = subprocess.run(args, capture_output=True, timeout=600).stdout.decode("utf-8", "replace")
    return [norm(p) for p in out.split("\f")]


ENT = re.compile(r'בע"?מ|בע״מ|שותפות|\bLtd\b|\bLimited\b|\bLLC\b|\bInc\b|\bB\.?V\b|\bGmbH\b|\bS\.?A\b|\bL\.?P\b|'
                 r'\bS\.?R\.?L\b|\bsp\.? z|\bPLC\b|\bCorp|\bAG\b|\behf\b|\bd\.o\.o', re.I)
REG11 = re.compile(r"תקנה\s*:?\s*11\b|11\s*:?\s*תקנה|רשימת\s+(?:ה)?השקעות\s+בחברות|השקעות\s+בחברות[- ]בת\s+ובחברות\s+כלולות", re.I)
REG12 = re.compile(r"תקנה\s*:?\s*12\b|12\s*:?\s*תקנה|שינויים\s+בהשקעות\s+בחברות", re.I)
REG26 = re.compile(r"תקנה\s*:?\s*26\b|26\s*:?\s*תקנה|הדירקטורים\s+של\s+התאגיד", re.I)
# "תרשים חברות מוחזקות עיקריות" (לאומי), "תרשים ההחזקות", "תרשים מבנה" - כל "תרשים" שאחריו מילה על החזקות / חברות / הקבוצה
CHART = re.compile(r"(?:תרשים|מבנה)\s+(?:ה)?(?:[אה]חזקות|החזקות|אחזקות|הקבוצה|התאגיד|חברות|השקעות)|תרשים\s+מבנה|"
                   r"Organi[sz]ational\s+(?:Structure|chart)|Holding\s+structure|Group\s+structure|Structure\s+chart", re.I)
NOTE = re.compile(r"חברות\s+מאוחדות|חברות\s+בנות\s+מהותיות|חברות\s+מוחזקות|ישויות\s+הקבוצה|השקע(?:ה|ות)\s+בחברות\s+(?:מוחזקות|כלולות|בנות)|"
                  r"חברות\s+כלולות|פירוט\s+החברות|Principal\s+subsidiaries|Significant\s+subsidiaries|Investments?\s+in\s+(?:subsidiaries|investees|associates)", re.I)
EXH = re.compile(r"Exhibit\s+(?:21|8)(?:\.1)?\b[^\n]{0,80}|List\s+of\s+(?:Significant\s+)?Subsidiaries|Subsidiaries\s+of\s+the\s+(?:Company|Registrant)", re.I)
PCT = re.compile(r"\d[\d.,]*\s*%|%\s*\d")


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


def locate(pages):
    """pages: [(raw, layout)] -> {section: [page numbers 1-based]}."""
    n = len(pages)
    sec = {k: set() for k in ("reg11", "reg12", "reg26", "charts", "fs_notes", "exhibit21")}
    for i, (raw, lay) in enumerate(pages):
        t = both(raw + "\n" + lay)
        ent = len(ENT.findall(t))
        pct = len(PCT.findall(t))
        head = "\n".join(raw.strip().splitlines()[:12])            # כותרת העמוד
        headb = both(head)
        if REG11.search(t) and (ent >= 3 or pct >= 3):
            sec["reg11"].add(i)
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
        if EXH.search(t) and ent >= 8:
            sec["exhibit21"].add(i)
    # המשך טבלה: עמוד אחרי תקנה 11 / ביאור / exhibit עם הרבה ישויות - עד 4 עמודים
    for k in ("reg11", "fs_notes", "exhibit21"):
        for i in sorted(sec[k]):
            for j in range(i + 1, min(n, i + 5)):
                raw, lay = pages[j]
                t = both(raw + "\n" + lay)
                if len(ENT.findall(t)) >= 4 and not (k == "reg11" and REG12.search(both("\n".join(raw.splitlines()[:8])))):
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
TRANSL = re.compile(r"translation|תרגום", re.I)


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


def build_pack(rid, files, out_dir):
    """files: [(name, local_pdf_path)]."""
    root = os.path.join(out_dir, str(rid))
    for d in ("pages", "text", "images", "render"):
        os.makedirs(os.path.join(root, d), exist_ok=True)
    seen_imgs = {}   # תמונה זהה (רקע / לוגו שחוזר בכל עמוד) נשמרת פעם אחת
    man = {"report_id": str(rid), "files": [], "built": time.strftime("%Y-%m-%d %H:%M")}
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
        man["files"].append({"name": name, "pages": n, "sections": loc, "chart_images": chart_imgs,
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
        urls = [e["snapshot"]["pdf_url"]] + list(e["snapshot"].get("extra_pdfs") or [])
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
        with open(os.path.join(a.out, rid, "manifest.json"), "w", encoding="utf-8") as f:
            json.dump(man, f, ensure_ascii=False, indent=1)
        print(f"PACK {rid} ({e.get('company_name')}): " + "; ".join(
            f"{f['name']} {f['pages']}p " + " ".join(f"{k}={v}" for k, v in f["sections"].items() if v) +
            f" images={len(f['chart_images'])}" for f in man["files"]))


if __name__ == "__main__":
    sys.exit(main())

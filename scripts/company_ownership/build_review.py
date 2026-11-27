# -*- coding: utf-8 -*-
"""
build_review.py - רשימת השינויים שממתינים לאישור המנהל (בלי AI).

לכל תוצאה של סוכן החילוץ (results/<report_id>.json בענף agent-results) - השוואה למה שהאתר מציג היום
(data/holdings/extracted_subsidiaries.json):
  verified  - שינויים שהסוכן אימת בלי ספק: חברה חדשה / חברה שתוסר / אחוז שהשתנה / חברת אם. אישור בלחיצה אחת לכל חברה.
  questions - שאלות שהסוכן ביקש להכריע בהן: תמונת האזור בדוח, הצעה, אפשרויות נוספות.
הפלט: <out>/pending.json + תמונות השאלות <out>/<report_id>/<qid>.jpg (מוצג בדשבורד הפענוח, extraction-dashboard.html).

הרצה:  python build_review.py --results ../../ar/results --site ../../revach/data/holdings/extracted_subsidiaries.json
                              --maya ../../revach/data/registry/maya_companies_full.json --out ../../revach/data/holdings/review
"""
import argparse
import datetime
import glob
import json
import os
import re
import shutil
import sys

LEG = r"בע\"?מ|בע״מ|\bltd\.?|\blimited\b|\binc\.?|\bllc\b|\bb\.?v\.?|\bgmbh\b|\bs\.?a\.?|\bplc\b|\bcorp\.?|\bl\.?p\.?|\bag\b|\bco\.?\b"


def tkey(n):
    """מפתח שם שלא תלוי בסדר המילים, בפיסוק, בצורת ההתאגדות ובכינוי המוגדר בסוגריים ("סטור")."""
    n = str(n or "").lower().replace("״", '"').replace("׳", "'")
    n = re.sub(r'\(\s*["“”][^()]{0,40}["“”]?\s*\)', " ", n)
    n = re.sub(LEG, " ", n)
    n = re.sub(r"[\"'`.,()\[\]\-–/&]", " ", n)
    return " ".join(sorted(t for t in n.split() if t and t not in ("and", "ו")))


def norm(n):
    """שם מנורמל בלי לוותר על הצורה המשפטית (להבחנה בין Ltd. ל-Inc.)."""
    n = str(n or "").lower().replace("״", '"').replace("׳", "'")
    return " ".join(re.sub(r"[\"'`.,()\[\]\-–/&]", " ", n).split())


DESC = re.compile(r"חברה|רשומה|רשום|קפריסא|יוונ|הולנד|אמריקא|אנגל|גרמנ|סינ|מיזם|שותפות|בקפריסין|ביוון|בהולנד|בסין|"
                  r"בארה\"?ב|registered|company|incorporated|partnership|\b(?:jv|joint venture)\b", re.I)
LOOSE_LEGAL = {"בע", "מ", "בעמ", "ltd", "inc", "llc", "limited", "bv", "lp", "שותפות", "מוגבלת", "sa",
               "co", "corp", "corporation", "gmbh", "plc", "pty", "as", "ag", "nv"}


def loose(name):
    """כמו review_apply._loose: בלי תיאור בסוגריים; סוגריים מבחינים (שנה / שם) - אין מפתח רופף."""
    n = str(name or "").lower()
    for par in re.findall(r"\([^()]*\)", n):
        if not DESC.search(par):
            return ""
        n = n.replace(par, " ")
    n = re.sub(r"\b(?:b\.?\s?v|l\.?\s?p|s\.?\s?a|n\.?\s?v|l\.?\s?l\.?\s?c)\b\.?", " ", n)   # אות בודדת אחרת נשארת (Solar A ≠ Solar B)
    toks = [t for t in re.findall(r"[א-תa-z0-9]+", n) if t not in LOOSE_LEGAL]
    return " ".join(sorted(toks)) if len(toks) >= 2 else ""


def cid_to_hp(maya_path):
    try:
        d = json.load(open(maya_path, encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {str(c["companyId"]): str(c["corporateNo"]).lstrip("0")
            for c in (d if isinstance(d, list) else d.get("companies") or []) if c.get("companyId") and c.get("corporateNo")}


def validate(script, res_path, pack_dir):
    """validate_result.py על התוצאה מול החבילה: {"ok", "errors"}. None - אין חבילה לבדיקה."""
    import contextlib
    import importlib.util
    import io
    if not os.path.exists(os.path.join(pack_dir, "manifest.json")):
        return None
    spec = importlib.util.spec_from_file_location("validate_result", script)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            rc = mod.main(res_path, pack_dir)
    except Exception as e:  # noqa: BLE001 - בדיקה שקרסה = לא עברה
        return {"ok": False, "errors": [f"הבדיקה נכשלה: {type(e).__name__}: {e}"[:200]]}
    errs = [x[len("שגיאה: "):][:200] for x in buf.getvalue().splitlines() if x.startswith("שגיאה:")]
    return {"ok": rc == 0, "errors": errs[:6], "n_errors": len(errs)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--results", required=True, help="תיקיית results מענף agent-results")
    ap.add_argument("--site", required=True)
    ap.add_argument("--maya", default="")
    ap.add_argument("--packs", default="", help="תיקיית packs (לח.פ / שם החברה מה-manifest)")
    ap.add_argument("--out", required=True)
    ap.add_argument("--redo-dir", default="", help="redo/ מענף agent-results - דוח שממתין להרצה חוזרת לא נכנס לרשימה")
    ap.add_argument("--validator", default="", help="validate_result.py (מאגר Revach) - תוצאה שלא עברה: בלי כניסה אוטומטית")
    ap.add_argument("--registry-fixes", default="registry_fixes.json",
                    help="קישורים לרשם החברות שנמצאו חשודים (סוגריים סותרים / התאמה מקורבת) - שאלות לאישור")
    a = ap.parse_args()
    site = json.load(open(a.site, encoding="utf-8")).get("parents", {})
    c2h = cid_to_hp(a.maya) if a.maya else {}
    os.makedirs(a.out, exist_ok=True)
    reports = []
    redo = {x[:-5] for x in os.listdir(a.redo_dir) if x.endswith(".json")} if a.redo_dir and os.path.isdir(a.redo_dir) else set()
    for f in sorted(glob.glob(os.path.join(a.results, "*.json"))):
        d = json.load(open(f, encoding="utf-8"))
        rid = str(d.get("report_id") or os.path.basename(f)[:-5])
        if rid in redo:
            print(f"  {rid}: ממתין להרצה חוזרת (redo) - לא ברשימה")
            continue
        man = {}
        if a.packs and os.path.exists(os.path.join(a.packs, rid, "manifest.json")):
            man = json.load(open(os.path.join(a.packs, rid, "manifest.json"), encoding="utf-8"))
        hp = str(man.get("hp") or c2h.get(str(man.get("company_id") or "")) or d.get("hp") or "").lstrip("0") or None
        cur = (site.get(hp) or {}).get("children", []) if hp else []
        # מפתח -> כל החברות באתר עם המפתח הזה. tkey מוריד צורה משפטית, ולכן "Actimize Ltd." ו-"Actimize Inc." (שתי חברות
        # שונות) מקבלות אותו מפתח - שומרים את כולן, לא רק את הראשונה, אחרת השנייה נראית "חסרה בדוח" ומסומנת להסרה.
        cur_by = {}
        for ch in cur:
            for n in (ch.get("name"), ch.get("raw")):
                if n:
                    lst = cur_by.setdefault(tkey(n), [])
                    if not any(x is ch for x in lst):
                        lst.append(ch)
        holds = d.get("holdings") or []
        seen = set()
        changes = []
        for h in holds:
            names = [x for x in [h.get("name")] + list(h.get("alt_names") or []) if x]
            keys = {tkey(x) for x in names}
            cands = [c for k in keys for c in cur_by.get(k, [])]
            for c in cands:          # כל חברה באתר שחולקת מפתח עם החזקה בדוח - לא תוסר
                seen.add(id(c))
            exact = {norm(x) for x in names}
            ch = next((c for c in cands if norm(c.get("name")) in exact or norm(c.get("raw")) in exact), None) \
                or (cands[0] if cands else None)
            if ch is None:               # תיאור בסוגריים באתר ("ICH Europe Ltd. (חברה קפריסאית)") - אותה חברה, לא חדשה
                lk = {loose(x) for x in names} - {""}
                lc = [c for c in cur if lk & {loose(c.get("name")), loose(c.get("raw"))}]
                if len(lc) == 1:
                    ch = lc[0]
                    seen.add(id(ch))
            item = {"name": h.get("name"), "type": h.get("type"), "parent": h.get("parent"), "parent_basis": h.get("parent_basis"),
                    "pct": h.get("pct_capital"), "page": h.get("page"), "section": h.get("section"), "evidence": h.get("evidence"),
                    "verified": h.get("verified", True)}
            if ch is None:
                changes.append({**item, "change": "add"})
            else:
                old = ch.get("vot")
                new = h.get("pct_capital")
                if isinstance(old, (int, float)) and isinstance(new, (int, float)) and abs(old - new) > 0.01:
                    changes.append({**item, "change": "pct", "old_pct": old})
        # כפילות בין חברות שנמצאו (שתיהן חולקות מפתח עם החזקה): שם מנורמל זהה / מפתח רופף זהה, ובלי ח.פ שונים - אחת נשארת
        # (זו שהשם שלה כמו בדוח), השאר הסרה עם סיבה. "Actimize Ltd." / "Actimize Inc." - שם מנורמל שונה ואין מפתח רופף: לא כפילות
        agent_norms = {norm(x) for h in holds for x in [h.get("name")] + list(h.get("alt_names") or []) if x}
        groups = {}
        for ch in cur:
            if id(ch) in seen:
                for k in {("n", norm(ch.get("name"))), ("l", loose(ch.get("name")))}:
                    if k[1]:
                        groups.setdefault(k, []).append(ch)
        dropped = set()
        for k, g in groups.items():
            g = [c for c in g if id(c) not in dropped]
            if len(g) < 2 or len({str(c.get("hp")) for c in g if c.get("hp")}) > 1:
                continue
            keep = next((c for c in g if norm(c.get("name")) in agent_norms or norm(c.get("raw")) in agent_norms), g[0])
            for c in g:
                if c is not keep:
                    dropped.add(id(c))
                    changes.append({"name": c.get("name"), "change": "remove", "old_pct": c.get("vot"), "verified": True,
                                    "reason": f"כפילות של {keep.get('name')} (אותה חברה פעמיים בנתונים הקודמים)"})
        # כפילות בנתונים הישנים: חברה באתר שאינה בדוח, אבל זהה בשמה (שם מנורמל / מפתח רופף - לא רק tkey, שמאחד Ltd. ו-Inc.)
        # לחברה אחרת באתר שהסוכן כן מצא - כפילות טהורה, הסרה עם סיבה (נכנסת אוטומטית)
        found_keys = {}
        for ch in cur:
            if id(ch) in seen:
                for x in (ch.get("name"), ch.get("raw")):
                    if x:
                        found_keys.setdefault(("n", norm(x)), ch.get("name"))
                        if loose(x):
                            found_keys.setdefault(("l", loose(x)), ch.get("name"))
        for ch in cur:
            if id(ch) in seen:
                continue
            dup = next((found_keys[k] for x in (ch.get("name"), ch.get("raw")) if x
                        for k in (("n", norm(x)), ("l", loose(x))) if k in found_keys and k[1]), None)
            if dup:
                seen.add(id(ch))
                changes.append({"name": ch.get("name"), "change": "remove", "old_pct": ch.get("vot"), "verified": True,
                                "reason": f"כפילות של {dup} (אותה חברה פעמיים בנתונים הקודמים)"})
        for ch in cur:
            if id(ch) not in seen:
                changes.append({"name": ch.get("name"), "change": "remove", "old_pct": ch.get("vot"),
                                "reason": next((x.get("reason") for x in d.get("former") or []
                                                if tkey(x.get("name")) in {tkey(ch.get("name")), tkey(ch.get("raw"))}), None)})
        qs = []
        for q in d.get("questions") or []:
            q = dict(q)
            img = q.get("image")
            src = os.path.join(a.results, rid, os.path.basename(img)) if img else ""
            if src and os.path.exists(src):
                os.makedirs(os.path.join(a.out, rid), exist_ok=True)
                shutil.copy(src, os.path.join(a.out, rid, os.path.basename(img)))
                q["image"] = f"{rid}/{os.path.basename(img)}"
            else:
                q["image"] = None
            q["item_id"] = f"{rid}:{q.get('id')}"
            qs.append(q)
        # חברת ביניים: חברה שמחזיקה בחברה בת היא בעצמה חברה בת. אם החזקה נכנסת וחברת האם שלה (מהדוח) לא באתר ולא נכנסת -
        # מוסיפים גם אותה מהפלט של הסוכן (גם אם סומנה לא-מאומתת בגלל שאלה על פרט אחר), אחרת הקישור בעץ נשבר (סאמיט: 110 חברות
        # תחת "Summit Properties Ltd." שחיכתה לשאלה)
        by_name = {}
        for h in holds:
            for x in [h.get("name")] + list(h.get("alt_names") or []):
                if x:
                    by_name.setdefault(tkey(x), h)
        on_site = set(cur_by) | {loose(c.get("name")) for c in cur} - {""}
        entering = {tkey(c["name"]) for c in changes if c["change"] in ("add", "pct") and (c.get("verified", True))}
        # כל החזקה שנכנסת או שכבר באתר (גם בלי שינוי) - חברת האם שלה צריכה להיות בעץ
        site_keys = set(cur_by)
        queue = [c.get("parent") for c in changes if c["change"] in ("add", "pct") and c.get("verified", True)] + \
                [h.get("parent") for h in holds if {tkey(x) for x in [h.get("name")] + list(h.get("alt_names") or []) if x} & site_keys]
        while queue:
            par = queue.pop()
            if not par or par == "REPORTING_COMPANY":
                continue
            k = tkey(par)
            if k in on_site or loose(par) in on_site or k in entering:
                continue
            h = by_name.get(k)
            if not h:
                continue
            entering.add(k)
            changes.append({"name": h.get("name"), "type": h.get("type"), "parent": h.get("parent"),
                            "parent_basis": h.get("parent_basis"), "pct": h.get("pct_capital"), "page": h.get("page"),
                            "section": h.get("section"), "evidence": h.get("evidence"), "verified": True,
                            "change": "add", "as_parent": True})
            queue.append(h.get("parent"))
        val = validate(a.validator, f, os.path.join(a.packs, rid)) if a.validator and a.packs else None
        auto, gated = [], []
        for c in changes:
            if c["change"] != "remove" and not c.get("verified", True):
                continue                                   # unverified - נכנס רק אחרי תשובה לשאלה
            if val is not None and not val["ok"]:
                gated.append({**c, "gate": "validation"})  # התוצאה לא עברה את validate_result.py
            elif c["change"] == "remove" and not c.get("reason"):
                gated.append({**c, "gate": "no_reason"})   # הסרה בלי סיבה בדוח (מיזוג / מכירה / חיסול) - רק באישור
            else:
                auto.append(c)
        reports.append({"report_id": rid, "company": d.get("company") or man.get("company"), "hp": hp,
                        "report_date": d.get("report_date"), "publish_date": man.get("publish_date"),
                        "holdings": len(holds), "current": len(cur), "validation": val,
                        "verified_changes": auto, "gated_changes": gated,
                        "unverified": [c for c in changes if not c.get("verified", True) and c["change"] != "remove"],
                        "questions": qs, "disagreements": d.get("disagreements") or [],
                        "item_id": f"{rid}:company"})
    if a.registry_fixes and os.path.exists(a.registry_fixes):
        regs = json.load(open(a.registry_fixes, encoding="utf-8"))
        for q in regs:
            q["item_id"] = f"registry:{q['id']}"
        if regs:
            reports.append({"report_id": "registry", "company": "קישורים לרשם החברות", "hp": None, "holdings": 0, "current": 0,
                            "verified_changes": [], "gated_changes": [], "unverified": [], "questions": regs, "disagreements": [],
                            "item_id": "registry:company"})
    out = {"built": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="minutes"), "reports": reports}
    with open(os.path.join(a.out, "pending.json"), "w", encoding="utf-8") as fo:
        json.dump(out, fo, ensure_ascii=False, indent=1)
    nq = sum(len(r["questions"]) for r in reports)
    nv = sum(len(r["verified_changes"]) for r in reports)
    ng = sum(len(r.get("gated_changes") or []) for r in reports)
    nf = sum(1 for r in reports if r.get("validation") and not r["validation"]["ok"])
    print(f"REVIEW {len(reports)} דוחות: {nv} שינויים אוטומטיים, {ng} ממתינים לאישור ({nf} דוחות לא עברו בדיקה), {nq} שאלות")
    # מעקב: כפילויות שנשארו באתר (שם מנורמל / מפתח רופף זהה, בלי ח.פ שונים) - יורדות כשסוכן מגיע לחברה
    dup = 0
    for p in site.values():
        g = {}
        for k in p.get("children", []):
            for key in {("n", norm(k.get("name"))), ("l", loose(k.get("name")))}:
                if key[1]:
                    g.setdefault(key, []).append(k)
        dup += sum(1 for v in g.values() if len(v) > 1 and len({str(x.get("hp")) for x in v if x.get("hp")}) <= 1)
    ndup = sum(1 for r in reports for c in r["verified_changes"] if "כפילות" in str(c.get("reason")))
    print(f"DUPLICATES באתר: {dup} קבוצות; מוסרות עכשיו: {ndup}")


if __name__ == "__main__":
    sys.exit(main())

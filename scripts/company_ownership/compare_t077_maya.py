# -*- coding: utf-8 -*-
"""
compare_t077_maya.py — השוואה בין שני מקורות בעלי העניין: ת077 ומאיה.

מטרה: להחליט אם ת077 יכול להחליף את מאיה כמקור לאחוזי החזקה, ע"י מדידה
מדויקת של מה שכל צד מכסה ומה חסר בו — לפני שמורידים את מאיה.

זהו סקריפט בדיקה מקומי בלבד. מאיה חסומה מסביבות ענן/סנדבוקס, ולכן יש
להריצו מהמחשב, עם אותם משתני סביבה כמו הרצה רגילה (TASE_APIKEY וכו').

הפלט: private/compare-t077-maya.csv — שורה לכל (חברה, מחזיק), עם האחוז
משני המקורות זה לצד זה, ועמודת אבחון. סיכום מודפס למסך.

מריץ:  python compare_t077_maya.py
       (או)  python compare_t077_maya.py --limit 50   # רק 50 חברות, לבדיקה מהירה
"""
import argparse
import csv
import json
import os
import sys

import pandas as pd

import maya_core as mc

# t077_core יושב תחת scripts/company_ownership/. מוסיפים לנתיב אם צריך.
_HERE = os.path.dirname(os.path.abspath(__file__))
for cand in (_HERE, os.path.join(_HERE, "scripts", "company_ownership"),
             os.path.join(_HERE, "..", "company_ownership")):
    if os.path.isfile(os.path.join(cand, "t077_core.py")):
        sys.path.insert(0, cand)
        break
import t077_core as tc   # noqa: E402


def _norm(name):
    """נרמול שם להשוואה — אותו נרמול עברי כמו במפת ת077."""
    return tc.norm_name(name)


# ── צד ת077: קורא את הקצוות המוכנים (data/holdings/t077-edges.json) ──────────
def load_t077_side():
    """מחזיר {(child_hp, holder_key): {"name","vot","cap","id","kind"}} מת077.

    child_hp = ח.פ החברה המדווחת; holder_key = שם מנורמל של המחזיק.
    משתמש בקובץ הקצוות שכבר נבנה — לא סורק דוחות מחדש.
    """
    path = os.path.join(tc.PUB_DIR, "t077-edges.json")
    try:
        with open(path, encoding="utf-8") as f:
            comps = (json.load(f) or {}).get("companies") or {}
    except FileNotFoundError:
        print(f"שגיאה: {path} לא נמצא. הרץ קודם את t077_latest.py לבניית הקצוות.")
        sys.exit(1)
    out = {}
    for chp, rec in comps.items():
        for h in rec.get("holders") or []:
            key = _norm(h.get("name"))
            if not key:
                continue
            out[(str(chp), key)] = {
                "name": h.get("name"), "vot": h.get("vot"),
                "cap": h.get("cap"), "id": h.get("hp") or h.get("pid"),
                "kind": "company" if h.get("hp") else "person",
            }
    return out


# ── צד מאיה: מריץ את אותן קריאות כמו build_all, אבל רק אוסף מחזיקים ─────────
def load_maya_side(limit=None):
    """מחזיר {(child_hp, holder_key): {"name","vot","cap","cat"}} ממאיה.

    מריץ את שכבת הרשת של maya_core (interested-parties לכל חברה), בדיוק
    כמו הצינור הרגיל, כדי שההשוואה תהיה מול אותם נתונים.
    """
    companies = mc.fetch_company_list()
    if limit:
        companies = companies[:limit]
    cid2hp = {c["companyId"]: c.get("corporateId") for c in companies}
    s = mc.new_session()
    out = {}
    from concurrent.futures import ThreadPoolExecutor, as_completed
    with ThreadPoolExecutor(max_workers=mc.WORKERS) as ex:
        futs = {ex.submit(mc.build_company, c["companyId"], c["companyName"], s): c
                for c in companies}
        done = 0
        for fut in as_completed(futs):
            done += 1
            if done % 50 == 0:
                print(f"  מאיה ...{done}/{len(companies)}")
            try:
                hdf, _summ, _b, _c, _cc = fut.result()
            except Exception as e:
                c = futs[fut]
                print(f"  {c['companyName']} נכשל: {e}")
                continue
            if hdf.empty:
                continue
            for r in hdf.itertuples(index=False):
                chp = cid2hp.get(r.companyId)
                if not chp:
                    continue
                key = _norm(r.holderName)
                if not key:
                    continue
                vot = pd.to_numeric(getattr(r, "voting", None), errors="coerce")
                cap = pd.to_numeric(getattr(r, "capital", None), errors="coerce")
                rec = out.setdefault((str(chp), key),
                                     {"name": r.holderName, "vot": 0.0,
                                      "cap": 0.0, "cat": r.holderCategory})
                # מאיה מדווחת נייר/חשבון בנפרד — מסכמים לכל מחזיק
                rec["vot"] += float(vot) if pd.notna(vot) else 0.0
                rec["cap"] += float(cap) if pd.notna(cap) else 0.0
    for rec in out.values():
        rec["vot"] = round(rec["vot"], 2)
        rec["cap"] = round(rec["cap"], 2)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None,
                    help="הגבל למספר חברות (לבדיקה מהירה)")
    ap.add_argument("--out", default=os.path.join(tc.RAW_DIR, "compare-t077-maya.csv"))
    args = ap.parse_args()

    print("טוען צד ת077 (מהקצוות המוכנים)...")
    t = load_t077_side()
    print(f"  {len(t):,} זוגות (חברה, מחזיק) בת077")

    print("טוען צד מאיה (קריאות רשת)...")
    m = load_maya_side(args.limit)
    print(f"  {len(m):,} זוגות (חברה, מחזיק) במאיה")

    # ── התאמה בתוך כל חברה, בשלושה מעברים ─────────────────────────────────
    # השמות בין המקורות שונים ("דוד פורר" מול "פורר דוד", "הראל-ק.גמל" מול
    # "הראל השקעות בביטוח..."), ולכן התאמה לפי שם בלבד מנפחת גם את "רק
    # בת077" וגם את "רק במאיה". מתאימים לכן בתוך אותה חברה:
    #   מעבר 1 — שם מנורמל זהה (הוודאי).
    #   מעבר 2 — אחוז הצבעה זהה (עד 0.5%) בין מה שנותר. שני מחזיקים באותה
    #            חברה עם אותו אחוז מדויק הם כמעט תמיד אותו גורם בשני ניסוחים.
    #   נותר   — פער אמיתי: קיים בצד אחד בלבד, בלי תאום שם ובלי תאום אחוז.
    from collections import defaultdict
    t_by_co, m_by_co = defaultdict(list), defaultdict(list)
    for (chp, key), v in t.items():
        t_by_co[chp].append((key, v))
    for (chp, key), v in m.items():
        m_by_co[chp].append((key, v))

    rows = []
    both = only_t = only_m = vot_gap = pct_matched = 0

    def _emit(chp, te, me, diag):
        rows.append({
            "companyHp": chp,
            "holderName": (te or me).get("name"),
            "t077_vot": te.get("vot") if te else None,
            "t077_cap": te.get("cap") if te else None,
            "t077_kind": te.get("kind") if te else None,
            "maya_vot": me.get("vot") if me else None,
            "maya_cap": me.get("cap") if me else None,
            "maya_cat": me.get("cat") if me else None,
            "diag": diag,
        })

    for chp in set(t_by_co) | set(m_by_co):
        t_list = t_by_co.get(chp, [])[:]
        m_list = m_by_co.get(chp, [])[:]
        m_used = [False] * len(m_list)

        # מעבר 1 — התאמת שם מנורמל
        t_left = []
        for tkey, te in t_list:
            hit = next((i for i, (mkey, _me) in enumerate(m_list)
                        if not m_used[i] and mkey == tkey), None)
            if hit is not None:
                m_used[hit] = True
                me = m_list[hit][1]
                tv, mv = te.get("vot"), me.get("vot")
                if tv is not None and mv is not None and abs(tv - mv) > 0.5:
                    vot_gap += 1
                    _emit(chp, te, me, "פער אחוז")
                else:
                    both += 1
                    _emit(chp, te, me, "שניהם")
            else:
                t_left.append((tkey, te))

        # מעבר 2 — התאמת אחוז הצבעה מדויק בין מה שנותר
        still_t = []
        for tkey, te in t_left:
            tv = te.get("vot")
            hit = None
            if tv is not None:
                hit = next((i for i, (mkey, me) in enumerate(m_list)
                            if not m_used[i] and me.get("vot") is not None
                            and abs(me["vot"] - tv) <= 0.5), None)
            if hit is not None:
                m_used[hit] = True
                pct_matched += 1
                _emit(chp, te, m_list[hit][1], "התאמת אחוז")
            else:
                still_t.append((tkey, te))

        # נותר בצד ת077 — פער אמיתי
        for _tkey, te in still_t:
            only_t += 1
            _emit(chp, te, None, "רק ת077")
        # נותר בצד מאיה — פער אמיתי
        for i, (_mkey, me) in enumerate(m_list):
            if not m_used[i]:
                only_m += 1
                _emit(chp, None, me, "רק מאיה")

    order = {"שניהם": 0, "התאמת אחוז": 1, "פער אחוז": 2,
             "רק ת077": 3, "רק מאיה": 4}
    rows.sort(key=lambda r: (order.get(r["diag"], 9), str(r["companyHp"])))
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    cols = ["companyHp", "holderName", "t077_vot", "t077_cap", "t077_kind",
            "maya_vot", "maya_cap", "maya_cat", "diag"]
    with open(args.out, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows(rows)

    total = both + pct_matched + vot_gap + only_t + only_m
    same = both + pct_matched
    print("\n" + "=" * 56)
    print(f"סה\"כ זוגות (חברה, מחזיק): {total:,}")
    print(f"  התאמת שם:          {both:,}")
    print(f"  התאמת אחוז:        {pct_matched:,}  <- אותו גורם, שם שונה, אחוז זהה")
    print(f"  --> בשני המקורות:  {same:,}  (סה\"כ מותאמים)")
    print(f"  פער אחוז >0.5:     {vot_gap:,}  <- שם זהה אך אחוז שונה, לבדוק")
    print(f"  רק בת077:          {only_t:,}  <- מה שמאיה מפספסת (רווח נקי)")
    print(f"  רק במאיה:          {only_m:,}  <- הפער האמיתי: מה שתאבד בלי מאיה")
    print("=" * 56)
    if same:
        cov = 100.0 * same / (same + only_m) if (same + only_m) else 0
        print(f"\nכיסוי ת077 מול מאיה: {cov:.1f}% ממה שיש למאיה, ת077 מכסה.")
    print(f"פלט מלא: {args.out}")
    print("סנן diag='רק מאיה' לפער האמיתי · diag='התאמת אחוז' לאימות ההתאמות.")


if __name__ == "__main__":
    main()

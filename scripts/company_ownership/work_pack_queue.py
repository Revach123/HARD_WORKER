# -*- coding: utf-8 -*-
"""
work_pack_queue.py - בוחר את הדוחות הבאים לסוכן ובונה להם חבילות עבודה (work_pack.build_pack). בלי AI.

סדר עדיפות: חברות שחיפשו באתר ב-30 הימים האחרונים (לפי מספר הצפיות, טבלת company_views), אחר כך ת"א-35, אחר כך ת"א-90 (= שאר ת"א-125 - ת"א-125 כולל את ת"א-35 ואת ת"א-90), אחר כך חברות שבמדד מניות כלשהו
אחר, ואז השאר; בתוך כל קבוצה - חברות עם יותר חברות מוחזקות באתר היום
קודם (מבנה מורכב = יותר מה לתקן). דוח שכבר יש לו חבילה (בענף work-packs) - מדלגים.

הרצה:  python work_pack_queue.py --n 20 --packs-dir ../../wp/packs --indices ../../revach/data/indices/by-company.json
                                 --site ../../revach/data/holdings/extracted_subsidiaries.json [--reports 1730567,...]
"""
import argparse
import json
import os
import sys
import tempfile
import time

import work_pack as wp

TOP = [("ת\"א-35", 0), ("ת\"א-90", 1)]      # ת"א-125 = ת"א-35 + ת"א-90


def cid_to_hp(maya_path=""):
    """companyId של מאיה -> ח.פ: רשימת החברות של מאיה (data/registry/maya_companies_full.json במאגר Revach),
    ובהיעדרה - הרשומות הקיימות של החילוץ."""
    out = {}
    if maya_path and os.path.exists(maya_path):
        d = json.load(open(maya_path, encoding="utf-8"))
        for c in (d if isinstance(d, list) else d.get("companies") or []):
            if c.get("companyId") and c.get("corporateNo"):
                out[str(c["companyId"])] = str(c["corporateNo"]).lstrip("0")
    for line in open("private_subsidiaries.jsonl", encoding="utf-8"):
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if r.get("parent_hp") and r.get("parent_ch_p"):
            out.setdefault(str(r["parent_hp"]), str(r["parent_ch_p"]).lstrip("0"))
    return out


def view_counts(days=30):
    """צפיות בכרטיסי החברות באתר ב-N הימים האחרונים (טבלת company_views ב-company-info-db) - {ח.פ: צפיות}.
    דרך Cloudflare D1 HTTP API (CF_ACCOUNT_ID, CF_D1_TOKEN, CF_DATABASE_ID). בלי הגדרות / תקלה - מילון ריק."""
    acc, tok, dbid = (os.environ.get(k, "").strip() for k in ("CF_ACCOUNT_ID", "CF_D1_TOKEN", "CF_DATABASE_ID"))
    if not (acc and tok and dbid):
        print("VIEWS: אין הגדרות D1 - בלי צפיות")
        return {}
    import requests
    sql = ("SELECT hp, SUM(n) AS n FROM company_views WHERE day >= date('now', ?) GROUP BY hp")
    try:
        r = requests.post(f"https://api.cloudflare.com/client/v4/accounts/{acc}/d1/database/{dbid}/query",
                          headers={"Authorization": f"Bearer {tok}"}, json={"sql": sql, "params": [f"-{days} day"]}, timeout=60)
        rows = (r.json().get("result") or [{}])[0].get("results") or []
        out = {str(x["hp"]).lstrip("0"): int(x["n"]) for x in rows}
        print(f"VIEWS: {len(out)} חברות נצפו ב-{days} הימים האחרונים")
        return out
    except Exception as e:
        print(f"VIEWS: שגיאה ({type(e).__name__}) - בלי צפיות")
        return {}


def priorities(plan, indices_path, site_path, maya_path=""):
    idx = json.load(open(indices_path, encoding="utf-8")).get("companies", {}) if os.path.exists(indices_path) else {}
    site = json.load(open(site_path, encoding="utf-8")).get("parents", {}) if os.path.exists(site_path) else {}
    c2h = cid_to_hp(maya_path)
    views = view_counts()
    rows = []
    for cid, e in plan.items():
        s = e.get("snapshot")
        if not s:
            continue
        hp = c2h.get(str(cid))
        ind = (idx.get(hp) or {}).get("indices", []) if hp else []
        names = {i.get("name") for i in ind}
        tier = min([t for n, t in TOP if n in names] or [2 if any(i.get("assetClass") == "equity" for i in ind) else 3])
        kids = len((site.get(hp) or {}).get("children", [])) if hp else 0
        v = views.get(hp or "", 0)
        rows.append({"report_id": str(s["report_id"]), "cid": str(cid), "hp": hp, "company": e.get("company_name"),
                     "tier": -1 if v else tier, "views": v, "kids": kids, "publish_date": s.get("publish_date")})
    # חברות שחיפשו באתר - קודם (לפי מספר הצפיות), ואז לפי המדדים; בתוך קבוצה - יותר חברות מוחזקות קודם
    rows.sort(key=lambda r: (r["tier"], -r["views"], -r["kids"], r["publish_date"] or ""))
    for i, r in enumerate(rows):
        r["priority"] = i + 1
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=20, help="כמה חבילות חדשות לבנות בהרצה")
    ap.add_argument("--reports", default="", help="report_id מסוימים (מופרדים בפסיק) במקום סדר העדיפות")
    ap.add_argument("--plan", default="selection_plan.json")
    ap.add_argument("--packs-dir", required=True)
    ap.add_argument("--indices", default="")
    ap.add_argument("--site", default="")
    ap.add_argument("--maya", default="", help="data/registry/maya_companies_full.json")
    a = ap.parse_args()
    plan = json.load(open(a.plan, encoding="utf-8"))
    rows = priorities(plan, a.indices, a.site, a.maya)
    done = {d for d in os.listdir(a.packs_dir) if os.path.exists(os.path.join(a.packs_dir, d, "manifest.json"))} \
        if os.path.isdir(a.packs_dir) else set()
    if a.reports:
        want = [r.strip() for r in a.reports.split(",") if r.strip()]
        todo = [r for r in rows if r["report_id"] in want]
    else:
        todo = [r for r in rows if r["report_id"] not in done][:a.n]
    print(f"QUEUE: {len(rows)} דוחות בתוכנית, {len(done)} עם חבילה, נבנות עכשיו {len(todo)}")
    by_cid = {str(c): e for c, e in plan.items()}
    import maya_reports_client as mrc
    for r in todo:
        e = by_cid[r["cid"]]
        urls = [e["snapshot"]["pdf_url"]] + list(e["snapshot"].get("extra_pdfs") or [])
        with tempfile.TemporaryDirectory() as td:
            files = []
            for i, u in enumerate(urls):
                local = os.path.join(td, f"{r['report_id']}_{i}.pdf")
                for attempt in range(6):
                    try:
                        with open(local, "wb") as f:
                            f.write(mrc.download_report_file(u))
                        break
                    except Exception as ex:
                        print(f"  {r['report_id']}_{i}: הורדה נכשלה ({type(ex).__name__}) - ניסיון {attempt + 1}")
                        time.sleep(15)
                if os.path.exists(local) and os.path.getsize(local) > 0:
                    files.append((f"{r['report_id']}_{i}", local))
                time.sleep(5)
            if not files:
                print(f"  {r['report_id']} ({r['company']}): אף קובץ לא ירד - מדלג")
                continue
            man = wp.build_pack(r["report_id"], files, a.packs_dir)
        man.update({"company": r["company"], "company_id": r["cid"], "hp": r["hp"], "publish_date": r["publish_date"],
                    "priority": r["priority"], "index_tier": r["tier"],
                    "urls": {f"{r['report_id']}_{i}": u for i, u in enumerate(urls)}})
        with open(os.path.join(a.packs_dir, r["report_id"], "manifest.json"), "w", encoding="utf-8") as f:
            json.dump(man, f, ensure_ascii=False, indent=1)
        print(f"PACK {r['report_id']} ({r['company']}) priority {r['priority']}: " + "; ".join(
            f"{f['name']} {f['pages']}p " + " ".join(f"{k}={len(v)}" for k, v in f["sections"].items() if v)
            for f in man["files"]))


if __name__ == "__main__":
    sys.exit(main())

# -*- coding: utf-8 -*-
"""
work_pack_queue.py - בוחר את הדוחות הבאים לסוכן ובונה להם חבילות עבודה (work_pack.build_pack). בלי AI.

סדר עדיפות: חברות שחיפשו באתר (ציון מכל ההיסטוריה, חיפושים אחרונים שווים יותר - טבלת company_views), אחר כך ת"א-35, אחר כך ת"א-90 (= שאר ת"א-125 - ת"א-125 כולל את ת"א-35 ואת ת"א-90), אחר כך חברות שבמדד מניות כלשהו
אחר, ואז השאר; בתוך כל קבוצה - חברות עם יותר חברות מוחזקות באתר היום
קודם (מבנה מורכב = יותר מה לתקן). דוח שכבר יש לו חבילה (בענף work-packs) - מדלגים.

הרצה:  python work_pack_queue.py --n 20 --packs-dir ../../wp/packs --indices ../../revach/data/indices/by-company.json
                                 --site ../../revach/data/holdings/extracted_subsidiaries.json [--reports 1730567,...]
"""
import argparse
import io
import json
import re
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


def html_to_pdf(url, base):
    """הורדת קובץ HTML / XHTML (או zip שבתוכו HTML) של דוח והמרה ל-PDF ב-Chrome headless. None אם לא הצליח."""
    import glob
    import shutil
    import subprocess
    import zipfile
    import maya_reports_client as mrc
    try:
        data = mrc.download_report_file(url)
    except Exception as ex:
        print(f"  HTML: הורדה נכשלה ({type(ex).__name__}): {url}")
        return None
    os.makedirs(base, exist_ok=True)
    src = os.path.join(base, "doc")
    if data[:2] == b"PK":
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            z.extractall(base)
        cands = sorted(glob.glob(os.path.join(base, "**", "*.*htm*"), recursive=True), key=os.path.getsize, reverse=True)
        if not cands:
            print(f"  HTML: אין קובץ HTML ב-zip: {url}")
            return None
        src = cands[0]
    else:
        src += ".html"
        open(src, "wb").write(data)
    chrome = next((c for c in ("google-chrome", "chromium", "chromium-browser") if shutil.which(c)), None)
    if not chrome:
        print("  HTML: אין Chrome להמרה")
        return None
    out = base + ".pdf"
    subprocess.run([chrome, "--headless", "--no-sandbox", "--disable-gpu", f"--print-to-pdf={out}", "--no-pdf-header-footer",
                    "file://" + os.path.abspath(src)], capture_output=True, timeout=600)
    if os.path.exists(out) and os.path.getsize(out) > 0:
        print(f"  HTML -> PDF: {os.path.basename(src)} ({os.path.getsize(out) // 1024} KB)")
        return out
    print(f"  HTML: ההמרה נכשלה: {url}")
    return None


def view_counts(half_life_days=30):
    """ציון חיפושים לכל חברה מכל ההיסטוריה (טבלת company_views ב-company-info-db): כל צפייה נספרת, ומשקלה יורד
    בחצי כל half_life_days ימים - חיפוש מאתמול שווה כמעט 1, מלפני חודש 0.5, מלפני שנה עדיין קצת. {ח.פ: ציון}.
    דרך Cloudflare D1 HTTP API (CF_ACCOUNT_ID, CF_D1_TOKEN, CF_DATABASE_ID). בלי הגדרות / תקלה - מילון ריק."""
    acc, tok, dbid = (os.environ.get(k, "").strip() for k in ("CF_ACCOUNT_ID", "CF_D1_TOKEN", "CF_DATABASE_ID"))
    if not (acc and tok and dbid):
        print("VIEWS: אין הגדרות D1 - בלי צפיות")
        return {}
    import datetime
    import requests
    try:
        r = requests.post(f"https://api.cloudflare.com/client/v4/accounts/{acc}/d1/database/{dbid}/query",
                          headers={"Authorization": f"Bearer {tok}"},
                          json={"sql": "SELECT hp, day, n FROM company_views"}, timeout=60)
        rows = (r.json().get("result") or [{}])[0].get("results") or []
    except Exception as e:
        print(f"VIEWS: שגיאה ({type(e).__name__}) - בלי צפיות")
        return {}
    today = datetime.date.today()
    out = {}
    for x in rows:
        try:
            age = (today - datetime.date.fromisoformat(str(x["day"]))).days
        except ValueError:
            continue
        hp = str(x["hp"]).lstrip("0")
        out[hp] = out.get(hp, 0.0) + int(x["n"]) * 0.5 ** (max(age, 0) / half_life_days)
    print(f"VIEWS: {len(out)} חברות נצפו (כל ההיסטוריה, משקל יורד בחצי כל {half_life_days} ימים)")
    return out


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
    # חברות שחיפשו באתר - קודם (לפי ציון החיפושים), ואז לפי המדדים; בתוך קבוצה - יותר חברות מוחזקות קודם
    rows.sort(key=lambda r: (r["tier"], -r["views"], -r["kids"], r["publish_date"] or ""))
    for i, r in enumerate(rows):
        r["priority"] = i + 1
    return rows


# דוחות שינוי שמצורפים לחבילת הדוח השנתי (--with-changes): רבעוני / חצי שנתי, ודיווח מיידי על אירוע בהחזקות.
# הסוכן קורא את הדוח השנתי ואת דוחות השינוי יחד - "יום הדוח" הוא מועד פרסום דוח השינוי האחרון (ר' ההוראות)
CHG_PERIODIC = re.compile(r"דוח\s+רבעון|חצי\s+שנתי|רבעון\s+(?:ה)?(?:ראשון|שני|שלישי)")
CHG_EVENT = re.compile(r"רכיש|מכיר|מיזוג|מימוש|השלמת\s+(?:ה)?עסק|הקמת|פירוק|חיסול|הצעת\s+רכש|שינוי\s+(?:ב)?(?:שיעור\s+)?(?:ה)?החזק|"
                       r"השקעה\s+ב|הסכם\s+(?:ל)?(?:רכישת|מכירת)|acquisit|merger|divest|disposal", re.I)
CHG_SKIP = re.compile(r"מועד\s+פרסום|מצגת|תרגום|translat")
MAX_CHANGES = 8


def pick_changes(e):
    """דוחות השינוי לחבילה (מהישן לחדש, עד MAX_CHANGES האחרונים). בלי כותרת בתוכנית (תוכנית ישנה) - לא מצרפים."""
    out = [c for c in e.get("changes") or [] if c.get("title") and c.get("pdf_url") and not CHG_SKIP.search(c["title"])
           and (CHG_PERIODIC.search(c["title"]) or CHG_EVENT.search(c["title"]))]
    return sorted(out, key=lambda c: c.get("publish_date") or "")[-MAX_CHANGES:]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=20, help="כמה חבילות חדשות לבנות בהרצה")
    ap.add_argument("--reports", default="", help="report_id מסוימים (מופרדים בפסיק) במקום סדר העדיפות")
    ap.add_argument("--plan", default="selection_plan.json")
    ap.add_argument("--packs-dir", required=True)
    ap.add_argument("--indices", default="")
    ap.add_argument("--site", default="")
    ap.add_argument("--maya", default="", help="data/registry/maya_companies_full.json")
    ap.add_argument("--results-dir", default="", help="results/ מענף agent-results - חבילה שכבר יש לה תוצאה לא נבנית מחדש")
    ap.add_argument("--rebuild-old", action="store_true",
                    help="לבנות מחדש חבילות שנבנו בגרסה ישנה (בלי table_renders: בלי תרשימים וקטוריים ותמונות טבלאות), לפני חדשות")
    ap.add_argument("--requests-dir", default="", help="pack-requests/ מענף agent-results: עמודים שסוכן ביקש להוסיף לחבילה")
    ap.add_argument("--done-requests", default="", help="קובץ פלט: רשימת report_id שהבקשות שלהם טופלו")
    ap.add_argument("--with-changes", action="store_true", help="לצרף לחבילה את דוחות השינוי שפורסמו אחרי הדוח השנתי")
    a = ap.parse_args()
    plan = json.load(open(a.plan, encoding="utf-8"))
    rows = priorities(plan, a.indices, a.site, a.maya)
    done = {d for d in os.listdir(a.packs_dir) if os.path.exists(os.path.join(a.packs_dir, d, "manifest.json"))} \
        if os.path.isdir(a.packs_dir) else set()
    extra = {}
    if a.requests_dir and os.path.isdir(a.requests_dir):
        for fn in sorted(os.listdir(a.requests_dir)):
            if fn.endswith(".json"):
                try:
                    req = json.load(open(os.path.join(a.requests_dir, fn), encoding="utf-8"))
                except ValueError:
                    continue
                ex = {}
                for x in req.get("pages") or []:
                    ex.setdefault(str(x.get("file")), []).append(int(x.get("page") or 0))
                extra[fn[:-5]] = ex
    if extra:
        # בקשות של סוכנים קודמות לתור: בונים מחדש את החבילה עם העמודים שביקשו
        todo = [r for r in rows if r["report_id"] in extra]
        print(f"REQUESTS: {len(todo)} חבילות לבנייה מחדש עם עמודים שהתבקשו")
    elif a.rebuild_old:
        def old(rid):
            try:
                m = json.load(open(os.path.join(a.packs_dir, rid, "manifest.json"), encoding="utf-8"))
            except (OSError, ValueError):
                return False
            return (m.get("builder") or 0) < wp.BUILDER_VERSION
        has_result = {x[:-5] for x in os.listdir(a.results_dir) if x.endswith(".json")} \
            if a.results_dir and os.path.isdir(a.results_dir) else set()
        todo = [r for r in rows if r["report_id"] in done and r["report_id"] not in has_result and old(r["report_id"])][:a.n]
        print(f"REBUILD: {len(todo)} חבילות מגרסה ישנה נבנות מחדש")
    elif a.reports:
        want = [r.strip() for r in a.reports.split(",") if r.strip()]
        todo = [r for r in rows if r["report_id"] in want]
    else:
        todo = [r for r in rows if r["report_id"] not in done][:a.n]
    print(f"QUEUE: {len(rows)} דוחות בתוכנית, {len(done)} עם חבילה, נבנות עכשיו {len(todo)}")
    by_cid = {str(c): e for c, e in plan.items()}
    import maya_reports_client as mrc
    for r in todo:
        e = by_cid[r["cid"]]
        amend = e["snapshot"].get("amendments") or []     # תיקון דוח שפורסם אחרי הדוח - קבצים נוספים בחבילה
        urls = [e["snapshot"]["pdf_url"]] + list(e["snapshot"].get("extra_pdfs") or []) + [x["pdf_url"] for x in amend]
        chg = pick_changes(e) if a.with_changes else []
        chg_files = {}
        html_urls = {}
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
            for k, c in enumerate(chg):
                local = os.path.join(td, f"{r['report_id']}_c{k}.pdf")
                for attempt in range(4):
                    try:
                        with open(local, "wb") as f:
                            f.write(mrc.download_report_file(c["pdf_url"]))
                        break
                    except Exception as ex:
                        print(f"  {r['report_id']}_c{k}: הורדה נכשלה ({type(ex).__name__}) - ניסיון {attempt + 1}")
                        time.sleep(15)
                if os.path.exists(local) and os.path.getsize(local) > 0:
                    files.append((f"{r['report_id']}_c{k}", local))
                    chg_files[f"{r['report_id']}_c{k}"] = c
                time.sleep(5)
            # דוח ב-iXBRL / HTML: ה-PDF הוא רק נספח (בזק - עבודת EY, 36 עמ'). ממירים את ה-HTML ל-PDF (Chrome headless)
            # ומוסיפים כקובץ נוסף בחבילה - כך יש טקסט, עמודים ותמונות כמו בכל דוח
            for j, o in enumerate(e["snapshot"].get("other_files") or []):
                if not re.search(r"x?html?|xbrl|zip", str(o.get("type") or "") + " " + o["url"], re.I):
                    continue
                pdf = html_to_pdf(o["url"], os.path.join(td, f"{r['report_id']}_h{j}"))
                if pdf:
                    files.append((f"{r['report_id']}_h{j}", pdf))
                    html_urls[f"{r['report_id']}_h{j}"] = o["url"]
            if not files:
                print(f"  {r['report_id']} ({r['company']}): אף קובץ לא ירד - מדלג")
                continue
            man = wp.build_pack(r["report_id"], files, a.packs_dir, extra.get(r["report_id"]))
        man.update({"title": e["snapshot"].get("title"), "doc_kind": wp.doc_kind(e["snapshot"].get("title"), man)})
        man.update({"company": r["company"], "company_id": r["cid"], "hp": r["hp"], "publish_date": r["publish_date"],
                    "priority": r["priority"], "index_tier": r["tier"],
                    "urls": {**{f"{r['report_id']}_{i}": u for i, u in enumerate(urls)}, **html_urls}})
        if chg_files:
            man["urls"].update({f: c["pdf_url"] for f, c in chg_files.items()})
            man["changes"] = [{"file": f, "report_id": c["report_id"], "title": c.get("title"),
                               "publish_date": c.get("publish_date")} for f, c in chg_files.items()]
        if amend:
            k0 = len(urls) - len(amend)
            man["amendments"] = [dict(x, file=f"{r['report_id']}_{k0 + j}") for j, x in enumerate(amend)]
        with open(os.path.join(a.packs_dir, r["report_id"], "manifest.json"), "w", encoding="utf-8") as f:
            json.dump(man, f, ensure_ascii=False, indent=1)
        if r["report_id"] in extra and a.done_requests:
            with open(a.done_requests, "a", encoding="utf-8") as f:
                f.write(r["report_id"] + "\n")
        print(f"PACK {r['report_id']} ({r['company']}) priority {r['priority']}: " + "; ".join(
            f"{f['name']} {f['pages']}p " + " ".join(f"{k}={len(v)}" for k, v in f["sections"].items() if v)
            for f in man["files"]))


if __name__ == "__main__":
    sys.exit(main())

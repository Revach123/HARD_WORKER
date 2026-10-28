# -*- coding: utf-8 -*-
"""
creation_limits_probe.py — בדיקה חד-פעמית: איפה מופיעות מגבלות יצירה ועמלת פדיון
אצל כל מנהלי קרנות הסל (מעבר לק079 של קסם).

שלבים:
  1. מטא-דאטה של 4 דוחות שנתיים ידועים -> formId של "דוח שנתי" לקרן.
  2. רשימת הדוחות מהטופס הזה ב-2026 -> דוח אחד לכל מנהל קרנות סל.
  3. הורדת ה-PDF, חילוץ טקסט (pypdf), ושמירת קטעים סביב מילות מפתח.
  4. שדות של דוח ק079 ישן אחד שלא פוענח (גרסת טופס קודמת).
פלט: data/funds_info/_debug/probe/
"""
import io, json, os, re, sys, time
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import creation_limits as cl
import requests
from pypdf import PdfReader

OUT = os.path.join(cl.DEBUG_DIR, "probe")
KNOWN_ANNUAL = [1727829, 1727374, 1730830, 1727369]   # אי.בי.אי, הראל, תכלית, קסם — 2025
OLD_K079 = 1475783
KEYWORDS = ["מגבלת שווי", "מגבלה לשווי", "מזערי", "מירבי", "מרבי", "עמלת פדיון",
            "עמלת יצירה", "מספר יחידות", "ק079", "ק79"]


def meta(s, rid):
    r = cl._req(s, "GET", cl.META_URL.format(id=rid), f"meta {rid}", headers=cl.GET_HEADERS)
    return r.json() if r.status_code == 200 else {"_http": r.status_code}


def list_form(s, form_id, frm="2025-12-31T22:00:00.000Z"):
    out, seen = [], set()
    to = cl.date.today().isoformat() + "T21:00:00.000Z"
    for page in range(1, 60):
        body = {"pageNumber": page, "fromDate": frm, "toDate": to, "noMeetings": False,
                "isPriority": False, "isSingle": False, "isIntendToTaseMember": False,
                "by": "company", "formId": form_id, "freeText": "", "limit": 20,
                "offset": (page - 1) * 20}
        r = cl._req(s, "POST", cl.LIST_URL, f"list {form_id} p{page}", timeout=30,
                    headers=cl.HEADERS, data=json.dumps(body))
        if r.status_code != 200:
            print(f"  list {form_id} p{page}: HTTP {r.status_code} {r.text[:200]}")
            break
        data = r.json() or []
        new = [x for x in data if x.get("id") not in seen]
        for x in new:
            seen.add(x["id"]); out.append(x)
        if not new:
            break
        time.sleep(0.2)
    return out


def pdf_text(s, m):
    atts = m.get("attachments") or []
    pdf = max((a for a in atts if (a.get("fileType") or "").lower().startswith("pdf")),
              key=lambda a: a.get("size") or a.get("fileSize") or 0, default=None)
    if not pdf:
        return None, None
    url = cl.FILES_BASE + pdf["url"].lstrip("/")
    r = cl._req(s, "GET", url, "pdf", timeout=90, headers=cl.GET_HEADERS)
    if r.status_code != 200:
        return url, f"HTTP {r.status_code}"
    reader = PdfReader(io.BytesIO(r.content))
    return url, "\n".join((p.extract_text() or "") for p in reader.pages)


def snippets(text, width=220):
    out = []
    for kw in KEYWORDS:
        for m in re.finditer(re.escape(kw), text):
            a, b = max(0, m.start() - width), m.end() + width
            out.append(f"[{kw}] ..." + re.sub(r"\s+", " ", text[a:b]) + "...")
    return out


def main():
    os.makedirs(OUT, exist_ok=True)
    s = requests.Session()

    # 1. formId של דוח שנתי
    metas = {rid: meta(s, rid) for rid in KNOWN_ANNUAL}
    with open(os.path.join(OUT, "annual_meta_sample.json"), "w", encoding="utf-8") as f:
        json.dump(metas[KNOWN_ANNUAL[0]], f, ensure_ascii=False, indent=1)
    form_ids = Counter()
    for rid, m in metas.items():
        fid = m.get("formId") or m.get("formNumber") or (m.get("form") or {}).get("id")
        print(f"[probe] meta {rid}: keys={sorted(m)[:25]} formId={fid}")
        if fid:
            form_ids[fid] += 1

    # 2. מנהלי קרנות סל
    funds = cl.fetch_all_funds(s)
    etf_mgrs = Counter(f.get("managerName") for f in funds.values() if f.get("fundType") == "קרן סל")
    print(f"[probe] מנהלי קרנות סל: {etf_mgrs.most_common()}")
    etf_ids = {fid for fid, f in funds.items() if f.get("fundType") == "קרן סל"}

    summary = {"form_ids": dict(form_ids), "etf_managers": dict(etf_mgrs), "per_manager": {}}
    for form_id in form_ids:
        reports = list_form(s, form_id)
        print(f"[probe] form {form_id}: {len(reports)} דוחות ב-2026")
        by_co = {}
        for rep in reports:
            for c in rep.get("companies") or []:
                by_co.setdefault((c.get("name") or "").strip(), []).append(rep)
        print(f"[probe] חברות מגישות: {[(k, len(v)) for k, v in by_co.items()]}")
        for co, reps in by_co.items():
            done = 0
            for rep in sorted(reps, key=lambda r: r["id"], reverse=True)[:8]:
                title = rep.get("title") or ""
                m = meta(s, rep["id"])
                url, text = pdf_text(s, m)
                if not text or text.startswith("HTTP"):
                    continue
                key = f"{co}_{rep['id']}"
                with open(os.path.join(OUT, f"annual_{key}.txt"), "w", encoding="utf-8") as f:
                    f.write(text)
                sn = snippets(text)
                summary["per_manager"][key] = {"title": title, "url": url, "snippets": sn}
                print(f"[probe] {co} {rep['id']}: {len(sn)} קטעים")
                done += 1
                if done >= 1:
                    break
                time.sleep(0.2)

    # 4. ק079 ישן
    try:
        old = cl.fetch_report_html(s, OLD_K079)
        with open(os.path.join(OUT, f"k079_old_{OLD_K079}_fields.txt"), "w", encoding="utf-8") as f:
            for a, v, lab in cl.extract_fields(old):
                f.write(f"{a}\t{v}\t<<{lab[-80:]}>>\n")
        print(f"[probe] ק079 ישן {OLD_K079}: נשמר")
    except Exception as e:
        print(f"[probe] ק079 ישן: {e}")

    with open(os.path.join(OUT, "summary.json"), "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=1)


if __name__ == "__main__":
    main()

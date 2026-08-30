#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
subsidiaries.py — משיכת חברות בת / תאגידים קשורים של כלל השוק.

הסקריפט מתבסס על התשתית של maya_core הקיים, מייצר סשן אחיד,
מושך את רשימת החברות ורץ במקביל למשיכת מבנה ההחזקות בחברות הבת.

הרצה:  py subsidiaries.py
"""

import pandas as pd
from concurrent.futures import ThreadPoolExecutor, as_completed
import maya_core as core  # שימוש חכם במנוע המרכזי שלך!

def fetch_subsidiaries(cid, name, s):
    """
    קריאה ל-Endpoint של "תאגידים קשורים/מוחזקים" במאיה.
    * הערה: במידה וזה מחזיר רשימה ריקה, ייתכן שמאיה עדכנו את נתיב ה-URL.
    (ניתן לאמת את ה-URL המדויק ע"י F12 -> Network בלשונית 'תאגידים קשורים' בחברה כלשהי).
    """
    url = f"{core.MAYA}/company/related-companies?companyId={cid}"
    # נתיבים חלופיים נפוצים במאיה במידה והעליון לא מניב תוצאה:
    # url = f"{core.MAYA}/company/sub-companies?companyId={cid}"
    # url = f"{core.MAYA}/corporate-structure/by_company?companyId={cid}"
    
    res = core._get(s, url)
    rows = []
    
    if not res:
        return rows
        
    # איתור אוטומטי של רשימת החברות מתוך מילון ה-JSON הראשי שחוזר
    items = []
    if isinstance(res, list):
        items = res
    elif isinstance(res, dict):
        # מחפש את המערך הראשון בתוך המילון (כדי לעקוף שינויי שמות כמו "subCompanies" או "items")
        for val in res.values():
            if isinstance(val, list):
                items = val
                break
                
    for item in items:
        # שימוש ב-get משורשר כדי לתפוס את המידע מכל וריאציה של מפתחות ה-JSON הפנימיים
        rows.append({
            "companyId": cid,
            "companyName": name,
            "subCompanyName": item.get("companyName", item.get("name", item.get("fullName", ""))),
            "holdingCapitalPct": item.get("holdingPercentage", item.get("capital", item.get("equity", 0))),
            "votingPct": item.get("votingPercentage", item.get("voting", 0)),
            "country": item.get("country", item.get("incorporationCountry", "")),
            "activity": item.get("activity", item.get("description", ""))
        })
        
    return rows

def main():
    print("מושך רשימת חברות מהתשתית המרכזית (maya_core)...")
    companies = core.fetch_company_list()
    
    if not companies:
        print("שגיאה: רשימת חברות ריקה. אין כתיבה.")
        raise SystemExit(1)
        
    print(f"מתחיל סריקת חברות בת עבור {len(companies)} חברות ({core.WORKERS} במקביל)...\n")
    
    s = core.new_session()
    all_subsidiaries = []
    failed = 0
    
    with ThreadPoolExecutor(max_workers=core.WORKERS) as ex:
        # בניית מקביליות זהה לחלוטין ל-update.py שלך
        futs = {ex.submit(fetch_subsidiaries, c["companyId"], c["companyName"], s): c for c in companies}
        
        for k, fut in enumerate(as_completed(futs), 1):
            c = futs[fut]
            try:
                subs = fut.result()
                if subs:
                    all_subsidiaries.extend(subs)
            except Exception as e:
                failed += 1
                # print(f"  {c['companyName']}: נכשל ({e})")
                
            if k % 50 == 0:
                print(f"  ... נסרקו {k}/{len(companies)} חברות")

    # ייצוא הנתונים
    if all_subsidiaries:
        df = pd.DataFrame(all_subsidiaries)
        
        # המרת עמודות אחוזים למספרים וסינון חברות ללא אחוזים בכלל
        df["holdingCapitalPct"] = pd.to_numeric(df["holdingCapitalPct"], errors="coerce").fillna(0)
        df = df[df["holdingCapitalPct"] > 0]
        
        # סידור התוצאות מהחזקות גדולות לקטנות לפי חברה
        df = df.sort_values(by=["companyName", "holdingCapitalPct"], ascending=[True, False])
        
        out_csv = core._out("subsidiaries.csv")
        df.to_csv(out_csv, index=False, encoding="utf-8-sig")
        
        print(f"\n✓ הושלם בהצלחה! {len(df)} חברות בת/קשורות אותרו.")
        print(f"  חברות שנכשלו/ללא מענה: {failed}")
        print(f"  → הקובץ נשמר ב: {out_csv}")
    else:
        print("\nלא אותרו נתוני חברות בת.")
        print("טיפ טכני: גש לאתר מאיה לחברה כמו 'אלביט מערכות', פתח F12 Network בלשונית 'תאגידים קשורים', ועדכן את ה-URL בשורה 23.")

if __name__ == "__main__":
    main()

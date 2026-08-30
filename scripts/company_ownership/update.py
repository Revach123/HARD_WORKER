#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
update.py — משיכת בעלות מלאה של כל השוק (מבוסס JSON).

מושך את רשימת החברות מ-datawise ולכל חברה קורא לשלושת ה-endpoints של מאיה
(בעלי עניין, פילוח, דירקטוריון) + הצלבת סוג נייר. מהיר ויציב.

רץ גם יומית (אוטומטי) וגם ידנית — אותה פעולה בדיוק (ה-JSON תמיד מחזיר מצב נוכחי,
אין היסטוריה או דלתא לתחזק).

הרצה:  py update.py
env:   WORKERS, OUTPUT_DIR, DATAWISE_APIKEY
"""

import maya_core as core


def main():
    print("מושך רשימת חברות מ-datawise...")
    companies = core.fetch_company_list()
    if not companies:
        print("שגיאה: רשימת חברות ריקה (בדוק DATAWISE_APIKEY). אין כתיבה.")
        raise SystemExit(1)
    print(f"{len(companies)} חברות. מושך בעלות מ-maya ({core.WORKERS} במקביל)...\n")
    core.build_all(companies)
    print("\nהושלם.")


if __name__ == "__main__":
    main()

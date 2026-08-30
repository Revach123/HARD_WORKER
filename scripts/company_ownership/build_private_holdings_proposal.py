"""
build_private_holdings_proposal.py

שלב 4 (חלק 1): מרכיב הצעת שילוב חברות בת פרטיות לתוך control.json -
כותב לקובץ נפרד (private_holdings_proposal.json) לבדיקה אנושית, לא
נוגע ב-control.json החי. רק אחרי אישור, שלב נפרד ימזג בפועל.

לוגיקה:
1. לכל parent_ch_p (חברת אם, אחרי שלב 1) - לוקח את הרשומה העדכנית
   ביותר עם subsidiaries לא ריק (snapshot אחרון, לפי report_publish_date).
   [הערה: change_events (עדכונים חלקיים) לא משולבים בשלב הזה - זה
   ידרוש לוגיקת מיזוג נפרדת שעוד לא נבנתה (שנרשם כ-TODO קודם).]
2. לכל חברת בת ברשימה - מחפש התאמה ב-subsidiary_name_matches.json:
   - "exact" -> ילד מלא עם hp, confidence="exact"
   - "fuzzy" -> ילד מלא עם hp, confidence="fuzzy"
   - לא נמצא -> ילד עם שם בלבד, בלי hp, confidence="none"
3. משווה מול control.json הקיים - האם לחברת האם כבר יש רשומה, ואילו
   children כבר יש לה (כדי לא להציע כפילות של מה שכבר קיים מ-maya_core).

הרצה:
    py build_private_holdings_proposal.py
"""

import json
from collections import defaultdict


def load_control_index(path: str = "control.json") -> dict[str, dict]:
    """אינדקס hp -> רשומה, לבדיקת כפילויות וזיהוי חברות אם קיימות."""
    with open(path, encoding="utf-8") as f:
        records = json.load(f)
    return {r["hp"]: r for r in records if r.get("hp")}


def load_latest_snapshots(jsonl_path: str = "private_subsidiaries.jsonl") -> dict[str, dict]:
    """לכל parent_ch_p - עדיפות ראשונה ל-annual_report העדכני ביותר
    (baseline מלא - תקנה 11/ביאור מבנה אחזקות, אמור לכלול את כל
    החברות הבת). quarterly_report רק כ-fallback אם אין annual_report
    בכלל לחברה הזו - כי דוח רבעוני עלול להזכיר רק חלק (למשל אגב עסקה
    ספציפית), לא רשימה מלאה. זו הייתה בדיוק התקלה שנצפתה בפועל (בנק
    לאומי): הבחירה הישנה "לפי תאריך בלבד" תפסה עדכון רבעוני עם רק 2
    חברות במקום הדוח השנתי עם 7 - כי הרבעוני היה quarterly_report
    מאוחר יותר בתאריך, אבל לא מקיף."""
    annual: dict[str, dict] = {}
    quarterly: dict[str, dict] = {}
    with open(jsonl_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            hp = rec.get("parent_ch_p")
            if not hp or not rec.get("subsidiaries"):
                continue

            bucket = annual if rec.get("source_type") == "annual_report" else quarterly
            existing = bucket.get(hp)
            if existing is None or (rec.get("report_publish_date") or "") > (existing.get("report_publish_date") or ""):
                bucket[hp] = rec

    latest = dict(quarterly)  # בסיס - יוחלף בהמשך בכל מקום שיש annual_report
    latest.update(annual)     # annual_report תמיד גובר, לא משנה תאריך
    return latest


def build_proposal(
    control_path: str = "control.json",
    jsonl_path: str = "private_subsidiaries.jsonl",
    matches_path: str = "subsidiary_name_matches.json",
    out_path: str = "private_holdings_proposal.json",
) -> None:
    control_index = load_control_index(control_path)
    latest_snapshots = load_latest_snapshots(jsonl_path)
    with open(matches_path, encoding="utf-8") as f:
        name_matches = json.load(f)

    proposal = {}
    n_parents_found_in_control = 0
    n_parents_missing_from_control = 0
    n_children_exact, n_children_fuzzy, n_children_none = 0, 0, 0

    for parent_hp, rec in latest_snapshots.items():
        existing_parent = control_index.get(parent_hp)
        existing_children_hps = set()
        parent_company_name = None
        if existing_parent:
            n_parents_found_in_control += 1
            parent_company_name = existing_parent.get("company")
            existing_children_hps = {c.get("hp") for c in existing_parent.get("children", []) if c.get("hp")}
        else:
            n_parents_missing_from_control += 1

        new_children = []
        for sub in rec.get("subsidiaries", []):
            name = sub.get("name")
            if not name:
                continue
            match = name_matches.get(name)

            if match and match.get("kind") == "company":
                confidence = match.get("confidence", "exact")  # רשומות ישנות בלי השדה = exact
                hp = match.get("id")
                if hp in existing_children_hps:
                    continue  # כבר קיים ב-control.json (ממקור maya_core) - לא כפילות
                new_children.append({
                    "name": match.get("name"),
                    "hp": hp,
                    "vot": sub.get("ownership_pct"),
                    "confidence": confidence,
                    "source_name_as_extracted": name,
                })
                if confidence == "exact":
                    n_children_exact += 1
                else:
                    n_children_fuzzy += 1
            elif match and match.get("kind") == "partnership":
                confidence = match.get("confidence", "exact")
                new_children.append({
                    "name": match.get("name"),
                    "hp": match.get("id"),
                    "vot": sub.get("ownership_pct"),
                    "confidence": confidence,
                    "kind": "partnership",
                    "source_name_as_extracted": name,
                })
                if confidence == "exact":
                    n_children_exact += 1
                else:
                    n_children_fuzzy += 1
            else:
                new_children.append({
                    "name": name,
                    "hp": None,
                    "vot": sub.get("ownership_pct"),
                    "confidence": "none",
                })
                n_children_none += 1

        if not new_children:
            continue

        proposal[parent_hp] = {
            "parent_company_name_in_control": parent_company_name,
            "parent_exists_in_control": existing_parent is not None,
            "report_id": rec.get("report_id"),
            "report_publish_date": rec.get("report_publish_date"),
            "new_children": new_children,
        }

    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(proposal, f, ensure_ascii=False, indent=2)

    print(f"סה\"כ {len(latest_snapshots)} חברות אם עם snapshot אחרון.")
    print(f"  {n_parents_found_in_control} נמצאו ב-control.json הקיים")
    print(f"  {n_parents_missing_from_control} לא נמצאו (אין להן רשומה ב-control.json בכלל)")
    print(f"סה\"כ הצעות children חדשים: {n_children_exact} מדויק, "
          f"{n_children_fuzzy} קרוב, {n_children_none} בלי התאמה (שם בלבד)")
    print(f"נשמר ל-{out_path} ({len(proposal)} חברות אם עם הצעות).")


if __name__ == "__main__":
    build_proposal()

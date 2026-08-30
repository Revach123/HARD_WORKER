"""
merge_private_holdings.py

שלב 4 (חלק 2): ממזג בפועל את private_holdings_proposal.json (שכבר נבנה
ואומת) לתוך control.json - מוסיף children חדשים לחברות אם קיימות, עם
תיוג source="ai_extraction" ו-confidence, כדי שה-frontend (כבר בנוי
ונדחף) ידע להציג אותם נכון (סימוני ?/???, לחיצה=חיפוש עבור לא-מאומתים).

לא נוגע בחברות אם שאינן קיימות כבר ב-control.json (11 חברות שנרשמו
כ-TODO קודם) - שלב נפרד בעתיד.

לא יוצר כפילויות: מדלג על כל hp שכבר קיים ב-children של אותה חברה
(הלוגיקה הזו כבר הייתה ב-build_private_holdings_proposal.py, אז זה
כפול-בטוח, לא נזק).

הרצה:
    py merge_private_holdings.py
"""

import json


def merge(
    control_path: str = "control.json",
    proposal_path: str = "private_holdings_proposal.json",
    out_path: str = "control.json",
) -> None:
    with open(control_path, encoding="utf-8") as f:
        control_list = json.load(f)
    with open(proposal_path, encoding="utf-8") as f:
        proposal = json.load(f)

    control_by_hp = {str(r["hp"]): r for r in control_list if r.get("hp")}

    n_merged_parents = 0
    n_children_added = 0
    n_skipped_no_parent = 0

    for parent_hp, info in proposal.items():
        if not info.get("parent_exists_in_control"):
            n_skipped_no_parent += 1
            continue

        record = control_by_hp.get(str(parent_hp))
        if record is None:
            n_skipped_no_parent += 1
            continue

        record.setdefault("children", [])
        existing_hps = {str(c.get("hp")) for c in record["children"] if c.get("hp")}
        # dedup גם לפי שם, לרשומות בלי hp (לא ניתן להשוות ח.פ - אין) -
        # בלעדי זה, מיזוג חוזר (כמו זה, בעקבות תיקון בחירת הדוח) היה
        # יוצר כפילות של אותה חברה-בת-לא-מאומתת בכל הרצה.
        existing_unmatched_names = {
            c.get("name") for c in record["children"]
            if not c.get("hp") and c.get("source") == "ai_extraction"
        }

        added_here = 0
        for child in info["new_children"]:
            if child.get("hp") and str(child["hp"]) in existing_hps:
                continue  # כבר קיים - לא כפילות
            if not child.get("hp") and child["name"] in existing_unmatched_names:
                continue  # כבר קיים כלא-מאומת באותו שם - לא כפילות

            new_child = {
                "name": child["name"],
                "vot": child.get("vot"),
                "hp": child.get("hp"),
                "source": "ai_extraction",
                "confidence": child.get("confidence", "none"),
            }
            record["children"].append(new_child)
            if child.get("hp"):
                existing_hps.add(str(child["hp"]))
            else:
                existing_unmatched_names.add(child["name"])
            added_here += 1
            n_children_added += 1

        if added_here:
            n_merged_parents += 1

    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(control_list, f, ensure_ascii=False)

    print(f"מוזגו {n_children_added} children חדשים ל-{n_merged_parents} חברות אם.")
    print(f"דולגו {n_skipped_no_parent} חברות אם ללא רשומה קיימת ב-control.json (TODO עתידי).")
    print(f"נשמר ל-{out_path}.")


if __name__ == "__main__":
    merge()

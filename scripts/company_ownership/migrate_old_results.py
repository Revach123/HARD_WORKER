"""
migrate_old_results.py

הגירה חד-פעמית: אם קיים private_subsidiaries.json (הפורמט הישן, מערך
JSON) - ממיר אותו ל-private_subsidiaries.jsonl (הפורמט החדש), בלי לאבד
נתונים ובלי כפילויות (בודק report_id קיים לפני הוספה).

לא נדרש פעם שנייה - אחרי הרצה אחת מוצלחת, הקובץ הישן כבר לא רלוונטי
(אפשר למחוק אותו מה-cache/מהרפו).

הרצה:
    py migrate_old_results.py --old private_subsidiaries.json --new private_subsidiaries.jsonl
"""

import argparse
import json
import os


def migrate(old_path: str, new_path: str) -> None:
    if not os.path.exists(old_path):
        print(f"{old_path} לא קיים - אין מה להגר, מדלג.")
        return

    with open(old_path, encoding="utf-8") as f:
        old_records = json.load(f)
    print(f"נמצאו {len(old_records)} רשומות ב-{old_path}.")

    existing_report_ids = set()
    if os.path.exists(new_path):
        with open(new_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    existing_report_ids.add(json.loads(line)["report_id"])
                except (json.JSONDecodeError, KeyError):
                    continue
        print(f"{new_path} כבר מכיל {len(existing_report_ids)} רשומות.")

    n_added = 0
    with open(new_path, "a", encoding="utf-8") as f:
        for record in old_records:
            if record.get("report_id") in existing_report_ids:
                continue  # כבר קיים - לא מכפילים
            # השלמת שדות חדשים שלא היו בפורמט הישן, כדי שהמבנה יהיה אחיד
            record.setdefault("report_publish_date", None)
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
            n_added += 1
        f.flush()
        os.fsync(f.fileno())

    print(f"נוספו {n_added} רשומות חדשות ל-{new_path} "
          f"({len(old_records) - n_added} כבר היו קיימות - דולגו).")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--old", default="private_subsidiaries.json")
    parser.add_argument("--new", default="private_subsidiaries.jsonl")
    args = parser.parse_args()
    migrate(args.old, args.new)

"""
map_parent_to_hp.py

שלב 1 בחיבור לעץ הבעלות: ל-private_subsidiaries.jsonl יש parent_hp שהוא
בפועל companyId של Maya (למשל "2063"), לא ח.פ אמיתי. הסקריפט הזה:

1. שולף את מיפוי companyId->corporateId (ח.פ) מ-maya_core.fetch_company_list()
   - מקור אמין ומדויק, לא ניחוש.
2. שומר את המיפוי לקובץ (companyid_to_hp.json) - לשימוש חוזר בלי לפנות
   שוב ל-API בכל הרצה.
3. קורא את private_subsidiaries.jsonl, ולכל רשומה מוסיף שדה חדש
   parent_ch_p = הח.פ האמיתי (או null אם לא נמצא במיפוי - למשל חברה
   דואלית או ישות שלא ב-datawise). לא נוגע ב-parent_hp הקיים (לא
   שוברים כלום), רק מוסיף מידע.

הרצה (דורש DATAWISE_APIKEY או TASE_APIKEY ב-environment):
    py map_parent_to_hp.py
"""

import json
import os

import maya_core


def build_company_id_to_hp_map(cache_path: str = "companyid_to_hp.json") -> dict[str, str]:
    """שולף (או טוען מ-cache) את המיפוי companyId->corporateId."""
    if os.path.exists(cache_path):
        print(f"נטען מיפוי קיים מ-{cache_path}.")
        with open(cache_path, encoding="utf-8") as f:
            return json.load(f)

    print("שולף רשימת חברות מ-datawise (fetch_company_list)...")
    companies = maya_core.fetch_company_list()
    mapping = {}
    n_missing_hp = 0
    for c in companies:
        cid = str(c["companyId"])
        hp = c.get("corporateId")
        if hp:
            mapping[cid] = str(hp)
        else:
            n_missing_hp += 1

    print(f"נמצאו {len(companies)} חברות, {len(mapping)} עם ח.פ תקין "
          f"({n_missing_hp} בלי ח.פ - כנראה חברות דואליות/זרות).")

    with open(cache_path, "w", encoding="utf-8") as f:
        json.dump(mapping, f, ensure_ascii=False, indent=2)
    print(f"נשמר ל-{cache_path}.")
    return mapping


def enrich_results(
    mapping: dict[str, str],
    in_path: str = "private_subsidiaries.jsonl",
    out_path: str = "private_subsidiaries.jsonl",
) -> None:
    """מוסיף parent_ch_p (ח.פ אמיתי) לכל רשומה, לפי המיפוי. לא דורס שום
    שדה קיים - רק מוסיף. כותב לקובץ זמני ואז מחליף (אטומי), כדי לא
    להשאיר קובץ פגום אם משהו נכשל באמצע."""
    n_total, n_mapped, n_unmapped = 0, 0, 0
    unmapped_ids = set()

    tmp_path = out_path + ".tmp"
    with open(in_path, encoding="utf-8") as fin, open(tmp_path, "w", encoding="utf-8") as fout:
        for line in fin:
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            n_total += 1

            company_id = rec.get("parent_hp")  # השם ההיסטורי מטעה - זה companyId בפועל
            hp = mapping.get(str(company_id))
            rec["parent_ch_p"] = hp
            if hp:
                n_mapped += 1
            else:
                n_unmapped += 1
                unmapped_ids.add(company_id)

            fout.write(json.dumps(rec, ensure_ascii=False) + "\n")

    os.replace(tmp_path, out_path)

    print(f"\nסה\"כ {n_total} רשומות | {n_mapped} עם ח.פ אמיתי | "
          f"{n_unmapped} בלי (companyId לא נמצא במיפוי)")
    if unmapped_ids:
        print(f"companyId ללא מיפוי (לבדיקה ידנית): {sorted(unmapped_ids)[:20]}"
              f"{' ...' if len(unmapped_ids) > 20 else ''}")


if __name__ == "__main__":
    mapping = build_company_id_to_hp_map()
    enrich_results(mapping)

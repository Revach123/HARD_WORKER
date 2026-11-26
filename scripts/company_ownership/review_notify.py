# -*- coding: utf-8 -*-
"""
review_notify.py - מייל לבעל האתר כשנוספו שינויים שממתינים לאישור בדשבורד הפענוח (בלי AI).

משווה את pending.json הקודם (לפני build_review.py) לחדש: אם נוספו דוחות עם שינויים מאומתים או שאלות חדשות - שולח מייל
מעוצב (scripts/registry/alerts.py במאגר Revach) עם שורה לכל חברה וכפתור לדשבורד. בלי חדשים - בלי מייל.

הרצה:  python review_notify.py --old /tmp/pending_old.json --new ../../revach/data/holdings/review/pending.json --alerts ../../revach/scripts/registry
"""
import argparse
import json
import os
import sys

DASH = "https://revach.pages.dev/extraction-dashboard"


def items(path):
    try:
        d = json.load(open(path, encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    out = {}
    for r in d.get("reports") or []:
        if r.get("verified_changes"):
            out[r["item_id"]] = r
        for q in r.get("questions") or []:
            out[q["item_id"]] = r
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--old", required=True)
    ap.add_argument("--new", required=True)
    ap.add_argument("--alerts", required=True, help="תיקיית alerts.py")
    a = ap.parse_args()
    old, new = items(a.old), items(a.new)
    fresh = [k for k in new if k not in old]
    if not fresh:
        print("NOTIFY: אין פריטים חדשים - בלי מייל")
        return 0
    by_rep = {}
    for k in fresh:
        r = new[k]
        e = by_rep.setdefault(r["report_id"], {"r": r, "q": 0, "c": False})
        if k.endswith(":company"):
            e["c"] = True
        else:
            e["q"] += 1
    rows = []
    for e in by_rep.values():
        r = e["r"]
        ch = r.get("verified_changes") or []
        n = lambda t: sum(1 for c in ch if c.get("change") == t)
        parts = []
        if e["c"]:
            parts.append(f"נוספו {n('add')} · יוסרו {n('remove')} · אחוז {n('pct')}")
        if e["q"]:
            parts.append(f"{e['q']} שאלות")
        rows.append((r.get("company") or r["report_id"], " | ".join(parts), bool(e["q"])))
    nq = sum(e["q"] for e in by_rep.values())
    nc = sum(1 for e in by_rep.values() if e["c"])
    body = (f"סוכן החילוץ סיים לבדוק דוחות. ממתינים לאישורך: {nc} חברות עם שינויים מאומתים"
            + (f" ו-{nq} שאלות שצריכות הכרעה" if nq else "") + ".\n"
            "שום שינוי לא נכנס לאתר לפני אישור בדשבורד.")
    sys.path.insert(0, a.alerts)
    import alerts
    alerts.alert(f"{len(by_rep)} דוחות ממתינים לאישור", body, error=False, rows=rows,
                 link=("לדשבורד האישורים", DASH), subtitle="חילוץ החזקות - ממתין לאישור", accent="#c8a96e", sign="&#9998;",
                 from_name="הון כשר - חילוץ החזקות")
    return 0


if __name__ == "__main__":
    sys.exit(main())

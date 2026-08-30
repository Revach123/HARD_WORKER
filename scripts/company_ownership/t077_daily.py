# -*- coding: utf-8 -*-
"""
בדיקה יומית לדוחות ת077 חדשים.
scripts/company_ownership/t077_daily.py

רץ אחרי t077_latest.py. מושך רק מה שפורסם מאז ההרצה האחרונה,
וממזג למפה הקיימת.

הרצה ב-Thonny: File -> Save as (לצד t077_core.py), ואז F5.
"""
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone

import t077_core as C

THROTTLE = 0.4
OVERLAP_DAYS = 3  # חפיפה - דוח מתקן עשוי להתפרסם על תאריך קודם


def iso(d):
    return d.strftime("%Y-%m-%dT21:00:00.000Z")


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    idx_path = C.INDEX_PATH

    if not os.path.exists(idx_path):
        raise SystemExit(f"לא נמצא {idx_path} - הרץ קודם את t077_latest.py")

    with open(idx_path, encoding="utf-8") as f:
        idx = json.load(f)
    known = {r["id"]: r for r in idx["reports"]}
    last = max((r["publishDate"] or "" for r in idx["reports"]), default="")
    print(f"ידועים: {len(known):,} דוחות. אחרון: {last[:19] or '(אין)'}")

    now = datetime.now(timezone.utc)
    d_from = (datetime.fromisoformat(last[:10]) - timedelta(days=OVERLAP_DAYS)
              if last else now - timedelta(days=30))
    d_from = d_from.replace(tzinfo=timezone.utc)
    print(f"בודק מ-{d_from.date()} (חפיפה {OVERLAP_DAYS} ימים)\n")

    print("1. חימום")
    s = C.new_session()

    print("2. פיד")
    # fetch_feed_window: retry עם חימום-מחדש על 400 (קוקיז פגים) וצמצום
    # חלון אם נדחה. מחזיר גם session מעודכן — חובה להשתמש בו להמשך.
    page, s = C.fetch_feed_window(s, iso(d_from), iso(now), d_from, now,
                                  THROTTLE, log=lambda m: print("   " + m))

    fresh = {}
    for r in page:
        m = C.report_meta(r)
        if m["id"] and m["formId"] == "ת077" and m["id"] not in known:
            fresh[m["id"]] = m

    print(f"\n   {len(page)} בטווח, מתוכם {len(fresh)} חדשים")
    if not fresh:
        print("\nאין דוחות חדשים.")
        return

    for m in sorted(fresh.values(), key=lambda x: x["publishDate"] or ""):
        fix = " (תיקון)" if "תיקון" in (m["title"] or "") else ""
        print(f"      {m['id']}  {(m['publishDate'] or '')[:10]}  "
              f"{m['companyName'][:24]:<26}{fix}")

    print("\n3. הורדה ופרסינג")
    ok = fail = 0
    folded = []   # למיזוג - ברנר אין ארכיון גולמי
    for m in sorted(fresh.values(), key=lambda x: x["id"]):
        if not m["url"]:
            print(f"   {m['id']} ללא קובץ htm - מדולג")
            continue
        try:
            parsed = C.parse_t077(C.fetch_report_html(s, m["url"]))
            C.save_report(m, parsed)
            folded.append((m["publishDate"], parsed["company"], parsed["holders"]))
            known[m["id"]] = m
            ok += 1
            corp = sum(1 for h in parsed["holders"] if h["isCorporate"])
            c = parsed.get("correction")
            note = f"  [מתקן {c['correctsReference']}]" if c else ""
            print(f"   {m['id']}  {m['companyName'][:22]:<24} "
                  f"{corp:>2} תאגידים{note}")
        except Exception as e:
            fail += 1
            print(f"   {m['id']}  שגיאה: {type(e).__name__}: {e}")
        time.sleep(THROTTLE)

    print(f"\n   הצליחו: {ok}   נכשלו: {fail}")

    idx["reports"] = sorted(known.values(), key=lambda x: x["id"])
    idx["count"] = len(known)
    idx["fetchedAt"] = now.isoformat(timespec="seconds")
    idx["to"] = str(now.date())
    with open(idx_path, "w", encoding="utf-8") as f:
        json.dump(idx, f, ensure_ascii=False, separators=(",", ":"))

    # מיזוג ולא בנייה מחדש - ברנר אין ארכיון, בנייה מאפס
    # הייתה מוחקת את כל מה ש-t077_latest.py צבר
    print("\n4. מיזוג למפה הציבורית")
    C.build_holder_map(folded, rebuild=False, log=lambda m: print("   " + m))


if __name__ == "__main__":
    main()

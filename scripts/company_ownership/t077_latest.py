# -*- coding: utf-8 -*-
"""
ת077 - הדוח האחרון לכל חברה.
scripts/company_ownership/t077_latest.py

סורק את הפיד אחורה, מקבץ לפי חברה, ומוריד רק את הדוח העדכני
ביותר של כל אחת. דוח מתקן גובר אוטומטית - הוא המאוחר.

  python t077_latest.py              # שנה
  python t077_latest.py חודש
  python t077_latest.py "5 שנים"
  python t077_latest.py 2023-01-01   # גם תאריך מפורש עובד

Thonny: File -> Save as (לצד t077_core.py), ואז בקונסול:
  %Run t077_latest.py חודש
"""
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone

import t077_core as C

THROTTLE = 0.4


def iso(d):
    return d.strftime("%Y-%m-%dT21:00:00.000Z")


PERIODS = {
    "יום": 1,
    "שבוע": 7,
    "חודש": 30,
    "שנה": 365,
    "שנתיים": 730,
    "5 שנים": 1826,
  "10 שנים" : 3700,
  "15 שנים" : 5500,
  "20 שנה" : 8000
}


def resolve_from(arg, now):
    """מקבל שם תקופה או תאריך ISO. ברירת מחדל: שנה."""
    if not arg:
        return now - timedelta(days=365), "שנה"

    a = arg.strip()
    if a in PERIODS:
        return now - timedelta(days=PERIODS[a]), a

    try:
        # fromisoformat מחזיר נאיבי; now(timezone.utc) מודע - ההשוואה נופלת
        d = datetime.fromisoformat(a)
        return (d.replace(tzinfo=timezone.utc) if d.tzinfo is None else d,
                f"מ-{d.date()}")
    except ValueError:
        raise SystemExit(
            f"ערך לא מוכר: {arg!r}\n"
            f"אפשרויות: {', '.join(PERIODS)}  או תאריך YYYY-MM-DD")


def main():
    sys.stdout.reconfigure(encoding="utf-8")

    now = datetime.now(timezone.utc)
    # ב-Actions הבחירה מגיעה כמילה אחת; ב-Thonny יכולה להיות "5 שנים"
    arg = " ".join(sys.argv[1:]).strip() if len(sys.argv) > 1 else ""
    d_from, label = resolve_from(arg, now)

    print(f"סריקה: {label}  ({d_from.date()} עד {now.date()})")
    print(f"גולמי:  {C.RAW_DIR}   (ת.ז. - לא לפרסום)")
    print(f"ציבורי: {C.PUB_DIR}\n", flush=True)

    print("1. חימום")
    s = C.new_session()
    print(f"   קוקיז: {list(s.cookies.keys())}\n", flush=True)

    # ============================================================
    # חלונות של 30 יום. הפיד בלי TotalRec, וסביר שיש לו תקרה
    # לטווח - בקשה לשנה בבת אחת עלולה לחזור חתוכה בשקט.
    # ============================================================
    print("2. סריקת הפיד")
    seen, cur = {}, d_from
    while cur < now:
        nxt = min(cur + timedelta(days=30), now)
        try:
            # fetch_feed_window מצמצם חלון אוטומטית אם maya דוחה אותו (400
            # ששרד חימום מחדש), ומחזיר גם session מעודכן (קוקיז רעננים) —
            # חובה להשתמש בו להמשך, אחרת החלון הבא יורש קוקיז פגים.
            page, s = C.fetch_feed_window(
                s, iso(cur), iso(nxt), cur, nxt, THROTTLE,
                log=lambda m: None)
        except Exception as e:
            print(f"   {cur.date()}: שגיאה {type(e).__name__}: {e}", flush=True)
            page = []
        for r in page:
            m = C.report_meta(r)
            if m["id"] and m["formId"] == "ת077":
                seen[m["id"]] = m
        print(f"   {cur.date()} .. {nxt.date()}  ->  {len(page):>3} "
              f"(מצטבר {len(seen)})", flush=True)
        cur = nxt
        time.sleep(THROTTLE)

    print(f"\n   {len(seen):,} דוחות בטווח", flush=True)

    # ============================================================
    print("\n3. הדוח האחרון לכל חברה")
    latest = {}
    for m in seen.values():
        cid = m["companyId"]
        if cid is None or not m["url"]:
            continue
        prev = latest.get(cid)
        if prev and (prev["publishDate"] or "") >= (m["publishDate"] or ""):
            continue
        latest[cid] = m

    print(f"   {len(latest):,} חברות  (מתוך {len(seen):,} דוחות)")
    print(f"   נחסכו {len(seen)-len(latest):,} הורדות", flush=True)

    fix = sum(1 for m in latest.values() if "תיקון" in (m["title"] or ""))
    print(f"   מתוכם {fix} דוחות מתקנים", flush=True)

    mb = sum((m["fileSize"] or 0) for m in latest.values()) / 1024
    print(f"   נפח משוער: {mb:.0f} MB", flush=True)

    # ============================================================
    print("\n4. הורדה ופרסינג", flush=True)
    ok = fail = 0
    folded, errors = [], []
    t0 = time.time()
    items = sorted(latest.values(), key=lambda x: x["id"])

    for i, m in enumerate(items, 1):
        try:
            parsed = C.parse_t077(C.fetch_report_html(s, m["url"]))
            C.save_report(m, parsed)
            folded.append((m["publishDate"], parsed["company"], parsed["holders"]))
            ok += 1
            if i % 50 == 0 or i == len(items):
                el = time.time() - t0
                print(f"   [{i:>4}/{len(items)}]  {ok} הצליחו, {fail} נכשלו   "
                      f"ETA {el/i*(len(items)-i)/60:.0f} דק'", flush=True)
        except Exception as e:
            fail += 1
            errors.append((m["id"], m["companyName"], type(e).__name__))
        time.sleep(THROTTLE)

    print(f"\n   הצליחו: {ok:,}   נכשלו: {fail}")
    for rid, nm, err in errors[:15]:
        print(f"      {rid}  {nm}  {err}")
    if len(errors) > 15:
        print(f"      ... ועוד {len(errors)-15}")

    # ============================================================
    print("\n5. אינדקס", flush=True)
    os.makedirs(C.PUB_DIR, exist_ok=True)
    with open(C.INDEX_PATH, "w", encoding="utf-8") as f:
        json.dump({
            "fetchedAt": now.isoformat(timespec="seconds"),
            "scannedFrom": str(d_from.date()),
            "period": label,
            "mode": "latest-per-company",
            "count": len(items),
            "reports": items,
        }, f, ensure_ascii=False, separators=(",", ":"))
    print(f"   {len(items):,} דוחות -> {C.INDEX_PATH}")

    # rebuild - זו תמונת מצב שלמה, לא תוספת
    print("\n6. בניית המפה מאפס")
    C.build_holder_map(folded, rebuild=True, log=lambda m: print("   " + m))

    print(f"\nסיום. {(time.time()-t0)/60:.1f} דקות.")


if __name__ == "__main__":
    main()

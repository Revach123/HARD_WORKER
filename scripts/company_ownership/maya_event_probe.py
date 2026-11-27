# -*- coding: utf-8 -*-
"""
maya_event_probe.py - אבחון: אילו סוגי אירועים (eventsIds) וטפסים (formId) יש לדיווחים המיידיים במאיה, ובאילו מהם
מופיעים דיווחים על רכישה / מכירה / מיזוג / הקמה / פירוק של חברות מוחזקות. (report_selector.CHANGE_EVENT_IDS = 104-106
מחזיר רק דוחות רבעוניים, מצגות והודעות על מועד פרסום - ולא דיווחים מיידיים.)

שולף כמה ימים בלי סינון אירוע, ומדפיס: מפתחות הרשומה, ולכל (formId, אירועים) - כמה דיווחים, כמה מהם בכותרת על אירוע בהחזקות,
ו-3 כותרות לדוגמה. הרצה: python maya_event_probe.py [days=7]
"""
import collections
import json
import re
import sys
import time
from datetime import date, timedelta

import requests
import urllib3

import maya_reports_client as m

urllib3.disable_warnings()
HOLD = re.compile(r"רכיש|מכיר|מיזוג|מימוש|השלמת|הקמת|פירוק|חיסול|הצעת\s+רכש|החזק|השקעה\s+ב|חברה\s+בת|חברת\s+בת|שותפות|acquisit|merger", re.I)


def page(d0, d1, n, extra=None):
    body = {"pageNumber": n, "fromDate": m._iso_start_of_day_utc(d0), "toDate": m._iso_start_of_day_utc(d1),
            "isPriority": False, "isTradeHalt": False, "by": "company", "limit": 30, "offset": (n - 1) * 30, **(extra or {})}
    r = requests.post(m.MAYA_REPORTS_URL, headers=m.HEADERS, json=body, verify=False, timeout=30)
    r.raise_for_status()
    d = r.json()
    return d if isinstance(d, list) else (d.get("reports") or [])


def scan_events(lo, hi, day):
    """לכל eventId בטווח: כמה דיווחים ביום אחד (עמוד ראשון), כמה מהם בכותרת על אירוע בהחזקות, ו-3 כותרות לדוגמה.
    השהיה של 1.5 שניות, ובחסימה (403) המתנה ארוכה וניסיון חוזר."""
    for ev in range(lo, hi + 1):
        got = None
        for attempt in range(4):
            try:
                got = page(day, day + timedelta(days=1), 1, {"eventsIds": [ev]})
                break
            except Exception as e:
                time.sleep(20 * (attempt + 1))
        time.sleep(1.5)
        if not got:
            continue
        h = [r.get("title") or "" for r in got if HOLD.search(r.get("title") or "")]
        forms = collections.Counter(r.get("formId") for r in got).most_common(3)
        print(f"EVENT {ev} | {len(got)}{'+' if len(got) == 30 else ''} | {len(h)} | {forms} | " +
              " || ".join((h or [r.get('title') or '' for r in got])[:3])[:300], flush=True)


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "events":
        lo, hi = int(sys.argv[2]), int(sys.argv[3])
        days = [date.today() - timedelta(days=k) for k in (2, 3)]
        for d in days:
            print(f"== יום {d}")
            scan_events(lo, hi, d)
        return
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 7
    end = date.today() - timedelta(days=1)
    items = []
    for k in range(days):
        d0 = end - timedelta(days=k + 1)
        for n in range(1, 40):
            try:
                got = page(d0, d0 + timedelta(days=1), n)
            except Exception as e:
                print(f"  {d0} page {n}: {type(e).__name__} {str(e)[:120]}")
                break
            if not got:
                break
            items.extend(got)
            time.sleep(0.3)
    print(f"PROBE: {len(items)} דיווחים ב-{days} ימים")
    if items:
        x = items[0]
        print("KEYS:", sorted(x.keys()))
        print("SAMPLE:", json.dumps({k: v for k, v in x.items() if k not in ("attachments", "companies")}, ensure_ascii=False)[:800])
    groups = collections.defaultdict(list)
    for r in items:
        ev = r.get("events") or r.get("eventIds") or r.get("eventsIds") or []
        evk = tuple(sorted(str(e.get("id") if isinstance(e, dict) else e) + (":" + str(e.get("name") or e.get("description") or "")[:30]
                                                                             if isinstance(e, dict) else "") for e in ev)) if isinstance(ev, list) else (str(ev),)
        groups[(r.get("formId") or r.get("form") or "?", evk)].append(r.get("title") or "")
    rows = sorted(groups.items(), key=lambda kv: -sum(1 for t in kv[1] if HOLD.search(t)))
    print("\nFORM | EVENTS | n | holdings-titled | examples")
    for (form, ev), titles in rows[:60]:
        h = [t for t in titles if HOLD.search(t)]
        print(f"{form} | {' / '.join(ev)[:120]} | {len(titles)} | {len(h)} | " + " || ".join((h or titles)[:3])[:300])


if __name__ == "__main__":
    main()

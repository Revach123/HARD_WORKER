#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
scripts/six_holders/build_six.py

מהודעות SIX (fetch_six.py) -> המחזיקים המהותיים הנוכחיים של כל חברה הנסחרת ב-SIX, באותה סכמה כמו חילוץ
הדוחות (results_eu_reports_*.jsonl), כדי ש-Revach build_eu_page יציג אותן בדף האירופאי.

כללים (כפי שהמאגר מדווח, בלי השלמות):
  * הודעה שנמחקה (deletorId) או שתוקנה בהודעה אחרת (correctorId) - לא נחשבת; קטגוריה D (הודעת מחיקה) - לא.
  * מחזיק = beneficialNames (או groupRepresentative לקבוצה); אותו מחזיק בכתיב מעט שונה ("Inc" / "Inc.",
    "Maag Rudolf" / "Rudolf Maag") מאוחד לפי המילים בשם.
  * לכל (חברה, מחזיק) - ההודעה האחרונה (תאריך עסקה, ואז פרסום). אם בה ירד מתחת ל-3%
    (belowThresholdVotingRate) או שפוזיציית הרכישה מתחת ל-3% - אינו מחזיק מהותי כיום.
  * האחוז = purchaseTotalVotingRate (מניות + זכויות רכישה, כפי שבהודעה); מניות = סכום positionsEquity.
חברה <- LEI: ISIN של המניה בהודעה, אחרת שם המנפיק מול יקום INDICES (מניות פעילות).
פלט: scripts/six_holders/results_eu_reports_six.jsonl + data/six_holders/companies.json (שורות לדף)
"""

import gzip
import json
import os
import re
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "eu_reports"))
from fetch_reports import lei_name_mismatch  # noqa: E402 - אותו כלל יקום כמו הדף האירופאי

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
D = os.path.join(ROOT, "data", "six_holders")
WORLD = os.environ.get("WORLD_DATA_DIR", os.path.join(ROOT, "_indices", "data", "world_securities"))
OUT = os.path.join(HERE, "results_eu_reports_six.jsonl")
SER_URL = "https://www.ser-ag.com/en/resources/notifications-market-participants/significant-shareholders.html"
SCHEMA_VERSION = 3
LEGAL = r"\b(ag|sa|ltd|limited|plc|nv|n v|s a|holding|holdings|group|inc|incorporated|se|the|corp|corporation|co|llc|lp|gmbh)\b"


def norm_issuer(s):
    s = (s or "").upper().replace("&", " AND ")
    s = re.sub(r"[^A-Z0-9 ]", " ", s.encode("ascii", "ignore").decode())
    return " ".join(re.sub(LEGAL.upper(), " ", s).split())


NOT_HOLDER = re.compile(r"^\s*(siehe|voir|see)\s+(bemerkung|remarque|remark|comment)", re.I)  # "siehe Bemerkung"
LEGAL_SEG = re.compile(r"^\s*(inc|ltd|llc|lp|plc|ag|sa|se|nv|gmbh|corp|limited|incorporated)\.?\s*$", re.I)


def clean_holder(name):
    """"BlackRock, Inc., New York, 10022" -> "BlackRock, Inc."; "BlackRock, Inc. (Mother Company)" -> "BlackRock, Inc."
    (המאגר מצרף כתובת ותיאור לשם)."""
    segs = re.sub(r"\([^)]*\)", " ", name or "").split(",")
    out = [segs[0]] + [x for x in segs[1:2] if LEGAL_SEG.match(x)]
    return " ".join(",".join(out).split()).strip(" ,;")


def name_keys(names):
    """מילות כל שם (בלי כתובת / צורת התאגדות / סדר): "Maag Rudolf" = "Rudolf Maag"."""
    keys = []
    for n in names:
        s = re.sub(r"[^a-z0-9 ]", " ", clean_holder(n).lower().encode("ascii", "ignore").decode())
        k = frozenset(re.sub(LEGAL, " ", s).split())
        if k:
            keys.append(k)
    return keys


def same_holder(a, b):
    """אותו מחזיק: אותן מילים, או שם אחד מוכל בשני ולפחות שתי מילים בקצר
    ("Vera Michalski" < "Vera Michalski-Hoffmann"; אבל "UBS" לבדו לא מוכל ב-"UBS Fund Management")."""
    return a == b or (min(len(a), len(b)) >= 2 and (a <= b or b <= a))


def ymd(n):
    try:
        return datetime.strptime(str(n), "%Y%m%d").date().isoformat()
    except ValueError:
        return None


def load_universe():
    isin2lei, name2lei, info = {}, {}, {}
    with open(os.path.join(WORLD, "securities.jsonl"), encoding="utf-8") as f:
        for line in f:
            r = json.loads(line)
            lei = r.get("lei")
            if not lei or r.get("category") != "stocks" or not r.get("last_seen_active"):
                continue
            if lei_name_mismatch(r):
                continue  # LEI שגוי מהרגולטור (ר' fetch_reports)
            isin2lei[r["isin"]] = lei
            n = r.get("issuer_name") or r.get("name")
            name2lei.setdefault(norm_issuer(n), lei)
            if r.get("issuer_name_exchange"):
                name2lei.setdefault(norm_issuer(r["issuer_name_exchange"]), lei)
            cur = info.get(lei)
            v = r.get("n_venues") or 0
            if cur is None or (r.get("security_type") == "Common Stock", v) > (cur["type"] == "Common Stock", cur["v"]):
                info[lei] = {"n": n, "isin": r["isin"], "cc": (r.get("issuer_country") or "").upper(),
                             "type": r.get("security_type"), "v": v, "vt": (cur or {}).get("vt", 0)}
            info[lei]["vt"] = info[lei].get("vt", 0) + v
    return isin2lei, name2lei, info


def main():
    with gzip.open(os.path.join(D, "notifications.json.gz"), "rt", encoding="utf-8") as f:
        items = json.load(f)["itemList"]
    isin2lei, name2lei, info = load_universe()

    issuer_lei, notes = {}, {}
    for x in items:
        p = x["publication"]
        sid = p["notificationSubmitterId"]
        for pos in x.get("positionsEquity") or []:
            if pos.get("isin") in isin2lei and sid not in issuer_lei:
                issuer_lei[sid] = isin2lei[pos["isin"]]
        if sid not in issuer_lei and norm_issuer(p["notificationSubmitter"]) in name2lei:
            issuer_lei[sid] = name2lei[norm_issuer(p["notificationSubmitter"])]
        if p["category"] == "D" or p["deletorId"] or p["correctorId"]:
            continue
        # מחזיק = הנהנים (beneficialNames); נציג הקבוצה הוא איש קשר שמתחלף (Roche: "Daniel Jirasko, Scobag ...")
        names = [n.strip() for n in x.get("beneficialNames") or [] if n.strip() and not NOT_HOLDER.search(n)]
        keys = name_keys(names)
        if keys:
            notes.setdefault(sid, []).append(((p["transactionDate"], p["publicationDate"], p["notificationId"]), x, names, keys))

    # מהחדשה לישנה: הודעה שחולקת מחזיק עם הודעה חדשה יותר (כולל "ירד מתחת ל-3%") - הוחלפה. שמות ההודעה
    # שהוחלפה נוספים גם הם, כך שקבוצה שהרכבה השתנה לאורך השנים נקשרת כולה להודעה האחרונה שלה.
    by_issuer = {}
    for sid, lst in notes.items():
        seen = []
        for _, x, names, keys in sorted(lst, key=lambda t: t[0], reverse=True):
            superseded = any(same_holder(k, s_) for k in keys for s_ in seen)
            seen += keys
            p = x["publication"]
            rate = p.get("purchaseTotalVotingRate") or 0.0
            if superseded or x.get("belowThresholdVotingRate") or rate < 3:
                continue
            holder = "; ".join(clean_holder(n) for n in names[:6]) + (f" (+{len(names) - 6})" if len(names) > 6 else "")
            shares = sum(int(float(q["positionVotesCount"] or 0)) for q in x.get("positionsEquity") or [])
            eq_rate = sum(float(q["positionVotingRate"] or 0) for q in x.get("positionsEquity") or [])
            ev = (f"SIX notification {p['notificationId']} ({ymd(p['publicationDate'])}): {holder} - purchase "
                  f"positions {rate:g}% of voting rights" + (f", of which shares {eq_rate:.3f}%" if eq_rate else ""))
            by_issuer.setdefault(sid, []).append({
                "name": holder, "role": "substantial_holder", "class": None,
                "shares_text": f"{shares:,}" if shares else None, "percent_text": f"{rate:g}%",
                "voting_percent_text": None, "as_of_text": ymd(p["transactionDate"]) or ymd(p["publicationDate"]),
                "evidence": ev, "_issuer": p["notificationSubmitter"], "_d": p["transactionDate"] or p["publicationDate"]})

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    companies, n_rec = {}, 0
    with open(OUT, "w", encoding="utf-8") as f:
        for sid, hs in sorted(by_issuer.items()):
            lei = issuer_lei.get(sid)
            if not lei or lei not in info:
                continue  # חברה שנמחקה מהמסחר / לא ביקום
            # הודעה ישנה שלא נסגרה מעולם ("ירד מתחת ל-3%" לא דווח לפני FinMIA) וסותרת חדשות - יחד הן מעל 100%:
            # מהחדשה לישנה, הודעה שמעבירה את הסכום מעל 100 נפסלת (Oerlikon: Victory 58.64% מ-2008 מול Liwet 42.7% מ-2024)
            kept, tot = [], 0.0
            for h in sorted(hs, key=lambda h: h["_d"], reverse=True):
                v = float(h["percent_text"][:-1])
                if tot + v <= 100.5:
                    kept.append(h)
                    tot += v
            hs = sorted(kept, key=lambda h: -float(h["percent_text"][:-1]))
            last = max(h["_d"] for h in hs)
            meta = info[lei]
            rec = {"lei": lei, "model": "six", "ts": now, "schema": SCHEMA_VERSION, "url": SER_URL,
                   "n": meta["n"], "cc": meta["cc"], "isin": meta["isin"], "as_of_date": ymd(last),
                   "shares_outstanding": [], "classes": [],
                   "holders": [{k: v for k, v in h.items() if not k.startswith("_")} for h in hs],
                   "structure": None, "control": None, "no_data_reason": None,
                   "grounding": {"holders_in": len(hs), "holders_kept": len(hs), "holders_dropped": 0, "fields_nulled": 0},
                   "issuer_six": hs[0]["_issuer"]}
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            n_rec += 1
    # שורות לדף לכל חברה שוויצרית ביקום (גם בלי מחזיק מעל 3% כיום), דירוג לפי מספר זירות המסחר
    ch = sorted((l for l, m in info.items() if m["cc"] == "CH"), key=lambda l: -info[l]["vt"])
    six_leis = set(issuer_lei.values())
    for i, lei in enumerate(ch, 1):
        m = info[lei]
        # six = יש הודעות לחברה (ואם אין שורה בפלט - אין כיום מחזיק מעל 3%); six_none = אין לה הודעות במאגר
        companies[lei] = {"n": m["n"], "isin": m["isin"], "cc": "CH", "k": i,
                          "status": "six" if lei in six_leis else "six_none"}
    json.dump({"generated_at": now[:10], "rows": companies}, open(os.path.join(D, "companies.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=0)
    print(f"six: {len(items)} notifications, {len(issuer_lei)} issuers matched to LEI, {n_rec} with current holders >=3%; "
          f"{len(companies)} Swiss companies in the universe")


if __name__ == "__main__":
    main()

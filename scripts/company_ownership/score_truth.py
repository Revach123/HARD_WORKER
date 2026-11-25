# -*- coding: utf-8 -*-
"""
score_truth.py - ציון קובץ חילוץ מול סט האמת (truth_set.json). מספרים בלבד (ריפו ציבורי - בלי שמות).

דיוק (precision): מהשמות שחולצו - כמה הם חברה/נכס/שם נוסף של ישות מסט האמת (not_holding / לא מוכר = שגוי;
uncertain לא נספר). כיסוי (recall): מישויות + נכסים בסט האמת - כמה נמצאו (בשם או בשם חלופי). גם כיסוי ישויות בלבד.
כשיש כמה רשומות לאותו דוח בקובץ - איחוד.

הרצה:  python score_truth.py model_test/subs_X.jsonl [עוד קבצים...]
"""
import json
import re
import sys


def key(n):
    return re.sub(r"[\s\"'״׳`.,()\-–]|בע\"?מ|בעמ|\bltd\b|\binc\b|\bllc\b|\bl\.?p\b", "", str(n or "").lower())


def load_truth(path="truth_set.json"):
    reps = json.load(open(path, encoding="utf-8"))["reports"]
    return list(reps.values()) if isinstance(reps, dict) else reps


def score(path, truth=None):
    truth = truth or load_truth()
    ext = {}
    for line in open(path, encoding="utf-8"):
        try:
            r = json.loads(line)
        except ValueError:
            continue
        ext.setdefault(str(r.get("report_id")), set()).update(
            key(s.get("name")) for s in r.get("subsidiaries") or [] if s.get("name"))
    tp = fp = n_found = n_good = ent_found = ent_good = 0
    reports = 0
    for rep in truth:
        got = ext.get(str(rep["report_id"]))
        if got is None:
            continue
        reports += 1
        lab = {}
        canon = {}
        for it in rep["items"]:
            for n in [it["name"]] + list(it.get("alt_names") or []):
                lab.setdefault(key(n), it["label"])
                if it["label"] in ("entity", "asset"):
                    canon[key(n)] = (key(it["name"]), it["label"])
        for k in got:
            l = lab.get(k)
            if l in ("entity", "asset", "duplicate"):
                tp += 1
            elif l == "uncertain":
                continue
            else:
                fp += 1
        found = {canon[k] for k in got if k in canon}
        good = {(key(it["name"]), it["label"]) for it in rep["items"] if it["label"] in ("entity", "asset")}
        n_found += len(found & good)
        n_good += len(good)
        ent_found += len({x for x in found & good if x[1] == "entity"})
        ent_good += len({x for x in good if x[1] == "entity"})
    return {"reports": reports, "precision": round(100 * tp / max(1, tp + fp)), "recall": round(100 * n_found / max(1, n_good)),
            "entity_recall": round(100 * ent_found / max(1, ent_good)), "found": n_found, "of": n_good, "wrong": fp}


if __name__ == "__main__":
    t = load_truth()
    for p in sys.argv[1:]:
        s = score(p, t)
        print(f"TRUTH {p.split('/')[-1]}: דוחות {s['reports']}, דיוק {s['precision']}% (שגויים {s['wrong']}), "
              f"כיסוי {s['recall']}% ({s['found']}/{s['of']}), כיסוי ישויות {s['entity_recall']}%")

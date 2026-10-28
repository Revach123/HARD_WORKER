# -*- coding: utf-8 -*-
"""creation_limits_texts.py — שמירת טקסט PDF של דוחות שנתיים (ק121) נבחרים לבדיקת פרסינג.
שימוש: python creation_limits_texts.py ID [ID ...] -> data/funds_info/_debug/texts/<id>.txt"""
import os, sys, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import creation_limits as cl
import annual_terms as at
import requests

OUT = os.path.join(cl.DEBUG_DIR, "texts")


def main(ids):
    os.makedirs(OUT, exist_ok=True)
    s = requests.Session()
    for rid in ids:
        meta = cl._req(s, "GET", cl.META_URL.format(id=rid), f"meta {rid}", headers=cl.GET_HEADERS).json()
        att = at.pdf_attachment(meta)
        r = cl._req(s, "GET", cl.FILES_BASE + att["url"].lstrip("/"), f"pdf {rid}", timeout=120, headers=cl.GET_HEADERS)
        text = at.pdf_to_text(r.content)
        with open(os.path.join(OUT, f"{rid}.txt"), "w", encoding="utf-8") as f:
            f.write(f"# title: {meta.get('title')}\n# file: {att.get('fileName')}\n{text}")
        print(f"[texts] {rid}: {att.get('fileName')} {len(text)} chars; parse={at.parse_annual(text)}")
        time.sleep(0.5)


if __name__ == "__main__":
    main([int(x) for x in sys.argv[1:]])

# -*- coding: utf-8 -*-
"""
scripts/eu_reports/universe_rules.py

כללי יקום משותפים ל-fetch_reports.py (הדוחות השנתיים) ול-six_holders/build_six.py (שוויץ). בלי תלויות.
"""

import re

_LEGAL = re.compile(r"\b(ag|sa|ltd|limited|plc|nv|holding|holdings|group|inc|se|the|corp|corporation|co|llc|lp|gmbh|"
                    r"asa|ab|oyj|spa|s|a|n|v|de|du|la|le|et|und|and|of)\b")
_SYN = {"kantonalbank": "cantonale", "banque": "bank", "banca": "bank"}


def _name_tokens(s):
    s = re.sub(r"[^a-z0-9 ]", " ", (s or "").lower().encode("ascii", "ignore").decode())
    return {_SYN.get(w, w) for w in _LEGAL.sub(" ", s).split()}


def lei_name_mismatch(r):
    """True כששם המנפיק לפי ה-LEI ושם המנפיק לפי הבורסה אינם חולקים אף מילה (גם לא תחילית של 4 אותיות)."""
    a, b = _name_tokens(r.get("issuer_name_exchange")), _name_tokens(r.get("issuer_name"))
    if not a or not b or a & b:
        return False
    return not any(x[:4] == y[:4] for x in a for y in b if len(x) > 3 and len(y) > 3)

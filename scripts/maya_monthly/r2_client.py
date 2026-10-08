# -*- coding: utf-8 -*-
"""
r2_client.py — קליינט מינימלי ל-Cloudflare R2 דרך ה-REST API של Cloudflare
(אותו endpoint ש-wrangler משתמש בו), עם API token רגיל - בלי מפתחות S3.

הטוקן צריך הרשאת "Workers R2 Storage: Edit" על החשבון.
"""

import time
from urllib.parse import quote

import requests

API = "https://api.cloudflare.com/client/v4/accounts/{acct}/r2/buckets"
_BACKOFF = (2, 4, 8, 16, 30)


class R2:
    def __init__(self, account_id, token, bucket):
        self.base = API.format(acct=account_id)
        self.bucket = bucket
        self.s = requests.Session()
        self.s.headers["Authorization"] = f"Bearer {token}"

    def _obj_url(self, key):
        return f"{self.base}/{self.bucket}/objects/{quote(key, safe='/')}"

    def _req(self, method, url, ok=(200,), **kw):
        last = None
        for wait in (0,) + _BACKOFF:
            if wait:
                time.sleep(wait)
            try:
                r = self.s.request(method, url, timeout=120, **kw)
            except requests.RequestException as e:
                last = f"{type(e).__name__}: {e}"
                continue
            if r.status_code in ok:
                return r
            last = f"HTTP {r.status_code}: {(r.text or '')[:300]!r}"
            if r.status_code in (400, 401, 403, 404, 409):
                break                          # לא זמני - אין טעם לנסות שוב
        raise RuntimeError(f"R2 {method} {url} נכשל: {last}")

    def ensure_bucket(self):
        """יוצר את ה-bucket אם אינו קיים. זורק חריגה ברורה אם לטוקן אין הרשאה."""
        r = self.s.get(f"{self.base}/{self.bucket}", timeout=60)
        if r.status_code == 200:
            return False
        if r.status_code in (401, 403):
            raise RuntimeError(f"לטוקן אין הרשאת R2 (HTTP {r.status_code}): {r.text[:300]}")
        self._req("POST", self.base, json={"name": self.bucket})
        return True

    def put(self, key, data, content_type="application/octet-stream"):
        self._req("PUT", self._obj_url(key), data=data,
                  headers={"Content-Type": content_type})

    def get(self, key):
        """מחזיר bytes, או None אם האובייקט לא קיים."""
        r = self.s.get(self._obj_url(key), timeout=120)
        if r.status_code == 404:
            return None
        if r.status_code != 200:
            raise RuntimeError(f"R2 GET {key}: HTTP {r.status_code}: {r.text[:300]!r}")
        return r.content

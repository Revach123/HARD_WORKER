"""
test_url_context.py

בדיקה חד-פעמית וזולה (בקשה אחת בלבד): האם Gemini's url_context tool
מצליח לשלוף PDF ישירות מ-mayafiles.tase.co.il, בלי שאנחנו מורידים אותו
קודם. אם כן - אפשר לפשט משמעותית את run_small_batch.py.

הרצה:
    py test_url_context.py --pdf-url "https://mayafiles.tase.co.il/rpdf/..."
"""

import argparse
import json
import os

import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

GEMINI_MODEL = "gemini-3.5-flash-lite"
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]
GEMINI_URL = (
    f"https://generativelanguage.googleapis.com/v1beta/models/"
    f"{GEMINI_MODEL}:generateContent?key={GEMINI_API_KEY}"
)


def test_url_context(pdf_url: str) -> None:
    payload = {
        "contents": [
            {
                "role": "user",
                "parts": [
                    {
                        "text": (
                            f"קרא את המסמך בכתובת {pdf_url} ותגיד לי: "
                            "1) האם הצלחת לגשת אליו, 2) כמה עמודים יש בו "
                            "בערך, 3) שם החברה המדווחת. תשובה קצרה בעברית."
                        )
                    },
                ],
            }
        ],
        "tools": [{"url_context": {}}],
    }

    resp = requests.post(GEMINI_URL, json=payload, verify=False, timeout=60)
    print("status:", resp.status_code)
    data = resp.json()

    # url_context מחזיר metadata על מה שהוא הצליח/נכשל לשלוף
    try:
        candidate = data["candidates"][0]
        text = candidate["content"]["parts"][0]["text"]
        print("\n=== תשובת המודל ===")
        print(text)

        url_metadata = candidate.get("urlContextMetadata") or candidate.get("url_context_metadata")
        print("\n=== url_context metadata (סטטוס השליפה בפועל) ===")
        print(json.dumps(url_metadata, ensure_ascii=False, indent=2))
    except (KeyError, IndexError):
        print("\n=== תגובה מלאה (לא בפורמט צפוי) ===")
        print(json.dumps(data, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--pdf-url", required=True)
    args = parser.parse_args()
    test_url_context(args.pdf_url)

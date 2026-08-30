"""
run_extraction.py

הרצה קלה: PDF מקומי -> Gemini -> private_subsidiaries.jsonl.
בלי D1, בלי Maya - רק כדי לוודא שהשרשרת המלאה עובדת מקצה לקצה.

הרצה:
    py run_extraction.py --pdf "C:\\Users\\אריה צבי\\Downloads\\P1732303-00.pdf" ^
        --company-legal-id 520025370 --report-id manual-test-1 ^
        --source-type investor_presentation
"""

import argparse
import json

import extract_subsidiaries as ex

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--pdf", required=True, help="נתיב מקומי לקובץ ה-PDF")
    parser.add_argument("--company-legal-id", required=True)
    parser.add_argument("--report-id", required=True)
    parser.add_argument(
        "--source-type",
        required=True,
        choices=["quarterly_report", "annual_report", "investor_presentation"],
    )
    args = parser.parse_args()

    with open(args.pdf, "rb") as f:
        pdf_bytes = f.read()

    print("שולח ל-Gemini...")
    result = ex.call_gemini_extraction(pdf_bytes, filename_hint=args.pdf)

    print("\n=== תוצאת החילוץ ===")
    print(json.dumps(result, ensure_ascii=False, indent=2))

    ex.save_extraction_json(
        company_legal_id=args.company_legal_id,
        report_id=args.report_id,
        source_type=args.source_type,
        extracted=result,
    )

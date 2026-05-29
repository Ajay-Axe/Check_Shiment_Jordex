"""
BL Extraction via Gemini API - DEBUG VERSION
Shows step-by-step progress
"""

import os
import sys
import json
import base64
import subprocess
import platform
import requests
from pathlib import Path
from dotenv import load_dotenv

print("=" * 70)
print("BL EXTRACTOR - STARTING")
print("=" * 70)

# Load .env
print("\n[1/8] Loading .env file...")
load_dotenv()

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
if not GEMINI_API_KEY:
    print("ERROR: GEMINI_API_KEY not found in .env file")
    sys.exit(1)
print(f"[OK] API Key found: {GEMINI_API_KEY[:20]}...")

# Config
GEMINI_MODEL = "gemini-2.5-flash"
GEMINI_URL = (
    f"https://generativelanguage.googleapis.com/v1beta/models/"
    f"{GEMINI_MODEL}:generateContent?key={GEMINI_API_KEY}"
)

INPUT_FILE = r"F:\Axe Global\Check Shipment\Shipments\25-May-2026__OI2611714\MBL.docx"
OUTPUT_JSON = str(Path(INPUT_FILE).with_suffix(".json"))

print(f"\n[2/8] Checking input file...")
print(f"  Input: {INPUT_FILE}")
if not os.path.isfile(INPUT_FILE):
    print(f"ERROR: File not found!")
    sys.exit(1)
print(f"[OK] File exists ({os.path.getsize(INPUT_FILE) / 1024:.1f} KB)")

# Extraction prompt
EXTRACTION_PROMPT = """You are a logistics document extraction AI used by a freight forwarder (JORDEX).
Analyze this shipping document PDF. Follow the steps IN ORDER. Do not skip ahead.

=====================================================================
STEP 1 — CLASSIFY THE DOCUMENT (DO THIS FIRST, BEFORE ANYTHING ELSE)
=====================================================================

BEFORE extracting any fields, determine what type of document this is.
Read the TITLE, HEADER, and FIRST FEW LINES of the document carefully.

CHECK FOR THESE NON-BL INDICATORS FIRST:
  - Words: "DEBIT NOTE", "CREDIT NOTE", "INVOICE", "ARRIVAL NOTICE",
    "BOOKING CONFIRMATION", "PACKING LIST", "CERTIFICATE OF ORIGIN"
  - Currency amounts: USD, EUR, GBP followed by numbers (e.g. USD 35.00)
  - Charge codes: DR, CR, RATE, AMOUNT, TOTAL, PER CONTAINER
  - Billing language: "Bill To", "Invoice No", "Due Date", "Payment Terms"
  - Tabular charges: lines with service descriptions and monetary amounts

A DEBIT NOTE typically contains:
  - Header saying "DEBIT NOTE" or "D/N" or "DEBIT ADVICE"
  - A debit note number (e.g. DL26050640HK)
  - Currency charges (e.g. USD 35.00/CONTAINER)
  - May reference a BL number, vessel, port — but it is NOT a BL itself
  - It is a financial document, not a transport document

AN ARRIVAL NOTICE typically contains:
  - Header saying "ARRIVAL NOTICE" or "NOTICE OF ARRIVAL"
  - ETA, vessel/voyage info, charges breakdown
  - It references a shipment but is NOT a BL itself

AN INVOICE typically contains:
  - Header saying "INVOICE" or "TAX INVOICE"
  - Invoice number, line items with amounts
  - Bill To / addressee section

IF ANY of the above non-BL indicators are found, the document is NOT a BL.
Return ONLY this JSON and STOP IMMEDIATELY:

  {"document_title": "<TITLE>", "skip": true}

Rules for document_title:
  - "DEBIT NOTE" or "D/N" in header -> document_title = "DEBIT NOTE"
  - "CREDIT NOTE" in header -> document_title = "CREDIT NOTE"
  - "ARRIVAL NOTICE" in header -> document_title = "ARRIVAL NOTICE"
  - "PACKING LIST" in header -> document_title = "PACKING LIST"
  - "CERTIFICATE OF ORIGIN" in header -> document_title = "CERTIFICATE OF ORIGIN"
  - "BOOKING CONFIRMATION" in header -> document_title = "BOOKING CONFIRMATION"
  - INVOICE (special rule):
      Look at the "Bill To" / "To" / addressee on the invoice.
      If addressee contains "JORDEX" -> document_title = "AGENT INVOICE"
      If addressee is any other company -> document_title = "COMMERCIAL INVOICE"
      If addressee unclear -> document_title = "INVOICE"
  - Any other non-BL -> document_title = "<DOCUMENT HEADER TEXT>"

CRITICAL: A debit note or invoice may mention vessel names, port names, BL numbers,
and container numbers as REFERENCES. That does NOT make it a Bill of Lading.
Financial documents reference shipments. Only actual BL documents should be extracted.

ONLY if NONE of the above non-BL indicators are found, proceed to Step 2.

A document IS a BL if it has ALL of these together:
  - A dedicated Shipper box with a company name/address
  - A dedicated Consignee box with a company name/address
  - A B/L No., Sea Waybill No., or Bill of Lading No. field
  - Vessel/Voyage, Port of Loading, Port of Discharge fields
  - "Shipped on Board" date and carrier signature
  - Header/title containing "BILL OF LADING" or "WAYBILL" or "B/L"

IMPORTANT: "SEA WAYBILL" IS a type of Bill of Lading. Do NOT skip it.

=====================================================================
STEP 2 — MASTER BL vs HOUSE BL (ONLY FOR BL DOCUMENTS)
=====================================================================

There are ONLY two valid document types. No exceptions.

Read the CONSIGNEE box on the document:

  RULE A: CONSIGNEE contains "JORDEX" (any variation) ->
    document_type = "MASTER BILL OF LADING"

  RULE B: CONSIGNEE is any other company (NOT JORDEX) ->
    document_type = "HOUSE BILL OF LADING"

CRITICAL:
  - The document header (e.g. "SEA WAYBILL") does NOT determine MBL vs HBL.
  - JORDEX in Delivery Agent, Notify, or Also Notify does NOT count.
  - ONLY the CONSIGNEE box determines MBL vs HBL.
  - NEVER output "SEA WAYBILL" or any other text as document_type.

=====================================================================
STEP 3 — BL TYPE (ORIGINAL vs SEA WAYBILL)
=====================================================================

bl_type is SEPARATE from document_type.

Resolve bl_type to EXACTLY one of two values: "ORIGINAL" or "SEA WAYBILL".

How to determine:
  - Find "No. of original B(s)/L" or "original bills of lading have been signed"
  - If the number is 3, THREE -> bl_type = "ORIGINAL"
  - If the number is 0, 1, ZERO, ONE, NIL -> bl_type = "SEA WAYBILL"
  - If the document header says "SEA WAYBILL" or "NON NEGOTIABLE" -> bl_type = "SEA WAYBILL"
  - If no originals count is mentioned -> bl_type = "ORIGINAL"

Output ONLY "ORIGINAL" or "SEA WAYBILL" as the bl_type value.

=====================================================================
STEP 4 — REFERENCE NUMBER (B/L NUMBER / MBL NUMBER)
=====================================================================

Extract the B/L or Waybill reference number from:
  "B/L No.", "BL No", "Sea Waybill No.", "Waybill No.", "Bill of Lading No."

CRITICAL — EXTRACT THE COMPLETE NUMBER WITH ITS PREFIX:
  - BL numbers almost always have a carrier prefix (letters) followed by digits
    or an alphanumeric sequence. The prefix is PART of the number.
  - The prefix may be printed ABOVE, BEFORE, or NEAR the number in smaller text.
  - Some BLs print the carrier code and number on separate lines — COMBINE them.
  - NEVER return just the numeric portion without the prefix.

SELF-CHECK: The reference_number should contain BOTH letters and digits.

=====================================================================
STEP 5 — CONTAINER NUMBER (STRICT ISO 6346 PATTERN)
=====================================================================

Container number = EXACTLY 4 UPPERCASE LETTERS + 7 DIGITS (total 11 characters).
Regex pattern: ^[A-Z]{4}[0-9]{7}$

SELF-CHECK BEFORE RETURNING: Count the characters in your container_no value.
  - It MUST have EXACTLY 4 letters then EXACTLY 7 digits. No more, no less.

COMMON MISTAKE — BL REFERENCE vs CONTAINER:
  - BL/booking references often appear NEAR container numbers. They are NOT containers.
  - Container numbers are ALWAYS exactly 4 letters + 7 pure digits. No exceptions.

DISAMBIGUATION (SLASHES):
  - Container / Seal pattern: "YMMU1395094 / YMAU451322"
    -> container_no=YMMU1395094, seal_no=YMAU451322

SUMMARY / TOTAL ROWS — DO NOT CREATE CONTAINER ENTRIES FOR THESE.

SCAN ALL PAGES for container numbers.

=====================================================================
STEP 6 — DESCRIPTION / GOODS CLEANUP
=====================================================================

Extract ONLY the actual product/commodity names.

STRIP ALL shipping instructions, movement types, container specs, quantity prefixes,
reference numbers, contact info, HS codes, freight terms, boilerplate.

SEPARATOR: Use newline between multiple product names.

=====================================================================
STEP 7 — REMAINING RULES
=====================================================================

1. ALL text MUST be UPPERCASE.
2. FREIGHT: Extract ONLY from the Freight box (PREPAID or COLLECT). Return null if unclear.
3. NOTIFY: If "Same as Consignee", copy full consignee details.
4. ATTACH LIST: If present, extract goods/weights/CBM from its rows.
5. CARRIER: From logo, header, or SIGNED BY section.

=====================================================================
OUTPUT — Return valid JSON only. No markdown. No backticks. No explanation.
=====================================================================

{
  "document_type": "MASTER BILL OF LADING or HOUSE BILL OF LADING",
  "bl_type": "ORIGINAL or SEA WAYBILL",
  "reference_number": "B/L OR WAYBILL NUMBER",
  "shipper": "FULL SHIPPER NAME AND ADDRESS",
  "consignee": "FULL CONSIGNEE NAME AND ADDRESS",
  "notify": "FULL NOTIFY PARTY NAME AND ADDRESS",
  "carrier_name": "CARRIER NAME (short form e.g. HAPAG-LLOYD)",
  "carrier_code": "JORDEX DROPDOWN CODE e.g. OOLU, HAPAG LLOYD, MSCU, ZIMU",
  "vessel": "VESSEL NAME",
  "voyage": "VOYAGE NUMBER",
  "port_of_loading": "PORT NAME",
  "port_of_discharge": "PORT NAME",
  "place_of_delivery": "PLACE NAME OR null",
  "marks_and_numbers": "MARKS DETAILS OR N/M",
  "number_and_type_of_packages": "e.g. 2020 CARTONS",
  "description": "CLEAN COMMODITY NAMES ONLY",
  "shipped_on_board_date": "YYYY-MM-DD",
  "containers": [
    {
      "container_no": "4 LETTERS + 7 DIGITS",
      "container_type": "e.g. 40HC or null",
      "seal_no": "SEAL NUMBER or null",
      "package_qty": "NUMBER ONLY or null",
      "package_type": "e.g. CARTONS or null",
      "gross_weight": "e.g. 13736.000 KGS or null",
      "measurement": "e.g. 67.590 CBM or null",
      "goods_description": "CLEAN COMMODITY NAMES or null"
    }
  ],
  "total_gross_weight": "TOTAL WEIGHT",
  "total_measurement": "TOTAL VOLUME",
  "freight_charges": "PREPAID OR COLLECT OR null",
  "issue_place_and_date": "PLACE AND DATE"
}"""


def docx_to_pdf(docx_path: str) -> str:
    """Convert .docx to .pdf and return the PDF path."""
    print("\n[3/8] Converting DOCX to PDF...")
    pdf_path = str(Path(docx_path).with_suffix(".pdf"))

    system = platform.system()
    print(f"  Detected OS: {system}")

    if system == "Windows":
        # Try docx2pdf first (requires MS Word installed)
        print("  Attempting docx2pdf (needs MS Word)...")
        try:
            from docx2pdf import convert
            convert(docx_path, pdf_path)
            print(f"[OK] Converted via docx2pdf -> {pdf_path}")
            return pdf_path
        except ImportError:
            print("  docx2pdf not installed, trying LibreOffice...")
        except Exception as e:
            print(f"  docx2pdf failed: {e}")
            print("  Trying LibreOffice...")

        # Fallback: LibreOffice on Windows
        lo_paths = [
            r"C:\Program Files\LibreOffice\program\soffice.exe",
            r"C:\Program Files (x86)\LibreOffice\program\soffice.exe",
        ]
        soffice = next((p for p in lo_paths if os.path.isfile(p)), None)
        if not soffice:
            print("ERROR: Neither MS Word nor LibreOffice found.")
            print("Install one of:")
            print("  - pip install docx2pdf (requires MS Word)")
            print("  - LibreOffice from https://www.libreoffice.org/download/")
            sys.exit(1)
    else:
        soffice = "libreoffice"

    print(f"  Using: {soffice}")
    out_dir = str(Path(docx_path).parent)
    cmd = [
        soffice,
        "--headless",
        "--convert-to", "pdf",
        "--outdir", out_dir,
        docx_path,
    ]
    print(f"  Command: {' '.join(cmd)}")
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    
    if result.returncode != 0:
        print(f"ERROR: Conversion failed")
        print(f"  stdout: {result.stdout}")
        print(f"  stderr: {result.stderr}")
        sys.exit(1)
    
    print(f"[OK] Converted via LibreOffice -> {pdf_path}")
    return pdf_path


def file_to_base64(filepath: str) -> str:
    """Read a file and return its base64-encoded string."""
    print("\n[4/8] Encoding PDF to base64...")
    with open(filepath, "rb") as f:
        b64 = base64.b64encode(f.read()).decode("utf-8")
    size_mb = len(b64) * 3 / 4 / (1024 * 1024)
    print(f"[OK] Encoded {size_mb:.2f} MB")
    return b64


def call_gemini(pdf_base64: str, prompt: str) -> dict:
    """Send PDF + prompt to Gemini and return the parsed JSON response."""
    print("\n[5/8] Preparing Gemini API request...")
    payload = {
        "contents": [
            {
                "parts": [
                    {
                        "inline_data": {
                            "mime_type": "application/pdf",
                            "data": pdf_base64,
                        }
                    },
                    {"text": prompt},
                ]
            }
        ],
        "generationConfig": {
            "temperature": 0.1,
            "maxOutputTokens": 8192,
        },
    }

    headers = {"Content-Type": "application/json"}
    print(f"[6/8] Calling Gemini API ({GEMINI_MODEL})...")
    print(f"  Endpoint: {GEMINI_URL[:80]}...")
    print(f"  Timeout: 180 seconds")
    
    try:
        resp = requests.post(GEMINI_URL, headers=headers, json=payload, timeout=180)
    except requests.exceptions.Timeout:
        print("ERROR: Request timed out after 180 seconds")
        sys.exit(1)
    except requests.exceptions.RequestException as e:
        print(f"ERROR: Network error: {e}")
        sys.exit(1)

    print(f"  Response status: {resp.status_code}")
    
    if resp.status_code != 200:
        print(f"ERROR: Gemini API returned {resp.status_code}")
        print(f"Response: {resp.text[:500]}")
        sys.exit(1)

    print("[7/8] Parsing response...")
    data = resp.json()

    # Extract text from the response
    try:
        raw_text = data["candidates"][0]["content"]["parts"][0]["text"]
        print(f"[OK] Got {len(raw_text)} characters of text")
    except (KeyError, IndexError) as e:
        print(f"ERROR: Unexpected response structure")
        print(json.dumps(data, indent=2))
        sys.exit(1)

    # Strip markdown fences if Gemini wraps them
    cleaned = raw_text.strip()
    if cleaned.startswith("```"):
        lines = cleaned.split("\n")
        if lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        cleaned = "\n".join(lines).strip()

    try:
        result = json.loads(cleaned)
        print("[OK] Valid JSON parsed")
    except json.JSONDecodeError as e:
        print(f"WARN: Response is not valid JSON")
        print(f"  Error: {e}")
        print(f"  Saving raw response instead")
        return {"_raw_response": raw_text, "_error": str(e)}

    return result


def main():
    input_path = INPUT_FILE
    ext = Path(input_path).suffix.lower()

    # Determine the PDF to send
    if ext == ".docx" or ext == ".doc":
        pdf_path = docx_to_pdf(input_path)
    elif ext == ".pdf":
        print("\n[3/8] Input is already PDF, skipping conversion")
        pdf_path = input_path
    else:
        print(f"ERROR: Unsupported file type '{ext}'. Use .docx or .pdf.")
        sys.exit(1)

    # Encode and send
    pdf_b64 = file_to_base64(pdf_path)
    result = call_gemini(pdf_b64, EXTRACTION_PROMPT)

    # Save JSON
    print(f"\n[8/8] Saving result...")
    print(f"  Output: {OUTPUT_JSON}")
    with open(OUTPUT_JSON, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2, ensure_ascii=False)

    print("\n" + "=" * 70)
    print("EXTRACTION COMPLETE")
    print("=" * 70)
    print(f"\nResult saved to: {OUTPUT_JSON}")
    print("\nExtracted data:")
    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n\nCancelled by user")
        sys.exit(1)
    except Exception as e:
        print(f"\n\nUNEXPECTED ERROR: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
"""
extractor.py
Sends a PDF to Google Gemini 2.5 Flash Lite and extracts shipping document data as JSON.

Identification rules (derived from real HBL/MBL samples):
  - If CONSIGNEE contains JORDEX -> MASTER BILL OF LADING
  - If CONSIGNEE is any other company -> HOUSE BILL OF LADING
  - bl_type resolved to: ORIGINAL (3 originals) or SEA WAYBILL (0/1 originals)
  - Container number pattern: exactly 4 uppercase letters + 7 digits (ISO 6346)
  - Non-BL documents (debit notes, invoices, arrival notices) return skip=true
    with document_title only. Invoice: AGENT INVOICE (to JORDEX) or COMMERCIAL INVOICE.
"""

import os
import json
import re
import base64
from dotenv import load_dotenv
import requests

load_dotenv()

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
# ── Carrier name → Jordex dropdown search code ────────────────────
CARRIER_NAME_TO_CODE = {
    "HAPAG-LLOYD": "HAPAG LLOYD",
    "HAPAG LLOYD": "HAPAG LLOYD",
    "OOCL": "OOLU",
    "ORIENT OVERSEAS CONTAINER LINE": "OOLU",
    "YANG MING": "YMJA",
    "YANG MING LINE": "YMJA",
    "MSC": "MSCU",
    "MEDITERRANEAN SHIPPING COMPANY": "MSCU",
    "MEDITERRANEAN SHIPPING CO": "MSCU",
    "MAERSK": "MAEU",
    "MAERSK LINE": "MAEU",
    "A.P. MOLLER - MAERSK": "MAEU",
    "OCEAN NETWORK EXPRESS": "ONEY",
    "ONE LINE": "ONEY",
    "EVERGREEN": "EGLV",
    "EVERGREEN LINE": "EGLV",
    "COSCO": "COEU",
    "COSCO SHIPPING": "COEU",
    "HMM": "HDMU",
    "HYUNDAI MERCHANT MARINE": "HDMU",
    "ZIM": "ZIMU",
    "ZIM INTEGRATED SHIPPING": "ZIMU",
    "ZIM INTEGRATED SHIPPING SERVICES LTD": "ZIMU",
}

FALLBACK_MODEL = "gemini-2.5-flash"

# ──────────────────────────────────────────────────────────────────────
# ISO 6346 container number: 4 uppercase letters + 7 digits
# ──────────────────────────────────────────────────────────────────────
CONTAINER_PATTERN = re.compile(r'[A-Z]{4}\d{7}')

CONTAINER_RETRY_PROMPT = """The following JSON was extracted from this shipping document (Bill of Lading).
However, the container number(s) are missing.

RULE: Every Bill of Lading ALWAYS contains at least one container number.
Container number = EXACTLY 4 UPPERCASE LETTERS + 7 DIGITS (ISO 6346 pattern: [A-Z]{4}[0-9]{7})

Examples of valid container numbers: OOCU5376542, FANU1151538, TCLU7845231, HLBU3704517

WHERE TO LOOK IN THE PDF:
  - "MARKS AND NUMBERS" column or box
  - "CONTAINER NO." or "CNTR NO" column in any table
  - "PARTICULARS FURNISHED BY SHIPPER" section
  - Any table row — container numbers often appear on PAGE 2 or later pages
  - Lines containing slash-separated values like "MRSU7913121/ML-ID1040329/40HC/1000BAGS/25750.000KGS/43.000CBM"
    (In this example, container_no = MRSU7913121, seal_no = ML-ID1040329)
  - RULE: If strings are separated by slashes "/", the FIRST matching the pattern is usually the container.
  - Near the goods description, weight, or measurement columns
  - Stamp or typed text anywhere on any page of the document

TASK:
1. Scan EVERY PAGE of the attached PDF carefully for the container number pattern.
2. Find ALL container numbers (4 uppercase letters + 7 digits).
3. Return the SAME JSON as below but with container_no filled in.
4. IMPORTANT: If the current "containers" array is empty [], you MUST create a new container object for each container number you find (e.g., {{"container_no": "MRSU7913121"}}).
5. If a container entry already exists with missing/null data, update it.
6. Do NOT change any other field outside of the "containers" array. Return ONLY valid JSON. No markdown. No backticks.

Current extracted JSON:
{current_json}"""

MBL_RETRY_PROMPT = """The following JSON was extracted from this shipping document.
However, the reference_number (B/L number) appears to be incomplete or incorrect.

RULE: Every Bill of Lading has a COMPLETE reference number that includes:
  - A carrier/company prefix (letters) followed by a numeric or alphanumeric sequence.
  - The prefix is part of the number — do NOT strip it.

The reference number is currently: {current_ref}

TASK:
1. Scan the ENTIRE PDF for the B/L number, Waybill number, or reference number.
2. Look in these locations:
   - "B/L No.", "BL No.", "Bill of Lading No." labeled field
   - Document header / title area
   - Top-right corner of the first page
   - Near the carrier logo or company name
   - "BOOKING NO" or "REFERENCE" fields
3. Find the COMPLETE number including any letter prefix.
   - If the prefix is printed SEPARATELY from the digits (e.g. on the line above),
     COMBINE them into one string.
   - Example: "HLCU" printed above "SZX2604CGUJ4" → "HLCUSZX2604CGUJ4"
   - Example: "MAEU" then "123456789" → "MAEU123456789"
4. Return the SAME JSON below but with reference_number corrected.
5. Do NOT change any other field. Return ONLY valid JSON. No markdown. No backticks.

Current extracted JSON:
{current_json}"""

# ──────────────────────────────────────────────────────────────────────
# EXTRACTION PROMPT — battle-tested against real HBL + MBL samples
# ──────────────────────────────────────────────────────────────────────
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
  - "DEBIT NOTE" or "D/N" in header → document_title = "DEBIT NOTE"
  - "CREDIT NOTE" in header → document_title = "CREDIT NOTE"
  - "ARRIVAL NOTICE" in header → document_title = "ARRIVAL NOTICE"
  - "PACKING LIST" in header → document_title = "PACKING LIST"
  - "CERTIFICATE OF ORIGIN" in header → document_title = "CERTIFICATE OF ORIGIN"
  - "BOOKING CONFIRMATION" in header → document_title = "BOOKING CONFIRMATION"
  - INVOICE (special rule):
      Look at the "Bill To" / "To" / addressee on the invoice.
      If addressee contains "JORDEX" → document_title = "AGENT INVOICE"
      If addressee is any other company → document_title = "COMMERCIAL INVOICE"
      If addressee unclear → document_title = "INVOICE"
  - Any other non-BL → document_title = "<DOCUMENT HEADER TEXT>"

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

  RULE A: CONSIGNEE contains "JORDEX" (any variation) →
    document_type = "MASTER BILL OF LADING"
    Example: "JORDEX SHIPPING & FORWARDING BV, AMBACHTSWEG 6, 3161 GL RHOON"

  RULE B: CONSIGNEE is any other company (NOT JORDEX) →
    document_type = "HOUSE BILL OF LADING"
    Example: "UNDIES INTERNATIONAL B.V., PROF. EYKMANWEG 21-23, 5144 ND WAALWIJK"

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
  - If the number is 3, THREE → bl_type = "ORIGINAL"
  - If the number is 0, 1, ZERO, ONE, NIL → bl_type = "SEA WAYBILL"
  - If the document header says "SEA WAYBILL" or "NON NEGOTIABLE" → bl_type = "SEA WAYBILL"
  - If no originals count is mentioned → bl_type = "ORIGINAL"

Output ONLY "ORIGINAL" or "SEA WAYBILL" as the bl_type value.
Do NOT output the raw text like "THREE(3)" or "0". Resolve it.

=====================================================================
STEP 4 — REFERENCE NUMBER (B/L NUMBER / MBL NUMBER)
=====================================================================

Extract the B/L or Waybill reference number from:
  "B/L No.", "BL No", "Sea Waybill No.", "Waybill No.", "Bill of Lading No."

CRITICAL — EXTRACT THE COMPLETE NUMBER WITH ITS PREFIX:
  - BL numbers almost always have a carrier prefix (letters) followed by digits
    or an alphanumeric sequence. The prefix is PART of the number.
  - Examples: HLCUSZX2604CGUJ4, OOLU2168544270, MAEU123456789, COSU6789012345,
              FSNBS2604286, SNKO123456789
  - The prefix may be printed ABOVE, BEFORE, or NEAR the number in smaller text.
  - Some BLs print the carrier code and number on separate lines:
      "HLCU" on one line, "SZX2604CGUJ4" on the next line.
      In that case, COMBINE them: reference_number = "HLCUSZX2604CGUJ4"
  - NEVER return just the numeric portion without the prefix.

WHERE TO FIND THE FULL NUMBER:
  - Look at the labeled field: "B/L No.", "BL No.", "Waybill No."
  - Check the document header/title area
  - Look near the top-right corner of the document
  - If you see only digits in the BL No field, scan NEARBY text for the prefix

SELF-CHECK: The reference_number should contain BOTH letters and digits.
  If you have only digits, you are likely missing the prefix — look harder.

=====================================================================
STEP 5 — CONTAINER NUMBER (STRICT ISO 6346 PATTERN)
=====================================================================

Container number = EXACTLY 4 UPPERCASE LETTERS + 7 DIGITS (total 11 characters).
Regex pattern: ^[A-Z]{4}[0-9]{7}$

SELF-CHECK BEFORE RETURNING: Count the characters in your container_no value.
  - It MUST have EXACTLY 4 letters then EXACTLY 7 digits. No more, no less.
  - If your value has more than 4 letters or more/fewer than 7 digits, it is WRONG.

VALID:   CAIU3221795 (C-A-I-U = 4 letters, 3221795 = 7 digits ✓)
VALID:   HLXU1215052, OOCU5376542, FANU1151538, TCLU7845231, ONEU6840355
INVALID: HLCUSZX2604CGUJ4 (7 letters + mixed — NOT a container, it is a BL reference!)
INVALID: OOLU2168544270 (10 digits — too many digits, this is a BL number)
INVALID: OOLJVU0265 (6 letters — too many letters)
INVALID: FSNBS2604286 (5 letters — this is a BL reference number, NOT a container)

COMMON MISTAKE — BL REFERENCE vs CONTAINER:
  - BL/booking references (like HLCUSZX2604CGUJ4, FSNBS2604286) often appear NEAR
    container numbers in parentheses or on the same line. They are NOT containers.
    They have 5+ letters or mixed letter/digit patterns in the digit section.
  - Container numbers are ALWAYS exactly 4 letters + 7 pure digits. No exceptions.

WHERE CONTAINERS APPEAR IN COMPLEX LINES:
  - Lines like: "(HLCUSZX2604CGUJ4) CAIU3221795/20' GP/HLK4005963/2700CTNS/19170.000KGS/29.300CBM"
    The parenthesized value (HLCUSZX2604CGUJ4) is a REFERENCE — ignore it.
    CAIU3221795 is the container (4 letters + 7 digits).
    After the first slash: 20' GP = container size/type.
    HLK4005963 = seal number.
    Remaining = package count, weight, volume for this container.
  - Extract: container_no=CAIU3221795, container_type=20GP, seal_no=HLK4005963,
    package_qty=2700, package_type=CTNS, gross_weight=19170.000 KGS, measurement=29.300 CBM

DISAMBIGUATION (SLASHES):
  - Container / Seal pattern: "YMMU1395094 / YMAU451322"
    → container_no=YMMU1395094, seal_no=YMAU451322
  - Container / Type / Seal pattern: "FSCU8898370/40HC/OOLLDJ0699"
    → container_no=FSCU8898370, container_type=40HC, seal_no=OOLLDJ0699
  - RULE: First value = container. If the second value is a size (e.g. 40HC, 20GP), it is the container_type, and the third value is the seal_no.
  - CRITICAL: Never extract a seal string as a separate container.

PACKAGE DETAILS (IMPORTANT):
  - Always scan the text immediately adjacent to, above, or below the container number to find `package_qty`, `package_type`, `gross_weight`, and `measurement` specific to THAT container. 
  - Do not leave them null if the document breaks them down per container!

SUMMARY / TOTAL ROWS — DO NOT CREATE CONTAINER ENTRIES FOR THESE:
  - BLs with multiple containers often have a TOTAL/SUMMARY row at the bottom
    that sums up weights and volumes across all containers.
  - Example: A line showing "36693.800 KGS" and "58.500 CBM" WITHOUT any
    container number (4 letters + 7 digits) is a TOTAL row.
  - DO NOT create a container entry for total/summary rows.
  - ONLY create container entries for rows/lines that have a valid container number.
  - Put total weight/volume in the top-level total_gross_weight and total_measurement.

SCAN ALL PAGES:
  - Container numbers often appear on PAGE 2 or later pages, not on page 1.
  - Page 1 may show description, qty, weight WITHOUT the container number.
  - YOU MUST READ EVERY PAGE before concluding container_no is null.
  - Look in: "MARKS AND NUMBERS", "DESCRIPTION OF GOODS", "PARTICULARS".
  - If you find a container number on ANY page, use it.
  - NEVER return container_no as null if a valid 4-letter+7-digit pattern exists anywhere.

=====================================================================
STEP 6 — DESCRIPTION / GOODS CLEANUP
=====================================================================

Extract ONLY the actual product/commodity names. The goal is a clean, meaningful
description like "NOTEBOOK & EXERCISE BOOK" or "GARMENTS" or "AUTO PARTS".

STRIP ALL OF THESE (they are NOT product names):
  - Shipping instructions: "SHIPPER'S LOAD & COUNT", "SAID TO CONTAIN",
    "SHIPPER'S WEIGHT", "LOADED ON BOARD", "CLEAN ON BOARD"
  - Movement types: "CY-CY", "CY/CY", "FCL/FCL", "CFS/CFS", "DOOR/DOOR"
  - Container specs: "1X20'GP", "2X40'HQ", "S/40", "IN 20' CONTAINER"
  - Quantity prefixes: "10PCS", "500 CTNS", "2020 CARTONS" (put these in package_qty/type instead)
  - Reference numbers: "MBL#...", "S/C:...", "NAC NO:...", "PO#...", "REF NO:..."
  - Contact info: "TEL:...", "EMAIL:...", "FAX:..."
  - HS codes: "HS CODE: 4820.10", "H.S. CODE 8471"
  - Freight terms: "FREIGHT PREPAID", "FREIGHT COLLECT", "*FREIGHT COLLECT*"
  - Boilerplate: "AS PER ATTACHED LIST", "SEE ATTACHED", "DETAILS AS ABOVE"
    (if this appears, read the attachment pages for actual product names)
  - Container/seal numbers and BL references that appear in the description area
  - Weight/volume text: "19170.000KGS", "29.300CBM" (put these in weight/measurement)

WHAT TO KEEP — examples of valid goods descriptions:
  - NOTEBOOK & EXERCISE BOOK
  - GARMENTS, TEXTILES
  - FURNITURE, WOODEN FURNITURE
  - AUTO PARTS, SPARE PARTS
  - ELECTRONIC COMPONENTS
  - TOYS, PLASTIC TOYS
  - FOOTWEAR, SHOES
  - CERAMIC TILES
  - HOUSEHOLD GOODS

FOR "description" FIELD: The main product name(s) for the entire shipment.
FOR EACH container's "goods_description": The product name for that specific container.
If all containers carry the same goods, each container's goods_description = same value.

SEPARATOR: Use "\n" (newline) between multiple product names.
  Example: "NOTEBOOK\nEXERCISE BOOK" not "NOTEBOOK&EXERCISE BOOK"

=====================================================================
STEP 7 — REMAINING RULES
=====================================================================

1. ALL text MUST be UPPERCASE, EXCEPT for "description" and "goods_description" which MUST be in lowercase.
2. FREIGHT: Extract ONLY from the Freight box (PREPAID or COLLECT). Return null if unclear.
3. NOTIFY: If "Same as Consignee", copy full consignee details.
4. ATTACH LIST: If present, extract goods/weights/CBM from its rows.
5. CARRIER: From logo, header, or SIGNED BY section.

=====================================================================
OUTPUT — ONLY for BL documents. Return valid JSON. No markdown. No backticks.
=====================================================================

CRITICAL RULES FOR CONTAINERS:
  1. EVERY container object MUST include ALL 8 fields listed below.
     If a value is not found in the document, set it to null — do NOT omit the field.
  2. SINGLE-CONTAINER BLs: When there is only ONE container, the package qty,
     package type, gross weight, and measurement are usually shown in the HEADER
     row (e.g. "No. of Pkgs" / "Gross Weight" / "Measurement" columns at the top).
     You MUST copy those values into the container's fields.
     Example: If header shows "3,288 CTNS" and "9,690.000KGS" and "67.006CBM"
     and there is one container TGBU8922277, then that container gets:
       package_qty=3288, package_type=CTNS, gross_weight=9690.000 KGS,
       measurement=67.006 CBM
  3. Container type format: "20GP", "40HC", "40HQ", "20RF", "45HC" etc.
     Normalize "40'HC" → "40HC", "20' GP" → "20GP" (remove apostrophes and spaces).
  4. Container numbers and seal numbers often appear separated by slashes in a long string:
     "MRSU7913121/ML-ID1040329/40HC/1000BAGS/25750.000KGS/43.000CBM"
     In this example, the FIRST matching pattern (4 letters+7 digits) is container_no=MRSU7913121, and seal_no=ML-ID1040329.

{
  "document_type": "MASTER BILL OF LADING or HOUSE BILL OF LADING",
  "bl_type": "ORIGINAL or SEA WAYBILL (resolved value only, not raw text)",
  "reference_number": "B/L OR WAYBILL NUMBER",
  "shipper": "FULL SHIPPER NAME AND ADDRESS",
  "consignee": "FULL CONSIGNEE NAME AND ADDRESS",
  "notify": "FULL NOTIFY PARTY NAME AND ADDRESS (if data is same as consignee then place the consinee address here)",
  "carrier_name": "CARRIER NAME (only need to take Name  example : 
HAPAG-LLOYD AKTIENGESELLSCHAFT, HAMBURG Then Value only HAPAG-LLOYD only)",
  "carrier_code": "JORDEX DROPDOWN CODE e.g. OOLU for OOCL, HAPAG LLOYD for Hapag-Lloyd, MSCU for MSC, ZIMU for ZIM",
  "vessel": "VESSEL NAME",
  "voyage": "VOYAGE NUMBER",
  "port_of_loading": "PORT NAME",
  "port_of_discharge": "PORT NAME",
  "place_of_delivery": "PLACE NAME OR null",
  "marks_and_numbers": "MARKS DETAILS OR N/M",
  "number_and_type_of_packages": "e.g. 2020 CARTONS",
  "description": "clean commodity names in lowercase only",
  "shipped_on_board_date": "YYYY-MM-DD",
  "containers": [
    {
      "container_no": "4 LETTERS + 7 DIGITS (e.g. OOCU5376542) — REQUIRED",
      "container_type": "e.g. 40HC — or null if not found",
      "seal_no": "SEAL NUMBER — or null if not found",
      "package_qty": "NUMBER ONLY (e.g. 2020) — or null if not found",
      "package_type": "e.g. CARTONS — or null if not found",
      "gross_weight": "e.g. 13736.000 KGS — or null if not found",
      "measurement": "e.g. 67.590 CBM — or null if not found",
      "goods_description": "clean commodity names in lowercase — or null if not found"
    }
  ],
  "total_gross_weight": "TOTAL WEIGHT",
  "total_measurement": "TOTAL VOLUME",
  "freight_charges": "PREPAID OR COLLECT OR null",
  "issue_place_and_date": "PLACE AND DATE"
}"""


def docx_to_pdf(docx_path: str) -> str:
    """Convert .docx or .doc to .pdf locally and return the PDF path."""
    import platform
    import subprocess
    from pathlib import Path
    
    pdf_path = str(Path(docx_path).with_suffix(".pdf"))
    system = platform.system()

    if system == "Windows":
        try:
            from docx2pdf import convert
            convert(docx_path, pdf_path)
            return pdf_path
        except Exception:
            pass

        lo_paths = [
            r"C:\Program Files\LibreOffice\program\soffice.exe",
            r"C:\Program Files (x86)\LibreOffice\program\soffice.exe",
        ]
        soffice = next((p for p in lo_paths if os.path.isfile(p)), None)
        if not soffice:
            raise RuntimeError("Neither docx2pdf nor LibreOffice found for conversion.")
    else:
        soffice = "libreoffice"

    out_dir = str(Path(docx_path).parent)
    cmd = [
        soffice,
        "--headless",
        "--convert-to", "pdf",
        "--outdir", out_dir,
        docx_path,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    
    if result.returncode != 0:
        raise RuntimeError(f"Conversion failed: {result.stderr}")
    
    return pdf_path


def extract_document(pdf_path):
    """
    Send a PDF to Gemini and get structured JSON extraction.

    Returns:
        dict with extraction results.
        If skip=True, document is non-BL and should not generate output JSON.
    """
    if not GEMINI_API_KEY:
        raise ValueError("GEMINI_API_KEY not set in .env")

    file_ext = os.path.splitext(pdf_path)[1].lower()
    if file_ext in [".docx", ".doc"]:
        try:
            pdf_path = docx_to_pdf(pdf_path)
        except Exception as e:
            import logging
            log = logging.getLogger("extractor")
            log.error("Failed to convert word document to PDF: %s", e)
            raise

    with open(pdf_path, "rb") as f:
        pdf_bytes = f.read()

    pdf_base64 = base64.standard_b64encode(pdf_bytes).decode("utf-8")

    # Detect mime type from file extension
    file_ext = os.path.splitext(pdf_path)[1].lower()
    MIME_TYPES = {
        ".pdf":  "application/pdf",
        ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ".doc":  "application/msword",
        ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    }
    mime_type = MIME_TYPES.get(file_ext, "application/pdf")

    payload = {
        "contents": [
            {
                "parts": [
                    {
                        "inline_data": {
                            "mime_type": mime_type,
                            "data": pdf_base64,
                        }
                    },
                    {
                        "text": EXTRACTION_PROMPT,
                    },
                ]
            }
        ],
        "generationConfig": {
            "temperature": 0.1,
            "maxOutputTokens": 8192,
            "responseMimeType": "application/json",
        },
    }

    MODEL = "gemini-2.5-flash-lite"
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL}:generateContent?key={GEMINI_API_KEY}"

    headers = {"Content-Type": "application/json"}
    resp = requests.post(url, headers=headers, json=payload, timeout=120)

    # ── Fallback model on rate limit / overload ──
    if resp.status_code in (429, 503, 500):
        fallback_url = f"https://generativelanguage.googleapis.com/v1beta/models/{FALLBACK_MODEL}:generateContent?key={GEMINI_API_KEY}"
        resp = requests.post(fallback_url, headers=headers, json=payload, timeout=120)

    if resp.status_code != 200:
        raise RuntimeError(f"Gemini API error {resp.status_code}: {resp.text[:500]}")

    result = resp.json()

    try:
        text = result["candidates"][0]["content"]["parts"][0]["text"]
    except (KeyError, IndexError) as e:
        raise RuntimeError(f"Unexpected Gemini response structure: {e}")

    # Clean markdown fences if present
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else text[3:]
    if text.endswith("```"):
        text = text[:-3]
    text = text.strip()

    try:
        parsed = json.loads(text, strict=False)
    except json.JSONDecodeError:
        import re
        text = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f]', '', text)
        try:
            parsed = json.loads(text, strict=False)
        except json.JSONDecodeError as e:
            raise RuntimeError(f"Failed to parse Gemini JSON: {e}\nRaw: {text[:500]}")

    # ── Non-BL document: return early ──
    if parsed.get("skip"):
        return parsed

    # ── Post-processing safety net ──
    parsed = _fix_document_type(parsed, pdf_path)
    parsed = _fix_bl_type(parsed)
    parsed = _fix_carrier_code(parsed)
    parsed = _fix_container_numbers(parsed)

    # ── HBL container retry: if HBL and any containers missing/invalid, retry up to 3 times ──
    retry_count = 0
    while _has_missing_containers(parsed) and retry_count < 3:
        parsed = _retry_container_extraction(pdf_path, parsed)
        retry_count += 1

    # ── MBL retry: if reference number looks incomplete, retry once ──
    if _has_bad_reference_number(parsed):
        parsed = _retry_mbl_extraction(pdf_path, parsed)

    return parsed


def _fix_carrier_code(data):
    """Map carrier_name to Jordex dropdown search code."""
    name = (data.get("carrier_name") or "").upper().strip()
    code = None
    for key, val in CARRIER_NAME_TO_CODE.items():
        if key.upper() in name or name in key.upper():
            code = val
            break
    data["carrier_code"] = code or name  # fallback to name itself
    return data


def _fix_document_type(data, pdf_path=""):
    """
    Enforce MBL/HBL based on filename first, then consignee content.
    This overrides whatever Gemini returned if it got confused by headers.
    """
    import os
    filename = os.path.basename(pdf_path).upper() if pdf_path else ""
    if "MASTER" in filename or "MBL" in filename:
        data["document_type"] = "MASTER BILL OF LADING"
        return data
    elif "HOUSE" in filename or "HBL" in filename:
        data["document_type"] = "HOUSE BILL OF LADING"
        return data

    consignee = (data.get("consignee") or "").upper()
    doc_type = (data.get("document_type") or "").upper()

    # Primary rule: JORDEX in consignee = MBL
    if "JORDEX" in consignee:
        data["document_type"] = "MASTER BILL OF LADING"
    elif consignee.strip():
        # Consignee exists and is not JORDEX = HBL
        data["document_type"] = "HOUSE BILL OF LADING"
    else:
        # No consignee extracted — normalize whatever Gemini returned
        if "MASTER" in doc_type or "MBL" in doc_type:
            data["document_type"] = "MASTER BILL OF LADING"
        else:
            data["document_type"] = "HOUSE BILL OF LADING"

    return data


def _fix_bl_type(data):
    """
    Enforce bl_type as resolved value: ORIGINAL or SEA WAYBILL only.
    Uses bl_type from Gemini, validates and normalizes it.
    """
    bl_type = (data.get("bl_type") or "").upper().strip()

    # Resolve to exactly one of two values
    if data.get("document_type", "").upper() == "HOUSE BILL OF LADING":
        data["bl_type"] = "ORIGINAL"
    elif "WAYBILL" in bl_type or "NON NEGOTIABLE" in bl_type:
        data["bl_type"] = "SEA WAYBILL"
    elif "ORIGINAL" in bl_type or not bl_type:
        data["bl_type"] = "ORIGINAL"
    else:
        # If Gemini put raw text like "THREE(3)" or "0" in bl_type
        raw = bl_type
        if any(z in raw for z in ("0", "ZERO", "NIL", "ONE", "1")):
            data["bl_type"] = "SEA WAYBILL"
        else:
            data["bl_type"] = "ORIGINAL"

    return data


def _fix_container_numbers(data):
    """
    Ensure containers is a list of objects.
    """
    # Ensure containers is a list of objects
    if "containers" not in data:
        data["containers"] = []
    
    # If containers is a number (sometimes Gemini puts count instead of list)
    if isinstance(data["containers"], (int, float)):
        data["containers"] = [{"container_no": "NONE"}] * int(data["containers"])
    
    if not isinstance(data["containers"], list):
        data["containers"] = []

    clean_list = []
    for container in data["containers"]:
        if not isinstance(container, dict):
            # If it's just a string, wrap it
            container = {"container_no": str(container)}
            
        raw_no = str(container.get("container_no", container.get("Container_No", ""))).upper().strip()
        
        # Strip common noise
        if raw_no in ["NONE", "NULL", "N/A", ""]:
            continue

        match = CONTAINER_PATTERN.search(raw_no)
        if match:
            container["container_no"] = match.group(0)
            clean_list.append(container)
        # else: discard non-ISO container numbers — prompt should have caught this

    # ── Scavenge from marks_and_numbers if clean_list is empty ──
    if not clean_list and data.get("marks_and_numbers"):
        marks = str(data["marks_and_numbers"]).upper()
        # Find all matches for 4 letters + 7 digits
        matches = CONTAINER_PATTERN.findall(marks)
        for m_no in matches:
            clean_list.append({
                "container_no": m_no,
                "container_type": "UNKNOWN",
                "goods_description": data.get("description", "")
            })

    data["containers"] = clean_list

    # ── Ensure all 8 fields + fill from top-level for single-container ──
    data = _normalize_container_fields(data)

    return data


def _normalize_container_fields(data: dict) -> dict:
    """
    Ensure every container has all 8 required fields (null if missing).
    For single-container BLs, copy top-level weight/qty/measurement into the container.
    Normalize container_type format.
    """
    REQUIRED_FIELDS = [
        "container_no", "container_type", "seal_no", "package_qty",
        "package_type", "gross_weight", "measurement", "goods_description"
    ]

    containers = data.get("containers", [])

    for c in containers:
        # Fill missing fields with null
        for field in REQUIRED_FIELDS:
            if field not in c:
                c[field] = None

        # Normalize container_type: "40'HC" → "40HC", "20' GP" → "20GP"
        ct = c.get("container_type")
        if ct and isinstance(ct, str):
            ct = ct.replace("'", "").replace("'", "").replace(" ", "").upper()
            c["container_type"] = ct

    # Single-container BL: copy top-level values into the container if missing
    if len(containers) == 1:
        c = containers[0]

        # gross_weight from total_gross_weight
        if not c.get("gross_weight") and data.get("total_gross_weight"):
            c["gross_weight"] = data["total_gross_weight"]

        # measurement from total_measurement
        if not c.get("measurement") and data.get("total_measurement"):
            c["measurement"] = data["total_measurement"]

        # package_qty and package_type from number_and_type_of_packages
        pkg_str = data.get("number_and_type_of_packages", "") or ""
        if pkg_str and (not c.get("package_qty") or not c.get("package_type")):
            # Parse "4,320 CTNS" or "2020 CARTONS" etc.
            pkg_match = re.match(r'([\d,. ]+)\s+(.+)', pkg_str.strip())
            if pkg_match:
                if not c.get("package_qty"):
                    c["package_qty"] = pkg_match.group(1).replace(",", "").replace(" ", "").strip()
                if not c.get("package_type"):
                    c["package_type"] = pkg_match.group(2).strip().upper()

        # goods_description from description
        if not c.get("goods_description") and data.get("description"):
            c["goods_description"] = data["description"]

        # seal_no: try to extract from marks_and_numbers if still missing
        if not c.get("seal_no") and c.get("container_no") and data.get("marks_and_numbers"):
            marks = str(data["marks_and_numbers"])
            cno = c["container_no"]
            # Look for pattern: CONTAINER/SEAL  (e.g. TRHU7991425/00LLBF9712)
            seal_match = re.search(re.escape(cno) + r'[/\\\\]([A-Z0-9]+)', marks, re.IGNORECASE)
            if seal_match:
                c["seal_no"] = seal_match.group(1).upper()

    data["containers"] = containers
    return data


def _has_missing_containers(data: dict) -> bool:
    """True when BL has no containers or any container lacks a valid ISO number."""
    containers = data.get("containers", [])
    if not containers:
        return True
    # Retry if ANY container lacks a valid ISO 6346 number
    return any(
        not CONTAINER_PATTERN.fullmatch(
            str(c.get("container_no", "")).upper().strip()
        )
        for c in containers
    )


def _retry_container_extraction(pdf_path: str, current_data: dict) -> dict:
    """
    Retry extraction focused only on finding container numbers.
    Sends the PDF again with the current JSON and a targeted container-finding prompt.
    """
    if not GEMINI_API_KEY:
        return current_data

    import logging
    log = logging.getLogger("extractor")
    log.info("HBL has no container numbers — retrying with focused container prompt...")

    try:
        with open(pdf_path, "rb") as f:
            pdf_bytes = f.read()
        pdf_base64 = base64.standard_b64encode(pdf_bytes).decode("utf-8")
    except Exception as e:
        log.error("Could not read PDF for retry: %s", e)
        return current_data

    # Serialize current JSON cleanly for the prompt
    current_json_str = json.dumps(current_data, indent=2)
    retry_prompt = CONTAINER_RETRY_PROMPT.replace("{current_json}", current_json_str)

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
                    {
                        "text": retry_prompt,
                    },
                ]
            }
        ],
        "generationConfig": {
            "temperature": 0.1,
            "maxOutputTokens": 8192,
            "responseMimeType": "application/json",
        },
    }

    MODEL = "gemini-2.5-flash-lite"
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL}:generateContent?key={GEMINI_API_KEY}"
    headers = {"Content-Type": "application/json"}

    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=120)
        if resp.status_code in (429, 503, 500):
            fallback_url = f"https://generativelanguage.googleapis.com/v1beta/models/{FALLBACK_MODEL}:generateContent?key={GEMINI_API_KEY}"
            resp = requests.post(fallback_url, headers=headers, json=payload, timeout=120)

        if resp.status_code != 200:
            log.warning("Container retry API error %s — keeping original data", resp.status_code)
            return current_data

        result = resp.json()
        text = result["candidates"][0]["content"]["parts"][0]["text"].strip()

        # Strip markdown fences if present
        if text.startswith("```"):
            text = text.split("\n", 1)[1] if "\n" in text else text[3:]
        if text.endswith("```"):
            text = text[:-3]
        text = text.strip()

        retry_parsed = json.loads(text)

        # Validate the retry actually found containers
        retry_containers = retry_parsed.get("containers", [])
        found_any = any(
            CONTAINER_PATTERN.search(str(c.get("container_no", "")).upper())
            for c in retry_containers
        )

        if found_any:
            log.info("Container retry SUCCESS — found %d container(s)", len(retry_containers))
            # Apply same post-processing to the retry result
            retry_parsed = _fix_document_type(retry_parsed)
            retry_parsed = _fix_bl_type(retry_parsed)
            retry_parsed = _fix_carrier_code(retry_parsed)
            retry_parsed = _fix_container_numbers(retry_parsed)
            return retry_parsed
        else:
            log.warning("Container retry found no valid container numbers — keeping original")
            return current_data

    except (json.JSONDecodeError, KeyError, IndexError, Exception) as e:
        log.warning("Container retry failed: %s — keeping original", e)
        return current_data


def _has_bad_reference_number(data: dict) -> bool:
    """True when reference_number is missing, digits-only, or suspiciously short."""
    ref = (data.get("reference_number") or "").strip()
    if not ref:
        return True
    # If it's all digits — prefix is likely missing
    if ref.isdigit():
        return True
    # If it's too short (less than 6 chars) — likely incomplete
    if len(ref) < 6:
        return True
    return False


def _retry_mbl_extraction(pdf_path: str, current_data: dict) -> dict:
    """
    Retry extraction focused only on finding the correct reference number (B/L number).
    Sends the PDF again with the current JSON and a targeted MBL-finding prompt.
    """
    if not GEMINI_API_KEY:
        return current_data

    import logging
    log = logging.getLogger("extractor")
    current_ref = (current_data.get("reference_number") or "")
    log.info("Reference number looks incomplete ('%s') — retrying with focused MBL prompt...", current_ref)

    try:
        with open(pdf_path, "rb") as f:
            pdf_bytes = f.read()
        pdf_base64 = base64.standard_b64encode(pdf_bytes).decode("utf-8")
    except Exception as e:
        log.error("Could not read PDF for MBL retry: %s", e)
        return current_data

    # Serialize current JSON cleanly for the prompt
    current_json_str = json.dumps(current_data, indent=2)
    retry_prompt = MBL_RETRY_PROMPT.replace("{current_ref}", current_ref).replace("{current_json}", current_json_str)

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
                    {
                        "text": retry_prompt,
                    },
                ]
            }
        ],
        "generationConfig": {
            "temperature": 0.1,
            "maxOutputTokens": 8192,
            "responseMimeType": "application/json",
        },
    }

    MODEL = "gemini-2.5-flash-lite"
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL}:generateContent?key={GEMINI_API_KEY}"
    headers = {"Content-Type": "application/json"}

    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=120)
        if resp.status_code in (429, 503, 500):
            fallback_url = f"https://generativelanguage.googleapis.com/v1beta/models/{FALLBACK_MODEL}:generateContent?key={GEMINI_API_KEY}"
            resp = requests.post(fallback_url, headers=headers, json=payload, timeout=120)

        if resp.status_code != 200:
            log.warning("MBL retry API error %s — keeping original data", resp.status_code)
            return current_data

        result = resp.json()
        text = result["candidates"][0]["content"]["parts"][0]["text"].strip()

        # Strip markdown fences if present
        if text.startswith("```"):
            text = text.split("\n", 1)[1] if "\n" in text else text[3:]
        if text.endswith("```"):
            text = text[:-3]
        text = text.strip()

        retry_parsed = json.loads(text)

        # Validate the retry found a better reference number
        new_ref = (retry_parsed.get("reference_number") or "").strip()
        if new_ref and len(new_ref) >= 6 and not new_ref.isdigit():
            log.info("MBL retry SUCCESS — reference_number: '%s' → '%s'", current_ref, new_ref)
            # Only update reference_number, keep everything else from original
            current_data["reference_number"] = new_ref
            return current_data
        else:
            log.warning("MBL retry did not improve reference_number — keeping original")
            return current_data

    except (json.JSONDecodeError, KeyError, IndexError, Exception) as e:
        log.warning("MBL retry failed: %s — keeping original", e)
        return current_data


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("Usage: python extractor.py <path_to_pdf>")
        sys.exit(1)

    result = extract_document(sys.argv[1])
    print(json.dumps(result, indent=2))
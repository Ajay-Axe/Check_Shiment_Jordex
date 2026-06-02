import os
import json
from pathlib import Path

from dotenv import load_dotenv
from google import genai
from google.genai import types

# ==========================
# Load Environment Variables
# ==========================
load_dotenv()

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-pro")

if not GEMINI_API_KEY:
    raise ValueError("GEMINI_API_KEY not found in .env file")

# ==========================
# Input DOCX File
# ==========================
DOCX_FILE = r"F:\Axe Global\Check Shipment\Shipments\28-May-2026__OI2614941\Master_BL__2327158760-MBL__no-date.docx"

# Output JSON File
OUTPUT_JSON = Path(DOCX_FILE).parent / "Gemini_result.json"

# ==========================
# Gemini Client
# ==========================
client = genai.Client(api_key=GEMINI_API_KEY)

# ==========================
# Upload Document
# ==========================
print(f"Uploading: {DOCX_FILE}")

uploaded_file = client.files.upload(
    file=DOCX_FILE
)

# ==========================
# Prompt
# ==========================
prompt = """
You are an expert logistics and shipping document parser.

Analyze the attached document and extract ALL shipment-related information.

Return ONLY valid JSON.

Requirements:
1. Extract every identifiable field.
2. Preserve original values exactly.
3. Include missing fields as null.
4. Group related information logically.

Suggested structure:

{
  "document_type": "",
  "master_bl_number": "",
  "house_bl_number": "",
  "booking_number": "",
  "container_numbers": [],
  "seal_numbers": [],
  "shipper": {},
  "consignee": {},
  "notify_party": {},
  "carrier": "",
  "vessel": "",
  "voyage": "",
  "port_of_loading": "",
  "port_of_discharge": "",
  "place_of_receipt": "",
  "place_of_delivery": "",
  "gross_weight": "",
  "net_weight": "",
  "packages": "",
  "commodity_description": "",
  "freight_terms": "",
  "issue_date": "",
  "shipment_date": "",
  "additional_details": {}
}

Return ONLY JSON.
No markdown.
No explanation.
"""

# ==========================
# Generate Response
# ==========================
response = client.models.generate_content(
    model=GEMINI_MODEL,
    contents=[
        uploaded_file,
        prompt
    ],
    config=types.GenerateContentConfig(
        temperature=0
    )
)

response_text = response.text.strip()

# ==========================
# Parse JSON Safely
# ==========================
try:
    result_json = json.loads(response_text)
except Exception:
    result_json = {
        "raw_response": response_text
    }

# ==========================
# Save JSON
# ==========================
with open(OUTPUT_JSON, "w", encoding="utf-8") as f:
    json.dump(result_json, f, indent=4, ensure_ascii=False)

print(f"\n✅ JSON saved:")
print(OUTPUT_JSON)
"""
Shipment_Process.py — Autonomous Shipment Processing Engine (v5)
================================================================
Complete rewrite. New architecture:

Flow per shipment:
  1. Open shipment → Documents tab → check HBL/MBL exist
     - No docs → no_doc.json, skip
     - Docs exist → download originals (dedup logic)
  2. Run extractor.py on PDFs → HBL.json, MBL.json
  3. DOM-scrape Jordex tabs (import_process style):
     - Parties tab: read Shipper, Consignee, Notify
     - Carrier tab: read Carrier, Vessel, MBL/HBL Type/Number, Payment Terms
     - Cargo tab: read per-container details
  4. Compare scraped data vs extracted JSON (string or AI mode)
  5. Carrier tracking (parallel per container)
  6. Routing: DOM-scrape View Routing per container, compare vs tracking
  7. Save Comparison_Result.json

Orchestration:
  - Date window scanning with page-first pagination
  - 15-shipment cap per run (excludes no_doc)
  - No_doc toggle: when ON, processes only no_doc.json entries
  - AI comparison toggle: when ON, uses Gemini for intelligent matching

Update queue (called from Documents page):
  - Single browser, sequential updates
  - Search bar → open shipment → fill via DOM → save → back → next
"""

import sys
import os
import re
import json
import logging
import threading
import time
import concurrent.futures
from datetime import datetime, timedelta
from urllib.parse import unquote
from pathlib import Path
import shutil
from playwright.sync_api import Page, TimeoutError as PlaywrightTimeoutError

try:
    from Login import main as launch_and_login, apply_filters, apply_date_filter
except ImportError:
    print("Error: Login.py not found.")
    sys.exit(1)

try:
    import extractor
except ImportError:
    extractor = None

try:
    import Tracking
except ImportError:
    Tracking = None

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("shipment_process")


# ═══════════════════════════════════════════════════════════════════════
#  CONFIGURATION & LOGGING SETUP
# ═══════════════════════════════════════════════════════════════════════
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOGS_ROOT = os.path.join(BASE_DIR, "Logs")
GLOBAL_LOGS_DIR = os.path.join(LOGS_ROOT, "Global")
os.makedirs(GLOBAL_LOGS_DIR, exist_ok=True)

# Add global file handler for standard logs
log_file_path = os.path.join(GLOBAL_LOGS_DIR, f"app_{datetime.now().strftime('%Y-%m-%d')}.log")
file_handler = logging.FileHandler(log_file_path, encoding='utf-8')
file_handler.setFormatter(logging.Formatter("%(asctime)s  %(levelname)-8s  %(message)s", "%H:%M:%S"))
logging.getLogger().addHandler(file_handler)

oi_logs_lock = threading.Lock()
def log_oi_event(ref_no: str, event_type: str, message: str = "", data: dict = None):
    """Updates the central Logs/oi_logs.json file."""
    if not ref_no:
        return
    json_path = os.path.join(LOGS_ROOT, "oi_logs.json")
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    new_event = {"time": timestamp, "type": event_type, "message": message}
    if data:
        new_event["data"] = data
        
    with oi_logs_lock:
        logs_data = []
        if os.path.exists(json_path):
            try:
                with open(json_path, "r", encoding="utf-8") as f:
                    logs_data = json.load(f)
            except Exception:
                pass
                
        # Find existing OI or create new
        oi_entry = next((item for item in logs_data if item.get("ref_no") == ref_no), None)
        if not oi_entry:
            oi_entry = {
                "ref_no": ref_no,
                "first_seen": timestamp,
                "last_updated": timestamp,
                "status": "processing",
                "events": []
            }
            logs_data.append(oi_entry)
            
        oi_entry["last_updated"] = timestamp
        oi_entry["events"].append(new_event)
        
        # Optionally update status based on event_type
        if event_type == "status_change" and message:
            oi_entry["status"] = message
            
        try:
            with open(json_path, "w", encoding="utf-8") as f:
                json.dump(logs_data, f, indent=2)
        except Exception as e:
            log.error(f"Failed to write to oi_logs.json: {e}")

SESSION_ID      = datetime.now().strftime("%Y%m%d_%H%M%S")
TRACKING_FILE   = "checked_shipments.json"
NO_DOC_FILE     = "no_doc.json"
SETTINGS_FILE   = "process_settings.json"
SHIPMENTS_ROOT  = os.path.join(os.path.dirname(os.path.abspath(__file__)), "Shipments")
MAX_PROCESSED   = 15
DATE_WINDOW     = 5

REF_PATTERN = re.compile(r'\b(OI\d+|01PKG\d+)\b', re.IGNORECASE)
DT_PATTERN  = re.compile(
    r'_DT(\d{4})_(\d{2})_(\d{2})_(\d{2})_(\d{2})_(\d{2})', re.IGNORECASE
)

NOISE_PREFIXES = [
    r'^Draft[\s_]+', r'^2\.\s*PKG[\s_]+', r'^PKG[\s_]+', r'^2\.\s*',
    r'^HBL[\s_]+', r'^MBL[\s_]+', r'^House\s*BL[\s_]+', r'^Master\s*BL[\s_]+',
]

SUPPORTED_CARRIERS = [
    "MSC", "MAERSK", "ONE", "HAPAG", "YANG MING",
    "EVERGREEN", "COSCO", "HMM", "OOCL",
]

# Container type mapping (carrier codes → Jordex dropdown values)
CONTAINER_TYPE_MAP = {
    "20GP": "20' General Purpose", "20G0": "20' General Purpose",
    "20G1": "20' General Purpose",
    "40GP": "40' General Purpose", "40G0": "40' General Purpose",
    "40G1": "40' General Purpose",
    "40HC": "40' High Cube", "40H0": "40' High Cube",
    "45HC": "45' High Cube",
    "20RF": "20' Reefer", "40RF": "40' Reefer",
    "40RH": "40' Reefer High Cube",
    "20OT": "20' Open Top", "40OT": "40' Open Top",
    "20FR": "20' Flat Rack", "40FR": "40' Flat Rack",
    "40HQ": "40' High Cube",
}

PACKAGE_TYPE_MAP = {
    "PKG": "Package", "PKGS": "Package", "PK": "Package",
    "PACKAGE": "Package", "PACKAGES": "Package",
    "CTN": "Carton", "CTNS": "Carton", "CARTONS": "Carton", "CARTON": "Carton", "CARTON(S)": "Carton",
    "PLT": "Pallet", "PLTS": "Pallet", "PALLETS": "Pallet", "PALLET": "Pallet",
    "BOX": "Box", "BOXES": "Box",
    "CRT": "Crate", "CRATES": "Crate", "CRATE": "Crate",
    "DRM": "Drum", "DRUMS": "Drum", "DRUM": "Drum",
    "BAG": "Bag", "BAGS": "Bag", "FLASHBAG": "Bag", "FLASH BAG": "Bag",
    "SKD": "Skid", "SKIDS": "Skid", "SKID": "Skid",
    "UNT": "Unit", "UNITS": "Unit", "UNIT": "Unit",
    "PCS": "Pieces", "PIECES": "Pieces", "PIECE": "Pieces",
    "ROLL": "Roll", "ROLLS": "Roll",
    "BUNDLE": "Bundle", "BUNDLES": "Bundle", "BDL": "Bundle",
    "SET": "Set", "SETS": "Set",
    "COIL": "Coil", "COILS": "Coil",
    "SACK": "Sack", "SACKS": "Sack",
    "BARREL": "Barrel", "BARRELS": "Barrel", "BRL": "Barrel",
    "REEL": "Reel", "REELS": "Reel",
    "CASE": "Case", "CASES": "Case", "CS": "Case",
    "PAIL": "Pail", "PAILS": "Pail",
    "TRAY": "Tray", "TRAYS": "Tray",
    "BALE": "Bale compressed", "BALES": "Bale compressed", "BCL": "Bale compressed",
    "IBC": "Intermediate bulk container",
    "TNK": "Tank cylindrical",
    "CONTAINER": "Container", "CONTAINERS": "Container",
    "COLLI": "Colli",
}



# ═══════════════════════════════════════════════════════════════════════
#  BROWSER POOL
# ═══════════════════════════════════════════════════════════════════════

SESSION_DIRS = [
    os.getenv("SESSION_DIR", "./session_data/main"),
    os.getenv("UPDATE_SESSION_DIR_1", "./session_data/update_1"),
    os.getenv("UPDATE_SESSION_DIR_2", "./session_data/update_2"),
    os.getenv("UPDATE_SESSION_DIR_3", "./session_data/update_3"),
]
POOL_SIZE = 4


class BrowserPool:
    """
    Manages up to POOL_SIZE persistent browsers for both orchestrator and updates.
    Each slot has its own isolated session dir, page reference, and lock.
    """

    def __init__(self):
        self._slots = [
            {
                "id": i,
                "session_dir": SESSION_DIRS[i],
                "page": None,
                "busy": False,
                "lock": threading.Lock(),
            }
            for i in range(POOL_SIZE)
        ]
        self._pool_lock = threading.Lock()

    def _launch_slot(self, slot: dict) -> bool:
        """Launch browser for a slot."""
        try:
            page = launch_and_login(session_dir=slot["session_dir"], headless=False)
            slot["page"] = page
            log.info("Browser %d ready.", slot["id"] + 1)
            return True
        except Exception as e:
            log.error("Failed to launch browser %d: %s", slot["id"] + 1, e)
            slot["page"] = None
            return False

    def _is_page_alive(self, page) -> bool:
        """Check if a page/browser is still usable."""
        if page is None:
            return False
        try:
            _ = page.url
            return True
        except Exception:
            return False

    def acquire(self) -> dict | None:
        """
        Get a free slot (blocks up to 60s waiting for one).
        Returns the slot dict or None if none available.
        """
        for _ in range(120):  # wait up to 60s
            with self._pool_lock:
                for slot in self._slots:
                    if not slot["busy"]:
                        slot["busy"] = True
                        return slot
            threading.Event().wait(0.5)
        log.warning("Browser pool: no free slot after 60s")
        return None

    def release(self, slot: dict):
        """Mark a slot as free."""
        with self._pool_lock:
            slot["busy"] = False

    def ensure_slot_ready(self, slot: dict) -> bool:
        """Ensure the slot has a live browser, launching if needed."""
        if self._is_page_alive(slot["page"]):
            return True
        log.info("Update browser %d not alive — relaunching...", slot["id"] + 1)
        return self._launch_slot(slot)

    def shutdown(self):
        """Close all update browsers."""
        for slot in self._slots:
            try:
                if slot["page"] is not None:
                    slot["page"].context.close()
                    if hasattr(slot["page"], "_pw_instance"):
                        slot["page"]._pw_instance.stop()
                    slot["page"] = None
            except Exception:
                pass


# Global pool instance
_browser_pool = BrowserPool()
# ═══════════════════════════════════════════════════════════════════════
#  SHARED STATE
# ═══════════════════════════════════════════════════════════════════════

orchestrator_state = {
    "status": "Idle",
    "is_running": False,
    "logs": [],
}

cancel_event = threading.Event()

# Settings (persisted)
_settings = {
    "ai_comparison": False,
    "no_doc_mode": False,
    "old_file_mode": False,
    "date_start": None,
    "date_end": None,
}

# Update queue for Documents page actions
_update_queue = []
_update_queue_lock = threading.Lock()
# _update_thread = None
_update_status = {}  # {folder: {status, message, logs}}
_queue_dispatcher_thread = None

def load_settings():
    global _settings
    if os.path.exists(SETTINGS_FILE):
        try:
            with open(SETTINGS_FILE, "r") as f:
                saved = json.load(f)
                _settings.update(saved)
        except Exception:
            pass
    return dict(_settings)


def save_settings(updates: dict):
    global _settings
    _settings.update(updates)
    with open(SETTINGS_FILE, "w") as f:
        json.dump(_settings, f, indent=2)


# ═══════════════════════════════════════════════════════════════════════
#  ZOOM HELPER (matches import_process pattern)
# ═══════════════════════════════════════════════════════════════════════

def _strip_vessel_imo(val: str) -> str:
    """Strip IMO/voyage number in parentheses from vessel name.
    'MORTEN MAERSK (9632105)' → 'MORTEN MAERSK'
    """
    if not val:
        return ""
    return re.sub(r'\s*\(\d+\)\s*$', '', str(val).strip())


def _apply_zoom(page: Page):
    """Apply 75% zoom for stable UI interaction. Call after every navigation."""
    try:
        page.evaluate("""() => {
            document.documentElement.style.zoom = '0.75';
            document.documentElement.style.minHeight = '100vh';
            document.documentElement.style.overflowY = 'auto';
        }""")
    except Exception:
        pass


# ═══════════════════════════════════════════════════════════════════════
#  NORMALIZATION HELPERS (from import_process)
# ═══════════════════════════════════════════════════════════════════════

def _norm(val: str) -> str:
    if not val:
        return ""
    return val.strip().upper().replace("'", "").replace("-", "").replace(" ", "")


def _is_empty(val) -> bool:
    if val is None:
        return True
    return str(val).strip().upper() in ("", "NONE", "NULL", "N/A", "--", "\u2014")


def _strip_units(val: str) -> str:
    if not val or str(val).strip() in ("", "--", "\u2014"):
        return "0"
    cleaned = re.sub(r"[^\d.]", "", str(val).strip().replace(",", ""))
    return cleaned if cleaned else "0"


def _normalize_container_type(raw: str) -> str:
    if not raw:
        return ""
    v = str(raw).strip().upper().replace(" ", "")
    if v in CONTAINER_TYPE_MAP:
        return CONTAINER_TYPE_MAP[v]
    if v.startswith("20"):
        return "20' General Purpose"
    if v.startswith("40"):
        return "40' High Cube" if "HC" in v or "HQ" in v or "H0" in v else "40' General Purpose"
    return v


def _normalize_pkg_type(raw: str) -> str:
    if not raw:
        return "Package"
    v = str(raw).strip().upper()
    if v in PACKAGE_TYPE_MAP:
        return PACKAGE_TYPE_MAP[v]
    # Strip trailing S for plural forms not in map
    if v.endswith("S") and v[:-1] in PACKAGE_TYPE_MAP:
        return PACKAGE_TYPE_MAP[v[:-1]]
    return v.title() if v else "Package"


def _normalize_carrier_name(carrier_name: str) -> str:
    if not carrier_name:
        return ""
    val_upper = str(carrier_name).upper().strip()
    if val_upper == "OOLU":
        return "OOCL"
    try:
        from Tracking import CARRIER_MAP
    except ImportError:
        CARRIER_MAP = {
            "MSC":        ["MEDITERRANEAN SHIPPING COMPANY", "MSC", "MEDITERRANEAN SHIPPING CO", "MEDITERRANEAN SHIPPIN"],
            "MAERSK":     ["MAERSK", "A.P. MOLLER - MAERSK", "MAERSK LINE", "AP MOLLER MAERSK"],
            "ONE":        ["OCEAN NETWORK EXPRESS", "ONE LINE", "ONE"],
            "EVERGREEN":  ["EVERGREEN", "EVERGREEN LINE", "EMC", "EVERGREEN MARINE"],
            "YANG MING":  ["YANG MING", "YANG MING LINE", "YML", "YANG MING MARINE"],
            "HAPAG":      ["HAPAG-LLOYD", "HAPAG LLOYD", "HAPAG", "HAPAG-LLOYD AKTIENGESELLSCHAFT"],
            "COSCO":      ["COSCO", "COSCO SHIPPING", "COSCO LINE", "COSCO SHIPPING LINES"],
            "HMM":        ["HMM", "HYUNDAI MERCHANT MARINE", "HMM CO"],
            "ZIM":        ["ZIM", "ZIM INTEGRATED SHIPPING", "ZIM LINE"],
            "PIL":        ["PIL", "PACIFIC INTERNATIONAL LINES"],
            "OOCL":       ["OOCL", "ORIENT OVERSEAS CONTAINER LINE", "ORIENT OVERSEAS"],
            "WAN HAI":    ["WAN HAI", "WAN HAI LINES", "WHL"],
        }
    val_clean = re.sub(r'[^\w\s]', '', val_upper).replace(" ", "")
    for key, aliases in CARRIER_MAP.items():
        for alias in aliases:
            a_upper = alias.upper().strip()
            a_clean = re.sub(r'[^\w\s]', '', a_upper).replace(" ", "")
            if val_upper == a_upper or val_clean == a_clean or val_clean == key.upper():
                return key
    return val_upper


def _field_status(existing: str, expected: str,
                  is_numeric: bool = False, is_goods: bool = False) -> str:
    """
    Returns "MATCH", "ADDED", or "MISMATCH".
    """
    if not expected or _is_empty(expected):
        return "MATCH"

    ext = str(existing or "").strip().upper()
    exp = str(expected or "").strip().upper()

    if is_goods:
        return "MATCH" if ext[:20] == exp[:20] else "MISMATCH"

    if is_numeric:
        def _to_f(v):
            c = re.sub(r"[^\d.]", "", str(v).replace(",", ""))
            try:
                return float(c)
            except Exception:
                return None
        v_ext = _to_f(ext)
        v_exp = _to_f(exp)
        if v_ext is not None and v_exp is not None:
            diff = abs(v_ext - v_exp)
            if diff <= max(v_exp * 0.005, 0.5):
                return "MATCH"

    if _norm(ext) == _norm(exp):
        return "MATCH"
    if not ext or ext in ("-", "--"):
        return "ADDED"
    return "MISMATCH"


# ═══════════════════════════════════════════════════════════════════════
#  DOM READ HELPERS (import_process style)
# ═══════════════════════════════════════════════════════════════════════

def _read_by_id(page: Page, el_id: str) -> str:
    try:
        el = page.locator(f"#{el_id}").first
        if el.is_visible(timeout=2000):
            return (el.input_value(timeout=1000) or "").strip()
    except Exception:
        pass
    return ""


def _read_by_label(page: Page, label_text: str, readonly_only: bool = False) -> str:
    """Read an input value by its form label text."""
    try:
        return page.evaluate("""([labelText, readonlyOnly]) => {
            for (const label of document.querySelectorAll('.el-form-item__label')) {
                const txt = label.textContent.trim().toUpperCase();
                if (!txt.startsWith(labelText.toUpperCase())) continue;
                const item = label.closest('.el-form-item');
                if (!item) continue;
                const inp = readonlyOnly
                    ? item.querySelector('input.el-input__inner[readonly]')
                    : item.querySelector('input.el-input__inner');
                if (inp) return inp.value || '';
            }
            return '';
        }""", [label_text, readonly_only]) or ""
    except Exception:
        return ""


def _read_party_card(page: Page, label: str) -> dict:
    """Read a party card (Shipper/Consignee/Notify) from Parties tab DOM."""
    lines = page.evaluate("""(labelText) => {
        const title = [...document.querySelectorAll('*')]
            .find(el => el.childElementCount === 0
                     && el.innerText?.trim() === labelText);
        if (!title) return [];
        const card = title.parentElement;
        if (!card) return [];
        return card.innerText
            .split('\\n')
            .map(v => v.trim())
            .filter(Boolean);
    }""", label) or []

    clean = [l for l in lines if l.strip() != label]
    name = clean[0] if clean else ""
    full_address = ", ".join(clean) if clean else ""
    return {"name": name, "full": full_address, "lines": clean}


def _read_routing_field(page: Page, selector: str) -> str:
    """Read a routing input value with JS fallback."""
    try:
        el = page.locator(selector).first
        if el.is_visible(timeout=1000):
            val = el.input_value(timeout=1000).strip()
            if val:
                return val
    except Exception:
        pass
    try:
        css_sel = selector.replace("'", "\\'")
        return (page.evaluate(f"""() => {{
            const el = document.querySelector('{css_sel}');
            return el ? (el.value || '').trim() : '';
        }}""") or "").strip()
    except Exception:
        return ""


# ═══════════════════════════════════════════════════════════════════════
#  DOM WRITE HELPERS (for update queue)
# ═══════════════════════════════════════════════════════════════════════

def _fill_text_by_id(page: Page, el_id: str, value: str) -> bool:
    if _is_empty(value):
        return False
    try:
        inp = page.locator(f"#{el_id}").first
        if not inp.is_visible(timeout=3000):
            return False
        if inp.get_attribute("readonly") is not None:
            return False
        inp.click()
        inp.fill("")
        inp.fill(value)
        page.wait_for_timeout(300)
        return True
    except Exception as e:
        log.warning("_fill_text_by_id #%s failed: %s", el_id, e)
        return False


def _fill_dropdown_by_label(page: Page, label_text: str, value: str) -> bool:
    if _is_empty(value):
        return False
    try:
        clicked = page.evaluate("""([labelText]) => {
            const target = labelText.toUpperCase().trim();
            for (const label of document.querySelectorAll('.el-form-item__label')) {
                const txt = label.textContent.trim().toUpperCase();
                if (txt === target || txt.startsWith(target)) {
                    const item = label.closest('.el-form-item');
                    if (!item) continue;
                    const inp = item.querySelector('input.el-input__inner[readonly]');
                    if (inp) { inp.click(); return true; }
                }
            }
            return false;
        }""", [label_text])

        if not clicked:
            return False
        page.wait_for_timeout(800)
        return _pick_dropdown_option(page, value)
    except Exception as e:
        log.warning("_fill_dropdown_by_label '%s' failed: %s", label_text, e)
        return False


def _pick_dropdown_option(page: Page, value: str) -> bool:
    val_lower = value.strip().lower()
    clicked = page.evaluate("""(valLower) => {
        const dropdowns = [...document.querySelectorAll('.el-select-dropdown')]
            .filter(d => d.offsetParent !== null);
        if (!dropdowns.length) return false;
        const active = dropdowns[dropdowns.length - 1];
        const items = [...active.querySelectorAll('.el-select-dropdown__item')];
        for (const item of items) {
            const t = (item.querySelector('span') || item).textContent.trim().toLowerCase();
            if (t === valLower) { item.click(); return true; }
        }
        for (const item of items) {
            const t = (item.querySelector('span') || item).textContent.trim().toLowerCase();
            if (t.includes(valLower)) { item.click(); return true; }
        }
        return false;
    }""", val_lower)
    page.wait_for_timeout(500)
    return bool(clicked)


def _fill_search_select(page: Page, el_id: str, value: str) -> bool:
    """Fill a readonly search-select (e.g. #carrier) with autocomplete."""
    if _is_empty(value):
        return False
    try:
        inp = page.locator(f"#{el_id}").first
        if not inp.is_visible(timeout=3000):
            return False
        inp.click()
        page.wait_for_timeout(500)
        inp.click(click_count=3)
        page.keyboard.press("Control+A")
        page.keyboard.press("Delete")
        page.wait_for_timeout(300)
        search = value.split("(")[0].strip()
        for ch in search:
            page.keyboard.type(ch, delay=50)
        page.wait_for_timeout(2000)

        search_upper = search.upper()
        clicked = page.evaluate("""(su) => {
            const pools = [
                '.el-autocomplete-suggestion__list li',
                '.el-autocomplete-suggestion li',
                '.el-select-dropdown__item',
                '.el-scrollbar__view li',
            ];
            for (const sel of pools) {
                const items = [...document.querySelectorAll(sel)];
                if (!items.length) continue;
                const match = items.find(i => i.textContent.trim().toUpperCase().includes(su));
                if (match) { match.click(); return true; }
                items[0].click(); return true;
            }
            return false;
        }""", search_upper)
        if not clicked:
            page.keyboard.press("Enter")
        page.wait_for_timeout(500)
        return True
    except Exception as e:
        log.warning("_fill_search_select #%s failed: %s", el_id, e)
        return False

def _fill_search_select_loc(page: Page, inp, value: str) -> bool:
    """Fill a search-select using a provided locator."""
    if _is_empty(value):
        return False
    try:
        if not inp.is_visible(timeout=3000):
            return False
        inp.click()
        page.wait_for_timeout(500)
        inp.click(click_count=3)
        page.keyboard.press("Control+A")
        page.keyboard.press("Delete")
        page.wait_for_timeout(300)
        search = value.split("(")[0].strip()
        for ch in search:
            page.keyboard.type(ch, delay=50)
        page.wait_for_timeout(2000)

        search_upper = search.upper()
        clicked = page.evaluate("""(su) => {
            const pools = [
                '.el-autocomplete-suggestion__list li',
                '.el-autocomplete-suggestion li',
                '.el-select-dropdown__item',
                '.el-scrollbar__view li',
            ];
            for (const sel of pools) {
                const items = [...document.querySelectorAll(sel)];
                if (!items.length) continue;
                const activeItems = items.filter(i => i.offsetParent !== null);
                if (!activeItems.length) continue;
                const match = activeItems.find(i => i.textContent.trim().toUpperCase().includes(su));
                if (match) { match.click(); return true; }
                activeItems[0].click(); return true;
            }
            return false;
        }""", search_upper)
        if not clicked:
            page.keyboard.press("Enter")
        page.wait_for_timeout(500)
        return True
    except Exception as e:
        log.warning("_fill_search_select_loc failed: %s", e)
        return False

def _select_vessel_dom(page: Page, vessel_value: str):
    """DOM-based interval typing for Vessel name autocomplete.
    Clears existing value first to avoid stale dropdown matches."""
    safe_val = vessel_value.replace('"', '\\"').upper()
    script = f"""
    (() => {{
        const vesselValue = "{safe_val}";
        const vesselItem = [...document.querySelectorAll('.el-form-item')]
            .find(el => el.innerText.includes('Vessel name'));
        if (!vesselItem) return;
        const input = vesselItem.querySelector('input');
        if (!input) return;
        input.removeAttribute('readonly');
        input.click();
        input.focus();

        // ── CLEAR existing value first ──
        input.value = '';
        input.dispatchEvent(new Event('input', {{ bubbles: true }}));
        input.dispatchEvent(new Event('change', {{ bubbles: true }}));

        // Wait for dropdown to close/reset after clearing
        setTimeout(() => {{
            // Double-check cleared
            input.value = '';
            input.dispatchEvent(new Event('input', {{ bubbles: true }}));

            setTimeout(() => {{
                let i = 0;
                const typeInterval = setInterval(() => {{
                    if (i >= vesselValue.length) {{
                        clearInterval(typeInterval);
                        setTimeout(() => {{
                            const selectors = ['.el-autocomplete-suggestion li',
                                               '.el-select-dropdown__item'];
                            let options = [];
                            for (const s of selectors) {{
                                const found = Array.from(document.querySelectorAll(s))
                                    .filter(el => el.offsetParent !== null);
                                if (found.length > 0) {{ options = found; break; }}
                            }}
                            if (options.length > 0) {{
                                const search = vesselValue.toUpperCase().trim();
                                const scored = options.map(el => {{
                                    const full = el.innerText.trim().toUpperCase();
                                    const nameOnly = full.split('(')[0].trim();
                                    let score = 0;
                                    if (nameOnly === search) score = 100;
                                    else if (nameOnly.indexOf(search) === 0) score = 80;
                                    else if (nameOnly.indexOf(search) > -1) score = 60;
                                    return {{ el, score }};
                                }});
                                scored.sort((a, b) => b.score - a.score);
                                if (scored[0].score > 0) scored[0].el.click();
                                else options[0].click();
                            }}
                        }}, 1000);
                        return;
                    }}
                    input.value += vesselValue[i];
                    input.dispatchEvent(new InputEvent('input', {{
                        bubbles: true, data: vesselValue[i], inputType: 'insertText'
                    }}));
                    i++;
                }}, 80);
            }}, 500);
        }}, 800);
    }})();
    """
    page.evaluate(script)
    page.wait_for_timeout(len(vessel_value) * 100 + 4500)


def _click_save(page: Page) -> bool:
    """Click Save button on the current tab."""
    saved = False
    for sel in [
        "button.save-button.el-button--primary",
        "button.el-button--primary:has-text('Save')",
        "button:has(span:text-is('Save'))",
    ]:
        try:
            btn = page.locator(sel).first
            if btn.is_visible(timeout=2000):
                btn.click()
                saved = True
                break
        except Exception:
            continue

    if not saved:
        saved = page.evaluate("""() => {
            const btn = document.querySelector('button.save-button')
                || [...document.querySelectorAll('button')]
                    .find(b => b.textContent.trim() === 'Save');
            if (btn) { btn.click(); return true; }
            return false;
        }""")

    page.wait_for_timeout(2500)
    try:
        ok = page.locator("button:has-text('OK'):visible").first
        if ok.is_visible(timeout=2000):
            ok.click()
            page.wait_for_timeout(1000)
    except Exception:
        pass
    return bool(saved)


# ═══════════════════════════════════════════════════════════════════════
#  TRACKING / NO_DOC / CHECKED FILE HELPERS
# ═══════════════════════════════════════════════════════════════════════

def load_checked() -> set:
    if os.path.exists(TRACKING_FILE):
        try:
            with open(TRACKING_FILE, "r") as f:
                return set(json.load(f).get("checked", []))
        except Exception:
            pass
    return set()


def save_checked(checked: set):
    with open(TRACKING_FILE, "w") as f:
        json.dump({"checked": sorted(checked)}, f, indent=2)


def load_no_doc() -> dict:
    if os.path.exists(NO_DOC_FILE):
        try:
            with open(NO_DOC_FILE, "r") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def save_no_doc(no_doc: dict):
    with open(NO_DOC_FILE, "w") as f:
        json.dump(no_doc, f, indent=2, ensure_ascii=False)


def is_past_date(date_str: str) -> bool:
    try:
        clean = date_str.replace("-", " ")
        return datetime.strptime(clean, "%d %b %Y").date() < datetime.now().date()
    except Exception:
        return False

def add_no_doc_entry(no_doc: dict, key: str, ref: str, date_str: str, **kwargs):
    now_str = datetime.now().isoformat(timespec='seconds')
    if key in no_doc:
        no_doc[key]["last_checked"] = now_str
        no_doc[key]["check_count"] = no_doc[key].get("check_count", 1) + 1
        no_doc[key].update(kwargs)
    else:
        no_doc[key] = {
            "ref": ref, "date": date_str,
            "first_seen": now_str, "last_checked": now_str, "check_count": 1,
            **kwargs
        }

def remove_no_doc_entry(no_doc: dict, key: str):
    no_doc.pop(key, None)


QUEUE_FILE = "queue.json"

def load_queue() -> dict:
    if os.path.exists(QUEUE_FILE):
        try:
            with open(QUEUE_FILE, "r") as f:
                return json.load(f)
        except Exception:
            pass
    return {}

def save_queue(queue_data: dict):
    with open(QUEUE_FILE, "w") as f:
        json.dump(queue_data, f, indent=2, ensure_ascii=False)

def add_to_queue(folder: str, fields: dict):
    queue_data = load_queue()
    queue_data[folder] = {
        "added_at": datetime.now().isoformat(timespec='seconds'),
        "fields": fields
    }
    save_queue(queue_data)

def remove_from_queue(folder: str):
    queue_data = load_queue()
    if folder in queue_data:
        del queue_data[folder]
        save_queue(queue_data)


def write_processing_log(save_dir: str, ref: str, date_str: str,
                         stage: str, detail: str = ""):
    # 1. Update the central JSON OI log
    log_oi_event(ref, stage, detail, data={"date": date_str, "save_dir": save_dir})
    
    # 2. Keep the local folder log for backward compatibility
    log_path = os.path.join(save_dir, "processing_log.json")
    entries = []
    if os.path.exists(log_path):
        try:
            with open(log_path, "r") as f:
                entries = json.load(f)
        except Exception:
            entries = []
    entries.append({
        "timestamp": datetime.now().isoformat(timespec='seconds'),
        "session_id": SESSION_ID,
        "ref": ref, "date": date_str, "stage": stage, "detail": detail,
    })
    try:
        with open(log_path, "w") as f:
            json.dump(entries, f, indent=2, ensure_ascii=False)
    except Exception:
        pass


# ═══════════════════════════════════════════════════════════════════════
#  DATE WINDOW
# ═══════════════════════════════════════════════════════════════════════

def build_date_window() -> list:
    settings = load_settings()
    if sys.platform == 'win32':
        fmt = "%#d %b %Y"
    else:
        fmt = "%-d %b %Y"

    custom_start = settings.get("date_start")
    custom_end = settings.get("date_end")

    if custom_start and custom_end:
        try:
            start_dt = datetime.strptime(custom_start, "%Y-%m-%d")
            end_dt = datetime.strptime(custom_end, "%Y-%m-%d")
            dates = []
            current = start_dt
            while current <= end_dt:
                dates.append(current.strftime(fmt))
                current += timedelta(days=1)
            if dates:
                return dates
        except ValueError:
            pass

    today = datetime.now()
    return [(today + timedelta(days=i)).strftime(fmt) for i in range(DATE_WINDOW)]


# ═══════════════════════════════════════════════════════════════════════
#  FOLDER + REF HELPERS
# ═══════════════════════════════════════════════════════════════════════

def make_shipment_folder(date_str: str, ref_no: str) -> str:
    safe_date = date_str.replace(" ", "-")
    folder_path = os.path.join(SHIPMENTS_ROOT, f"{safe_date}__{ref_no}")
    os.makedirs(folder_path, exist_ok=True)
    return folder_path


def extract_ref_number(row) -> str:
    try:
        cells = row.locator("td").all()
        if not cells:
            cells = row.locator("[role='cell']").all()
        for cell in cells:
            try:
                text = cell.inner_text(timeout=2000).strip()
            except Exception:
                continue
            if not text:
                continue
            for line in text.splitlines():
                match = REF_PATTERN.search(line.strip())
                if match:
                    return match.group(0).replace(" ", "_").upper()
    except Exception:
        pass
    return "UNKNOWN_REF"


# ═══════════════════════════════════════════════════════════════════════
#  ROW COLLECTION + PAGINATION
# ═══════════════════════════════════════════════════════════════════════

def get_all_rows_for_date(page: Page, date_str: str) -> list:
    selectors = [
        f".el-table__body tr:has-text('{date_str}')",
        f"table tbody tr:has-text('{date_str}')",
        f"tr:has-text('{date_str}')",
    ]
    for sel in selectors:
        rows = page.locator(sel).all()
        visible = [r for r in rows if r.is_visible()]
        if visible:
            return visible
    return []


def try_next_page(page: Page) -> bool:
    try:
        page.locator(".el-pagination").wait_for(state="visible", timeout=10000)
    except Exception:
        pass
    try:
        page.evaluate("document.documentElement.style.zoom = '1.0'")
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        page.wait_for_timeout(2000)
    except Exception:
        pass

    js_clicked = page.evaluate("""() => {
        const btn = [...document.querySelectorAll('button')].find(el => {
            const text = el.textContent.trim();
            return text.startsWith('Next') && !el.disabled
                && !el.classList.contains('is-disabled');
        });
        if (btn) { btn.scrollIntoView(); btn.click(); return true; }
        return false;
    }""")

    if js_clicked:
        page.wait_for_timeout(4000)
        try:
            page.locator(".el-loading-mask").wait_for(state="hidden", timeout=10000)
        except Exception:
            pass
        return True
    return False


def go_to_first_page(page: Page):
    try:
        active_page = page.locator(
            ".el-pagination li.number.active, .el-pager li.active"
        ).first
        if active_page.is_visible(timeout=1000):
            if active_page.inner_text().strip() == "1":
                return
        first_page = page.locator(
            ".el-pagination li.number:first-child, .el-pager li:first-child"
        ).first
        if first_page.is_visible(timeout=3000):
            first_page.click()
            page.wait_for_timeout(1000)
            try:
                page.locator(".el-loading-mask").wait_for(state="hidden", timeout=15000)
            except Exception:
                pass
            page.wait_for_timeout(2000)
    except Exception:
        try:
            apply_filters(page)
            page.wait_for_timeout(3000)
        except Exception:
            pass


# ═══════════════════════════════════════════════════════════════════════
#  DEDUP LOGIC
# ═══════════════════════════════════════════════════════════════════════

def parse_dt_from_filename(raw_name: str):
    name = unquote(raw_name)
    name = os.path.splitext(name)[0]
    m = DT_PATTERN.search(name)
    if m:
        try:
            return datetime(
                int(m.group(1)), int(m.group(2)), int(m.group(3)),
                int(m.group(4)), int(m.group(5)), int(m.group(6))
            )
        except ValueError:
            pass
    return None


def extract_core_id(raw_name: str) -> str:
    name = unquote(raw_name)
    name = os.path.splitext(name)[0]
    name = DT_PATTERN.sub("", name).rstrip("_")
    for pattern in NOISE_PREFIXES:
        name = re.sub(pattern, "", name, flags=re.IGNORECASE).strip()
    return name.strip("_").strip()


def classify_docs_by_dedup(doc_entries: list) -> list:
    for entry in doc_entries:
        raw = entry.get("raw_name") or entry.get("ui_name") or ""
        entry["core_id"] = extract_core_id(raw)
        entry["dt"] = parse_dt_from_filename(raw)

    groups: dict = {}
    for entry in doc_entries:
        key = (entry["doc_type"], entry["core_id"])
        groups.setdefault(key, []).append(entry)

    for key, group in groups.items():
        has_dt = [e for e in group if e["dt"] is not None]
        no_dt = [e for e in group if e["dt"] is None]
        if len(group) == 1:
            group[0]["is_original"] = True
        elif has_dt:
            has_dt.sort(key=lambda e: e["dt"], reverse=True)
            has_dt[0]["is_original"] = True
            for e in has_dt[1:]:
                e["is_original"] = False
            for e in no_dt:
                e["is_original"] = False
        else:
            group[0]["is_original"] = True
            for e in group[1:]:
                e["is_original"] = False

    return doc_entries


# ═══════════════════════════════════════════════════════════════════════
#  DOCUMENT TAB: READ + RESOLVE + DOWNLOAD
# ═══════════════════════════════════════════════════════════════════════

def read_doc_table_entries(page: Page) -> list:
    entries = []
    all_rows = page.locator("table tr").all()
    for tr in all_rows:
        try:
            cells = tr.locator("td").all()
            if len(cells) < 2:
                continue
            type_text = cells[0].inner_text().strip()
            name_text = cells[1].inner_text().strip()
            is_house = "House" in type_text
            is_master = "Master" in type_text
            if is_house or is_master:
                entries.append({
                    "doc_type": "House BL" if is_house else "Master BL",
                    "ui_name": name_text, "raw_name": name_text,
                    "cell": cells[0], "pdf_url": None,
                })
        except Exception:
            continue

    if not entries:
        for doc_type in ("House BL", "Master BL"):
            for cell in page.get_by_role("cell", name=doc_type).all():
                try:
                    if cell.is_visible():
                        entries.append({
                            "doc_type": doc_type, "ui_name": doc_type,
                            "raw_name": doc_type, "cell": cell, "pdf_url": None,
                        })
                except Exception:
                    pass
    return entries


def resolve_pdf_url(page: Page, cell) -> tuple:
    try:
        import threading
        import tempfile
        event = threading.Event()
        result = {}

        def on_popup(popup):
            result['popup'] = popup
            event.set()

        def on_download(download):
            result['download'] = download
            event.set()

        page.once("popup", on_popup)
        page.once("download", on_download)

        cell.click()

        # Wait up to 8 seconds for either event
        for _ in range(80):
            if event.is_set():
                break
            page.wait_for_timeout(100)

        # Cleanup listeners in case one didn't fire
        try:
            page.remove_listener("popup", on_popup)
            page.remove_listener("download", on_download)
        except Exception:
            pass

        if "download" in result:
            download = result["download"]
            suggested = download.suggested_filename or "document.pdf"
            ext = os.path.splitext(suggested)[1] or ".pdf"
            tmp_fd, tmp_path = tempfile.mkstemp(suffix=ext)
            os.close(tmp_fd)
            download.save_as(tmp_path)
            return f"local://{tmp_path}", suggested

        if "popup" in result:
            viewer = result["popup"]
            viewer.wait_for_load_state("domcontentloaded")

            pdf_url = None
            embed = viewer.locator("embed").first
            try:
                if embed.is_visible(timeout=2000):
                    pdf_url = (
                        embed.get_attribute("original-url")
                        or embed.get_attribute("src")
                    )
            except Exception:
                pass

            if not pdf_url:
                iframe = viewer.locator("iframe").first
                try:
                    if iframe.is_visible(timeout=2000):
                        pdf_url = iframe.get_attribute("src")
                except Exception:
                    pass

            if not pdf_url and "shipment-document" in viewer.url:
                pdf_url = viewer.url

            viewer.close()

            if pdf_url:
                if pdf_url.startswith("/"):
                    pdf_url = "https://jit-api.jordex.com" + pdf_url
                url_path = pdf_url.split("?")[0]
                raw_from_url = url_path.split("/")[-1]
                return pdf_url, raw_from_url

        return None, None
    except Exception as e:
        log.error("resolve_pdf_url failed: %s", e)
        return None, None


def download_one(page: Page, entry: dict, save_dir: str) -> bool:
    doc_type = entry["doc_type"]
    core_id = entry.get("core_id", "UNKNOWN")
    dt = entry.get("dt")
    is_orig = entry.get("is_original", True)
    pdf_url = entry.get("pdf_url")

    type_slug = doc_type.replace(" ", "_")
    dt_slug = dt.strftime("%Y-%m-%d") if dt else "no-date"
    dup_suffix = "" if is_orig else "_Copy"

    # Detect real extension from raw_name (suggested filename from download)
    raw_name = entry.get("raw_name", "")
    raw_ext = os.path.splitext(raw_name)[1].lower() if raw_name else ""
    ext = raw_ext if raw_ext in (".pdf", ".docx", ".doc", ".xlsx") else ".pdf"
    filename = f"{type_slug}__{core_id}__{dt_slug}{dup_suffix}{ext}"

    if not pdf_url:
        return False

    save_path = os.path.join(save_dir, filename)

    if pdf_url.startswith("local://"):
        import shutil
        tmp_path = pdf_url[8:] # Strip local://
        try:
            shutil.move(tmp_path, save_path)
            entry["saved_path"] = save_path
            return True
        except Exception as e:
            log.error("Failed to move local download: %s", e)
            return False

    for attempt_num in range(1, 4):
        try:
            timeout_ms = [None, 120_000, 180_000][attempt_num - 1]
            if timeout_ms:
                response = page.request.get(pdf_url, timeout=timeout_ms)
            else:
                response = page.request.get(pdf_url)

            if response.ok:
                body = response.body()
                if len(body) < 1024:
                    if attempt_num < 3:
                        page.wait_for_timeout(3000)
                        continue
                    return False
                # Sniff real type from response if ext was guessed
                if ext == ".pdf":
                    content_type = response.headers.get("content-type", "")
                    if "officedocument" in content_type:
                        ext = ".docx"
                    elif "msword" in content_type:
                        ext = ".doc"
                    elif "spreadsheet" in content_type:
                        ext = ".xlsx"
                    
                    if ext != ".pdf":
                        filename = f"{type_slug}__{core_id}__{dt_slug}{dup_suffix}{ext}"
                        save_path = os.path.join(save_dir, filename)
                with open(save_path, "wb") as f:
                    f.write(body)
                entry["saved_path"] = save_path
                return True
            else:
                if attempt_num == 3:
                    return False
        except Exception:
            if attempt_num == 3:
                return False
        page.wait_for_timeout(3000)
    return False


# ═══════════════════════════════════════════════════════════════════════
#  STEP 1: CHECK + DOWNLOAD DOCUMENTS
# ═══════════════════════════════════════════════════════════════════════

def process_shipment_documents(page: Page, date_str: str, ref_no: str) -> tuple:
    """
    Returns: (True, [original_paths], save_dir) or (False, [], None)
    """
    log.info("Opening Documents tab for ref %s...", ref_no)
    try:
        doc_tab = page.get_by_text("Documents", exact=True)
        doc_tab.scroll_into_view_if_needed()
        doc_tab.click()

        found_data = False
        for attempt in range(10):
            rows = page.locator(
                "table tr:has-text('House'), table tr:has-text('Master')"
            ).all()
            if rows:
                found_data = True
                break
            page.wait_for_timeout(1000)

        if not found_data:
            log.warning("No House/Master documents after 10s polling.")
    except Exception as e:
        log.error("Could not load Documents tab: %s", e)
        return False, [], None

    entries = read_doc_table_entries(page)
    if not entries:
        return False, [], None

    # Resolve PDF URLs
    valid_entries = []
    for entry in entries:
        pdf_url, raw_from_url = resolve_pdf_url(page, entry["cell"])
        if pdf_url:
            entry["pdf_url"] = pdf_url
            entry["raw_name"] = raw_from_url or entry["ui_name"]
            valid_entries.append(entry)
        page.wait_for_timeout(500)

    if not valid_entries:
        return False, [], None

    # Dedup
    classified = classify_docs_by_dedup(valid_entries)
    save_dir = make_shipment_folder(date_str, ref_no)
    original_paths = []

    for entry in classified:
        ok = download_one(page, entry, save_dir)
        if ok and entry["is_original"]:
            original_paths.append(entry["saved_path"])
        page.wait_for_timeout(800)

    return True, original_paths, save_dir


# ═══════════════════════════════════════════════════════════════════════
#  DOCX/DOC → PDF CONVERSION
# ═══════════════════════════════════════════════════════════════════════

def _convert_to_pdf(file_path: str) -> str:
    """
    Convert .docx or .doc to PDF using LibreOffice headless.
    Returns the path to the converted PDF, or original path if already PDF.
    """
    ext = os.path.splitext(file_path)[1].lower()
    if ext == ".pdf":
        return file_path

    if ext not in (".docx", ".doc", ".xlsx"):
        log.warning("Unsupported file type for conversion: %s", ext)
        return file_path

    save_dir = os.path.dirname(file_path)
    base_name = os.path.splitext(os.path.basename(file_path))[0]
    pdf_output = os.path.join(save_dir, base_name + ".pdf")

    # Already converted?
    if os.path.exists(pdf_output) and os.path.getsize(pdf_output) > 0:
        log.info("PDF already exists: %s", pdf_output)
        return pdf_output

    # Try LibreOffice first (works on both Windows and Linux)
    import subprocess
    import platform

    soffice_cmd = "soffice"
    if platform.system() == "Windows":
        # Common Windows paths
        for path in [
            r"C:\Program Files\LibreOffice\program\soffice.exe",
            r"C:\Program Files (x86)\LibreOffice\program\soffice.exe",
        ]:
            if os.path.exists(path):
                soffice_cmd = path
                break

    try:
        result = subprocess.run(
            [
                soffice_cmd, "--headless", "--norestore",
                "--convert-to", "pdf",
                "--outdir", save_dir,
                file_path,
            ],
            capture_output=True, text=True, timeout=60,
        )
        if os.path.exists(pdf_output) and os.path.getsize(pdf_output) > 0:
            log.info("Converted %s → %s", os.path.basename(file_path), os.path.basename(pdf_output))
            return pdf_output
        else:
            log.warning("LibreOffice conversion produced no output. stderr: %s", result.stderr[:300])
    except FileNotFoundError:
        log.warning("LibreOffice not found. Install it for DOCX→PDF conversion.")
    except subprocess.TimeoutExpired:
        log.warning("LibreOffice conversion timed out for %s", file_path)
    except Exception as e:
        log.error("LibreOffice conversion failed: %s", e)

    # Fallback: try docx2pdf (Windows only, requires MS Word)
    if ext == ".docx":
        try:
            from docx2pdf import convert as docx2pdf_convert
            docx2pdf_convert(file_path, pdf_output)
            if os.path.exists(pdf_output) and os.path.getsize(pdf_output) > 0:
                log.info("Converted via docx2pdf: %s → %s",
                         os.path.basename(file_path), os.path.basename(pdf_output))
                return pdf_output
        except ImportError:
            log.warning("docx2pdf not installed. Run: pip install docx2pdf")
        except Exception as e:
            log.error("docx2pdf conversion failed: %s", e)

    log.error("Could not convert %s to PDF. Passing original to extractor.", file_path)
    return file_path

# ═══════════════════════════════════════════════════════════════════════
#  STEP 2: EXTRACT PDFs WITH extractor.py
# ═══════════════════════════════════════════════════════════════════════

def extract_documents(original_paths: list, save_dir: str,
                      status_cb=None) -> dict:
    """
    Run extractor.py on each PDF. Returns {"hbl": [list], "mbl": dict}.
    Converts .docx/.doc to PDF first if needed.
    """
    result = {"hbl": [], "mbl": None}
    if not extractor:
        log.error("extractor module not available")
        return result

    for pdf_path in original_paths:
        filename = os.path.basename(pdf_path)
        if status_cb:
            status_cb(f"Extracting: {filename[:40]}...")

        # ── Convert non-PDF files before extraction ──
        ext = os.path.splitext(pdf_path)[1].lower()
        if ext in (".docx", ".doc"):
            if status_cb:
                status_cb(f"Converting {ext} to PDF: {filename[:40]}...")
            pdf_path = _convert_to_pdf(pdf_path)
            filename = os.path.basename(pdf_path)

            # If conversion failed (still not .pdf), skip
            if not pdf_path.lower().endswith(".pdf"):
                log.warning("Skipping %s — conversion to PDF failed.", filename)
                continue

        data = None
        for attempt in range(3):
            try:
                data = extractor.extract_document(pdf_path)
                if data:
                    break
            except Exception as e:
                log.error("  Extraction failed for %s on attempt %d: %s", filename, attempt + 1, e)
                time.sleep(2)

        if not data:
            continue

        try:
            if data.get("skip"):
                log.info("  Skipped non-BL: %s (%s)",
                         filename, data.get("document_title", ""))
                continue

            json_path = os.path.splitext(pdf_path)[0] + ".json"
            with open(json_path, "w", encoding="utf-8") as jf:
                json.dump(data, jf, indent=2, ensure_ascii=False)

            doc_type = (data.get("document_type") or "").upper()
            if "MASTER" in doc_type:
                if not result["mbl"]:
                    result["mbl"] = data
            elif "HOUSE" in doc_type:
                result["hbl"].append(data)

            log.info("  Extracted: %s -> %s", filename, doc_type)

        except Exception as e:
            log.error("  Error processing extracted data for %s: %s", filename, e)

        time.sleep(2)

    return result


# ═══════════════════════════════════════════════════════════════════════
#  STEP 3: DOM-SCRAPE SYSTEM DATA FROM JORDEX TABS
# ═══════════════════════════════════════════════════════════════════════

def scrape_parties(page: Page) -> dict:
    """Scrape Parties tab. Returns dict with Shipper/Consignee/Notify."""
    try:
        page.get_by_text("Parties", exact=True).first.click()
        page.wait_for_timeout(1500)
    except Exception:
        return {}

    parties = {}
    for label in ["Shipper", "Consignee", "Notify party"]:
        card = _read_party_card(page, label)
        key = label.replace(" ", "_")
        parties[key] = card.get("full", "")
    return parties


def scrape_carrier(page: Page) -> dict:
    """Scrape Carrier tab. Returns dict with all carrier fields."""
    try:
        page.get_by_role("tab", name="Carrier").click(timeout=5000)
        page.wait_for_timeout(1500)
    except Exception:
        return {}

    carrier = {
        "Carrier": _read_by_id(page, "carrier"),
        "Vessel_Name": page.evaluate("""() => {
            const item = [...document.querySelectorAll('.el-form-item')]
                .find(el => el.innerText.includes('Vessel name'));
            return item?.querySelector('input')?.value || '';
        }""") or "",
        "MBL_Type": _read_by_label(page, "MB/L Type", readonly_only=True),
        "MBL_Number": _read_by_id(page, "masterBLNumber"),
        "HBL_Type": _read_by_label(page, "HB/L Type", readonly_only=True),
        "HBL_Number": _read_by_id(page, "houseBLNumber"),
    }

    return carrier


def scrape_cargo(page: Page, ref_no: str) -> list:
    """Scrape Cargo tab. Returns list of container dicts."""
    try:
        cargo_tab = page.get_by_role("tab", name="Cargo").first
        if not cargo_tab.is_visible(timeout=3000):
            cargo_tab = page.get_by_text("Cargo", exact=True).first
        cargo_tab.click(timeout=5000)
        page.wait_for_timeout(3000)
    except Exception:
        return []

    try:
        page.wait_for_selector(".el-table__body", timeout=5000)
    except Exception:
        pass

    def get_visible_rows():
        return [r for r in page.locator(
            ".el-table__body .el-table__row, tr.selectable"
        ).all() if r.is_visible()]

    visible_rows = get_visible_rows()
    total_rows = len(visible_rows)
    containers = []

    for i in range(total_rows):
        try:
            # Re-sync to Cargo tab
            for _ in range(3):
                current_tab = page.locator(
                    ".el-tabs__item.is-active:has-text('Cargo')"
                ).first
                if not current_tab.is_visible(timeout=1000):
                    page.locator(".el-tabs__item:has-text('Cargo')").first.click()
                    page.wait_for_timeout(1500)
                rows_fresh = get_visible_rows()
                if i < len(rows_fresh):
                    break
                page.wait_for_timeout(2000)

            current_visible = get_visible_rows()
            if i >= len(current_visible):
                break

            row = current_visible[i]
            row.scroll_into_view_if_needed(timeout=2000)
            row_text = row.inner_text(timeout=2000).replace("\n", " ").strip()

            # Pre-extract from row text as fallback
            f_no = ""
            f_vol = ""
            f_weight = ""
            no_match = re.search(r'([A-Z]{4}\d{7})', row_text)
            if no_match:
                f_no = no_match.group(1)
            vol_match = re.search(r'([\d.,]+)\s*m3', row_text)
            if vol_match:
                f_vol = vol_match.group(1)
            weight_match = re.search(r'([\d.,]+)\s*kg', row_text)
            if weight_match:
                f_weight = weight_match.group(1)

            row.click(timeout=5000)
            page.wait_for_timeout(2500)

            # Try Packages sub-tab
            try:
                pkg_tab = page.get_by_role("tab", name="Packages").first
                if pkg_tab.is_visible(timeout=3000):
                    pkg_tab.click()
                    page.wait_for_timeout(1500)
            except Exception:
                pass

            container = {
                "Container_No": f_no,
                "Container_Type": "",
                "Qty": "",
                "Package_Type": "",
                "Volume": f_vol,
                "Total_Gross_Weight": f_weight,
                "Goods_Description": "",
            }

            # Read fields via label-anchored DOM
            for field, keywords in [
                ("Container_Type", ["Container type"]),
                ("Container_No", ["Container number", "Container n"]),
                ("Volume", ["Volume"]),
                ("Total_Gross_Weight", ["Weight", "Gross"]),
                ("Qty", ["Qty", "Quantity"]),
                ("Package_Type", ["Package", "Unit"]),
            ]:
                val = page.evaluate("""(keywords) => {
                    for (const label of document.querySelectorAll('.el-form-item__label')) {
                        const t = label.textContent.trim().toLowerCase();
                        if (!keywords.some(k => t.includes(k.toLowerCase()))) continue;
                        const item = label.closest('.el-form-item');
                        if (!item) continue;
                        const inp = item.querySelector('input.el-input__inner');
                        if (inp) return inp.value || '';
                    }
                    return '';
                }""", keywords) or ""
                if val:
                    container[field] = val.strip()

            # Packages table fallback
            try:
                pkg_pane = page.locator(".el-tab-pane:visible").last
                table_rows = pkg_pane.locator(".el-table__row, tr").all()
                if not container["Qty"] and table_rows:
                    for tr in table_rows:
                        if not tr.is_visible(timeout=500):
                            continue
                        cells = tr.locator("td").all()
                        if len(cells) >= 2:
                            qty_text = cells[0].inner_text(timeout=500).strip()
                            pkg_text = cells[1].inner_text(timeout=500).strip()
                            if qty_text and qty_text.replace(",", "").replace(".", "").replace(" ", "").isdigit():
                                container["Qty"] = qty_text
                                container["Package_Type"] = pkg_text
                                if len(cells) >= 3:
                                    w = cells[2].inner_text(timeout=500).strip()
                                    if w and w not in ("--", "-") and not container["Total_Gross_Weight"]:
                                        container["Total_Gross_Weight"] = w
                                if len(cells) >= 4:
                                    v = cells[3].inner_text(timeout=500).strip()
                                    if v and v not in ("--", "-") and not container["Volume"]:
                                        container["Volume"] = v
                                break
            except Exception:
                pass

            # Goods tab
            try:
                goods_tab = page.locator("#tab-goods, [id*='tab-goods']").first
                if not goods_tab.is_visible(timeout=2000):
                    goods_tab = page.get_by_role("tab", name="Goods").first
                if goods_tab.is_visible(timeout=2000):
                    goods_tab.click()
                    page.wait_for_timeout(1000)
                    try:
                        desc = page.locator(
                            "textarea.el-textarea__inner:visible, textarea:visible"
                        ).first.input_value(timeout=1000).strip()
                        if desc:
                            container["Goods_Description"] = desc
                    except Exception:
                        pass
            except Exception:
                pass

            # Fallbacks
            if not container["Container_No"] and f_no:
                container["Container_No"] = f_no
            if not container["Volume"] and f_vol:
                container["Volume"] = f_vol
            if not container["Total_Gross_Weight"] and f_weight:
                container["Total_Gross_Weight"] = f_weight

            containers.append(container)

            # Navigate back to cargo list
            try:
                ref_crumb = page.locator(
                    f".el-breadcrumb__item:has-text('{ref_no}'), "
                    f".el-page-header__content:has-text('{ref_no}')"
                ).first
                if ref_crumb.is_visible(timeout=3000):
                    ref_crumb.click()
                else:
                    # Generic back button can be risky, prefer go_back to reverse the SPA state
                    if "/shipments/ocean" not in page.url:
                        page.go_back(timeout=5000)
            except Exception:
                try:
                    page.locator(".el-tabs__item:has-text('Cargo')").first.click(
                        timeout=1000
                    )
                except Exception:
                    pass

            page.wait_for_timeout(2500)
            if "/shipments/ocean" in page.url:
                log.error("Navigated to dashboard unexpectedly. Aborting cargo extraction loop.")
                break

        except Exception as e:
            log.warning("Error on cargo row %d: %s", i + 1, e)
            if "/shipments/ocean" in page.url:
                log.error("On dashboard during cargo error. Aborting remaining rows.")
                break
            try:
                page.go_back(timeout=3000)
            except Exception:
                pass
            page.wait_for_timeout(1500)
            continue

    return containers


def scrape_routing(page: Page, save_dir: str,
                   container_nos: list) -> dict:
    """Scrape View Routing per container. Returns {cno: {Lane, Destination}}."""
    routing_data = {}

    try:
        btn = page.locator(".routing-sidebar__routing-label").first
        btn.wait_for(state="visible", timeout=10000)
        try:
            btn.click(timeout=5000)
        except Exception:
            page.evaluate(
                "document.querySelector('.routing-sidebar__routing-label')?.click()"
            )
        page.wait_for_load_state("load", timeout=30000)
        page.wait_for_timeout(2000)
    except Exception as e:
        log.warning("Could not click 'View routing': %s", e)
        return routing_data

    # Reset zoom for clean reads
    try:
        page.evaluate("document.documentElement.style.zoom = '1.0'")
        page.wait_for_timeout(1000)
    except Exception:
        pass

    try:
        page.locator(".cargo-tab__block").first.wait_for(
            state="visible", timeout=5000
        )
    except Exception:
        pass

    sidebar_blocks = page.locator(
        ".cargo-tab__content .cargo-tab__block"
    ).all()
    num_containers = max(len(sidebar_blocks), len(container_nos) if container_nos else 0)
    if num_containers == 0:
        num_containers = 1

    for idx in range(num_containers):
        cno = container_nos[idx] if container_nos and idx < len(container_nos) else f"container_{idx + 1}"

        # Click sidebar container (skip first — already selected)
        if idx > 0:
            try:
                blocks = page.locator(".cargo-tab__content .cargo-tab__block").all()
                if blocks and idx < len(blocks):
                    blocks[idx].scroll_into_view_if_needed(timeout=2000)
                    blocks[idx].click(timeout=3000)
                    page.wait_for_timeout(2000)
                else:
                    continue
            except Exception:
                continue

        # Lane tab
        try:
            lane_tab = page.locator(
                "#tab-lane-1, .el-tabs__item:has-text('Lane')"
            ).first
            if lane_tab.is_visible(timeout=5000):
                lane_tab.click()
                page.wait_for_timeout(2000)
        except Exception:
            pass

        lane = {
            "Departure_Original": _read_routing_field(page, "#departure-start"),
            "Departure_Update": _read_routing_field(page, "#departure-end"),
            "Port_of_Loading": _read_routing_field(page, "#portOfLoading"),
            "Voyage": _read_routing_field(page, "#voyage"),
            "Arrival_Original": _read_routing_field(page, "#arrival-start"),
            "Arrival_Update": _read_routing_field(page, "#arrival-end"),
            "Port_of_Discharge": "",
        }

        # Robust POD, POL, and On-Carriage fallback
        try:
            advanced_data = page.evaluate("""() => {
                const results = { pod: '', pol: '', onCarriagePlace: '', onCarriageDate: '' };
                
                let originY = -1;
                let arrivalY = -1;
                let onCarriageY = -1;
                
                // 1. Find section headers by their visual coordinates (Y axis)
                const allEls = document.querySelectorAll('h6, h5, h4, div, span, label');
                for (let el of allEls) {
                    const rect = el.getBoundingClientRect();
                    if (rect.height === 0 || rect.width === 0) continue;
                    
                    const text = (el.innerText || '').trim().toUpperCase();
                    if (text === 'ORIGIN' || text === 'PORT OF LOADING') {
                        if (originY === -1 || rect.y < originY) originY = rect.y;
                    }
                    if (text === 'ARRIVAL' || text === 'PORT OF DISCHARGE') {
                        if (arrivalY === -1 || rect.y < arrivalY) arrivalY = rect.y;
                    }
                    if (text === 'CARRIER ON-CARRIAGE' || text === 'CARRIER ON CARRIAGE') {
                        if (onCarriageY === -1 || rect.y < onCarriageY) onCarriageY = rect.y;
                    }
                }
                
                // 2. Find all port inputs
                const portInputs = [...document.querySelectorAll('input[placeholder*="search port" i]')].map(el => {
                    return { el: el, y: el.getBoundingClientRect().y, val: el.value };
                });
                portInputs.sort((a, b) => a.y - b.y);
                
                // 3. Map inputs based on labels OR their Y-coordinate relative to headers
                for (let pi of portInputs) {
                    let label = '';
                    let p = pi.el.parentElement;
                    for (let i = 0; i < 5; i++) {
                        if (!p) break;
                        const lbls = p.querySelectorAll('label, .el-form-item__label');
                        if (lbls.length > 0) {
                            label = (lbls[0].innerText || '').toUpperCase();
                            break;
                        }
                        p = p.parentElement;
                    }
                    
                    if (label.includes('DISCHARGE') || label.includes('ARRIVAL')) {
                        results.pod = pi.val;
                    } else if (label.includes('LOADING') || label.includes('RECEIPT')) {
                        results.pol = pi.val;
                    } else if (label.includes('ON-CARRIAGE') || label.includes('ON CARRIAGE')) {
                        results.onCarriagePlace = pi.val;
                    } else {
                        // Fallback to Y-coordinate sections
                        if (onCarriageY !== -1 && pi.y > onCarriageY - 10) {
                            if (!results.onCarriagePlace) results.onCarriagePlace = pi.val;
                        } else if (arrivalY !== -1 && pi.y > arrivalY - 10) {
                            if (!results.pod) results.pod = pi.val;
                        } else if (originY !== -1 && pi.y > originY - 10) {
                            if (!results.pol) results.pol = pi.val;
                        }
                    }
                }
                
                // 4. Date logic for On-Carriage (placeholder="TBD")
                const tbdInputs = [...document.querySelectorAll('input[placeholder="TBD" i], input.el-date-editor')].map(el => {
                    return { el: el, y: el.getBoundingClientRect().y, val: el.value };
                });
                tbdInputs.sort((a, b) => a.y - b.y);
                
                for (let ti of tbdInputs) {
                    if (onCarriageY !== -1 && ti.y > onCarriageY - 10) {
                        results.onCarriageDate = ti.val;
                    }
                }
                
                return results;
            }""")
            

            if advanced_data:
                if not lane["Port_of_Discharge"] and advanced_data.get("pod"):
                    lane["Port_of_Discharge"] = advanced_data["pod"].strip()
                if not lane["Port_of_Loading"] and advanced_data.get("pol"):
                    lane["Port_of_Loading"] = advanced_data["pol"].strip()
                    
                if advanced_data.get("onCarriagePlace"):
                    lane["Carrier_On_Carriage"] = [{
                        "arrival_place": advanced_data["onCarriagePlace"].strip(),
                        "arrival_date": advanced_data.get("onCarriageDate", "").strip()
                    }]
        except Exception:
            pass

        # Transits
        transits = []
        try:
            transit_data = page.evaluate("""() => {
                const allH6 = [...document.querySelectorAll('h6')];
                const header = allH6.find(h => h.textContent.includes('Transit'));
                if (!header) return null;
                let container = header.parentElement;
                for (let i = 0; i < 5; i++) {
                    if (container.querySelectorAll('input').length >= 4) break;
                    container = container.parentElement;
                    if (!container) return null;
                }
                const inputs = [...container.querySelectorAll('input.el-input__inner')];
                return inputs.map(inp => inp.value.trim());
            }""")
            if transit_data and len(transit_data) >= 4:
                for i in range(0, len(transit_data), 4):
                    chunk = transit_data[i:i + 4]
                    stop = {
                        "eta": chunk[0] if len(chunk) > 0 else "",
                        "port": chunk[1] if len(chunk) > 1 else "",
                        "etd": chunk[2] if len(chunk) > 2 else "",
                        "voyage": chunk[3] if len(chunk) > 3 else "",
                    }
                    if stop["port"] or stop["eta"] or stop["etd"]:
                        transits.append(stop)
        except Exception:
            pass
        lane["Transits"] = transits

        # Destination tab
        terminal = ""
        try:
            dest_tab = page.locator("#tab-destination-2")
            if not dest_tab.is_visible(timeout=3000):
                dest_tab = page.get_by_role("tab", name="Destination")
            dest_tab.click()
            page.wait_for_timeout(3000)

            terminal = page.evaluate("""() => {
                const textNodes = [...document.querySelectorAll('*')];
                let foundTerminal = false;
                for (const el of textNodes) {
                    if (!foundTerminal && el.textContent.trim() === 'Terminal' && el.children.length === 0) {
                        foundTerminal = true;
                        continue;
                    }
                    if (foundTerminal) {
                        if (el.classList && el.classList.contains('full-address-name')) {
                            return el.textContent.trim();
                        }
                        if (el.textContent.trim() === 'Transport company' || el.textContent.trim() === 'Delivery address') {
                            return '';
                        }
                        if (el.textContent.trim() === 'No address' && el.children.length === 0) {
                            return '';
                        }
                    }
                }
                return '';
            }""") or ""
        except Exception:
            pass

        # Take lane screenshot
        try:
            lane_tab2 = page.locator(
                "#tab-lane-1, .el-tabs__item:has-text('Lane')"
            ).first
            if lane_tab2.is_visible(timeout=3000):
                lane_tab2.click()
                page.wait_for_timeout(1000)
            page.evaluate("window.scrollTo(0, 0)")
            page.wait_for_timeout(800)
            ss = page.screenshot(full_page=True)
            ss_path = os.path.join(save_dir, f"{cno}_lane.png")
            with open(ss_path, "wb") as f:
                f.write(ss)
        except Exception:
            pass

        routing_data[cno] = {
            "Lane": lane,
            "Destination": {"Terminal": terminal},
        }

    # Restore zoom and go back
    try:
        _apply_zoom(page)
    except Exception:
        pass
    try:
        page.get_by_text("Back").first.click()
        page.wait_for_timeout(2000)
    except Exception:
        try:
            page.go_back()
        except Exception:
            pass

    # Save routing JSON
    routing_path = os.path.join(save_dir, "view_routing.json")
    with open(routing_path, "w", encoding="utf-8") as f:
        json.dump(routing_data, f, indent=2, ensure_ascii=False)

    return routing_data


# ═══════════════════════════════════════════════════════════════════════
#  BACKGROUND TRACKING & COMPARISON PIPELINE
# ═══════════════════════════════════════════════════════════════════════




# ═══════════════════════════════════════════════════════════════════════
#  STEP 4: COMPARISON (String or AI)
# ═══════════════════════════════════════════════════════════════════════

def compare_data(system_data: dict, hbl_data: dict, mbl_data: dict,
                 tracking_results: dict, routing_data: dict, is_direct_file: bool = False) -> dict:
    """
    Compare scraped Jordex data vs extracted document data.
    Always attempts AI comparison first, falls back to Python string comparison.
    Routing is handled separately via _compare_routing_with_ai.
    """
    return _compare_with_ai(
        system_data, hbl_data, mbl_data, tracking_results, routing_data, is_direct_file
    )


def _compare_string(system_data: dict, hbl_data: dict, mbl_data: dict,
                    tracking_results: dict, routing_data: dict, is_direct_file: bool = False) -> dict:
    """Pure Python string comparison with normalization + tolerance."""
    result = {"Parties": [], "Carrier": [], "Cargo": [], "Routing": []}

    parties = system_data.get("Parties", {})
    carrier = system_data.get("Carrier", {})
    containers = system_data.get("Cargo", [])

    # ── PARTIES ──
    party_map = {
        "Shipper": hbl_data.get("shipper", ""),
        "Consignee": hbl_data.get("consignee", ""),
        "Notify_party": hbl_data.get("notify", ""),
    }
    for field, doc_val in party_map.items():
        jdx_val = parties.get(field, "")
        status = _field_status(jdx_val, doc_val)
        result["Parties"].append({
            "field": field,
            "jordex_value": jdx_val,
            "document_value": doc_val,
            "status": status,
        })

    # ── CARRIER ──
    # Vessel: compare against tracking arrival_vessel, not BL
    arrival_vessel = ""
    if tracking_results:
        first_track = next(iter(tracking_results.values()), {})
        arrival_vessel = first_track.get("arrival_vessel", "")

    doc_vessel = arrival_vessel if arrival_vessel else "No tracking result"

    carrier_map = {
        "Carrier": mbl_data.get("carrier_name", "") or mbl_data.get("carrier_code", ""),
        "Vessel_Name": doc_vessel,
        "MBL_Type": mbl_data.get("bl_type", ""),
        "MBL_Number": mbl_data.get("reference_number", ""),
        "HBL_Number": hbl_data.get("reference_number", ""),
        "HBL_Type": hbl_data.get("bl_type", ""),
    }
    for field, doc_val in carrier_map.items():
        if is_direct_file and field in ["HBL_Number", "HBL_Type"]:
            continue

        jdx_val = carrier.get(field, "")

        # Special: Vessel name — strip IMO number (9632105) before comparing
        if field == "Vessel_Name":
            if not jdx_val or _is_empty(jdx_val):
                status = "MATCH" if doc_val == "No tracking result" or _is_empty(doc_val) else "ADDED"
            elif doc_val == "No tracking result":
                status = "MISMATCH"
            elif jdx_val and doc_val:
                j_vessel = _strip_vessel_imo(jdx_val).upper().strip()
                d_vessel = _strip_vessel_imo(doc_val).upper().strip()
                if j_vessel == d_vessel or _norm(j_vessel) == _norm(d_vessel):
                    status = "MATCH"
                else:
                    status = "MISMATCH"
            else:
                status = "MISMATCH"
        # Special: Carrier name normalization
        elif field == "Carrier":
            if jdx_val and doc_val:
                j_norm = _normalize_carrier_name(jdx_val)
                d_norm = _normalize_carrier_name(doc_val)
                status = "MATCH" if j_norm == d_norm else "MISMATCH"
            elif not jdx_val and doc_val:
                status = "ADDED"
            else:
                status = "MATCH"
        # Special: MBL number suffix matching
        elif field == "MBL_Number" and jdx_val and doc_val:
            j_clean = jdx_val.replace(" ", "")
            d_clean = doc_val.replace(" ", "")
            if j_clean == d_clean or j_clean.endswith(d_clean) or d_clean.endswith(j_clean):
                status = "MATCH"
            else:
                status = "MISMATCH"
        # Special: BL type normalization
        elif field in ("MBL_Type", "HBL_Type"):
            j_norm = "ORIGINAL" if "ORIGINAL" in (jdx_val or "").upper() else "SEA WAYBILL" if "WAYBILL" in (jdx_val or "").upper() else jdx_val
            d_norm = "ORIGINAL" if "ORIGINAL" in (doc_val or "").upper() else "SEA WAYBILL" if "WAYBILL" in (doc_val or "").upper() else doc_val
            status = "MATCH" if _norm(j_norm) == _norm(d_norm) else "MISMATCH"
        else:
            status = _field_status(jdx_val, doc_val)

        result["Carrier"].append({
            "field": field,
            "jordex_value": jdx_val,
            "document_value": doc_val,
            "status": status,
            "source": "tracking" if field == "Vessel_Name" and arrival_vessel else "document",
        })

    # ── CARGO ──
    doc_containers = mbl_data.get("containers", []) if is_direct_file else hbl_data.get("containers", [])
    if not doc_containers:
        doc_containers = mbl_data.get("containers", [])

    for i, jdx_c in enumerate(containers):
        jdx_cno = jdx_c.get("Container_No", "")

        # Find matching document container
        doc_c = {}
        for hc in doc_containers:
            hc_no = hc.get("container_no", "")
            if hc_no and hc_no == jdx_cno:
                doc_c = hc
                break
        if not doc_c and i < len(doc_containers):
            doc_c = doc_containers[i]

        cargo_entry = {"container": jdx_cno or f"Container {i + 1}", "fields": []}

        field_pairs = [
            ("Container_No", jdx_c.get("Container_No", ""),
             doc_c.get("container_no", "")),
            ("Container_Type", _normalize_container_type(jdx_c.get("Container_Type", "")),
             _normalize_container_type(doc_c.get("container_type", ""))),
            ("Qty", jdx_c.get("Qty", ""), doc_c.get("package_qty", "")),
            ("Package_Type", _normalize_pkg_type(jdx_c.get("Package_Type", "")),
             _normalize_pkg_type(doc_c.get("package_type", ""))),
            ("Total_Gross_Weight", jdx_c.get("Total_Gross_Weight", ""),
             doc_c.get("gross_weight", "")),
            ("Volume", jdx_c.get("Volume", ""), doc_c.get("measurement", "")),
            ("Goods_Description", jdx_c.get("Goods_Description", ""),
             doc_c.get("goods_description", "")),
        ]

        for field, jdx_val, doc_val in field_pairs:
            is_num = field in ("Total_Gross_Weight", "Volume", "Qty")
            is_goods = field == "Goods_Description"
            status = _field_status(jdx_val, doc_val,
                                   is_numeric=is_num, is_goods=is_goods)
            cargo_entry["fields"].append({
                "field": field,
                "jordex_value": jdx_val,
                "document_value": doc_val,
                "status": status,
            })

        result["Cargo"].append(cargo_entry)

    # ── ROUTING ──
    for cno, route in routing_data.items():
        track = tracking_results.get(cno, {})
        if not track:
            continue

        lane = route.get("Lane", {})
        routing_entry = {"container": cno, "fields": []}

        has_on_carriage = bool(track.get("carrier_on_carriage"))
        route_pairs = [
            ("Departure", lane.get("Departure_Update") or lane.get("Departure_Original", ""),
             track.get("etd", "")),
            ("Arrival", lane.get("Arrival_Update") or lane.get("Arrival_Original", ""),
             track.get("eta", "")),
            ("Port_of_Loading", lane.get("Port_of_Loading", ""),
             track.get("pol", "")),
            ("Port_of_Discharge", lane.get("Port_of_Discharge", ""),
             track.get("pod", "")),
            ("Voyage", lane.get("Voyage", ""),
             track.get("arrival_voyage") or track.get("loaded_voyage", "")),
        ]
        if not has_on_carriage:
            route_pairs.append(
                ("Terminal", route.get("Destination", {}).get("Terminal", ""),
                 track.get("pod_terminal", ""))
            )

        for field, jdx_val, doc_val in route_pairs:
            status = _field_status(jdx_val, doc_val)
            routing_entry["fields"].append({
                "field": field,
                "jordex_value": jdx_val,
                "document_value": doc_val,
                "status": status,
            })

        result["Routing"].append(routing_entry)

    jdx_carrier = carrier.get("Carrier", "")
    doc_carrier = mbl_data.get("carrier_name", "") or mbl_data.get("carrier_code", "")
    is_manual = (_normalize_carrier_name(jdx_carrier) in ["OOCL", "CMACGM", "CMA", "CMA CGM"] or 
                 _normalize_carrier_name(doc_carrier) in ["OOCL", "CMACGM", "CMA", "CMA CGM"])
    result["is_oocl"] = is_manual

    return result


def _compare_with_ai(system_data: dict, hbl_data: dict, mbl_data: dict,
                     tracking_results: dict, routing_data: dict, is_direct_file: bool = False) -> dict:
    """AI-powered comparison via Gemini. Falls back to string comparison on error."""
    try:
        from google import genai
        from google.genai import types

        api_key = os.getenv("GEMINI_API_KEY", "")
        if not api_key:
            log.warning("No GEMINI_API_KEY — falling back to string comparison")
            return _compare_string(
                system_data, hbl_data, mbl_data, tracking_results, routing_data, is_direct_file
            )

        client = genai.Client(api_key=api_key)

        # Build arrival vessel from tracking
        arrival_vessel = ""
        if tracking_results:
            first_track = next(iter(tracking_results.values()), {})
            arrival_vessel = first_track.get("arrival_vessel", "")
        doc_vessel = arrival_vessel if arrival_vessel else "No tracking result"

        direct_file_rule = ""
        if is_direct_file:
            direct_file_rule = "\nTHIS IS A DIRECT FILE (MBL ONLY).\n- IGNORE HBL_Number and HBL_Type entirely. Do NOT flag them as MISMATCH or ADDED. Return them as MATCH with document_value 'N/A (Direct File)'.\n"

        prompt = f"""You are a global logistics data comparator.
Compare Jordex system data against document extraction data (HBL + MBL).

JORDEX SYSTEM DATA:
{json.dumps(system_data, indent=2)}

HOUSE BL EXTRACTED DATA:
{json.dumps(hbl_data, indent=2)}

MASTER BL EXTRACTED DATA:
{json.dumps(mbl_data, indent=2)}

TRACKING ARRIVAL VESSEL: {doc_vessel}
(Use this for Vessel_Name comparison, NOT the BL vessel. If "No tracking result", set document_value to "No tracking result".)
{direct_file_rule}
═══════════════════════════════════════════
COMPARISON RULES:
═══════════════════════════════════════════

STATUSES:
- "MATCH"    → values represent the same data (allow abbreviations, formatting, suffix diffs)
- "MISMATCH" → values are factually different
- "ADDED"    → Jordex field is empty/missing but document has data (new data to add)

NORMALIZATION:
- Strip CO., LTD., INC, CORP, LIMITED, S.A., LINE, LINES, CONTAINER, GROUP suffixes
- Convert to uppercase, ignore whitespace
- For addresses: "NETHERLANDS" = "THE NETHERLANDS" = "NL"

PARTY RULES:
- Shipper/Consignee/Notify:
  - MATCH if the City, Zip code, and Country match perfectly, even if the Company Name is an abbreviation/short form in one and a full form in the other.
  - MATCH if company name + country match.
  - MATCH if company name, country, and some address words match (e.g., "Young poong co.,ltd... South Korea" vs "YOUNG POONG CO.,LTD... REP. OF KOREA").
  - MISMATCH ONLY if the entities are factually different or located in different cities/countries.
- "SAME AS CONSIGNEE" in notify → copy consignee value, compare against system notify.

CARRIER RULES:
- Carrier: Compare MBL carrier_name vs System Carrier. Use carrier alias mapping:
  OOCL/ORIENT OVERSEAS CONTAINER LINE/OOLU → same carrier → MATCH
  MSC/MEDITERRANEAN SHIPPING COMPANY → same carrier → MATCH
  CMA/CMA CGM → same carrier → MATCH, etc.
- Vessel_Name: Compare tracking arrival vessel (NOT BL vessel) vs System Vessel. Strip IMO numbers in parens.
- MBL_Number: Strip whitespace. If one is suffix of other (carrier prefix) → MATCH.
- MBL_Type/HBL_Type: Normalize to ORIGINAL or SEA WAYBILL. number_of_original=3→ORIGINAL, 0/1→SEA WAYBILL.

CARGO RULES:
- Match containers by Container_No between system and {'MBL' if is_direct_file else 'HBL'}.
- Qty/Weight/Volume: ±0.5% tolerance → MATCH.
- Package_Type: Normalize (CTN=CARTON=Carton, PKG=Package, etc.)
- Goods_Description: Same commodity → MATCH.

═══════════════════════════════════════════
OUTPUT FORMAT (RETURN ONLY VALID JSON):
═══════════════════════════════════════════
{{
  "Parties": [
    {{"field": "Shipper", "jordex_value": "...", "document_value": "...", "status": "MATCH/MISMATCH/ADDED"}},
    {{"field": "Consignee", "jordex_value": "...", "document_value": "...", "status": "..."}},
    {{"field": "Notify_party", "jordex_value": "...", "document_value": "...", "status": "..."}}
  ],
  "Carrier": [
    {{"field": "Carrier", "jordex_value": "...", "document_value": "...", "status": "...", "source": "document"}},
    {{"field": "Vessel_Name", "jordex_value": "...", "document_value": "...", "status": "...", "source": "tracking"}},
    {{"field": "MBL_Type", "jordex_value": "...", "document_value": "...", "status": "...", "source": "document"}},
    {{"field": "MBL_Number", "jordex_value": "...", "document_value": "...", "status": "...", "source": "document"}},
    {{"field": "HBL_Number", "jordex_value": "...", "document_value": "...", "status": "...", "source": "document"}},
    {{"field": "HBL_Type", "jordex_value": "...", "document_value": "...", "status": "...", "source": "document"}}
  ],
  "Cargo": [
    {{
      "container": "ABCD1234567",
      "fields": [
        {{"field": "Container_No", "jordex_value": "...", "document_value": "...", "status": "..."}},
        {{"field": "Container_Type", "jordex_value": "...", "document_value": "...", "status": "..."}},
        {{"field": "Qty", "jordex_value": "...", "document_value": "...", "status": "..."}},
        {{"field": "Package_Type", "jordex_value": "...", "document_value": "...", "status": "..."}},
        {{"field": "Total_Gross_Weight", "jordex_value": "...", "document_value": "...", "status": "..."}},
        {{"field": "Volume", "jordex_value": "...", "document_value": "...", "status": "..."}},
        {{"field": "Goods_Description", "jordex_value": "...", "document_value": "...", "status": "..."}}
      ]
    }}
  ]
}}

Return ONLY the JSON. No explanation, no markdown fences."""

        import time
        response = None
        for attempt in range(3):
            try:
                response = client.models.generate_content(
                    model=os.getenv("GEMINI_MODEL_SMART", "gemini-2.5-flash"),
                    contents=prompt,
                    config=types.GenerateContentConfig(
                        temperature=0.1, max_output_tokens=8192
                    ),
                )
                break
            except Exception as e:
                if attempt == 2:
                    raise e
                time.sleep(2)

        raw = response.text.strip() if response else ""
        clean = raw.replace("```json", "").replace("```", "").strip()

        # Extract JSON
        match = re.search(r'(\{.*\})', clean, re.DOTALL)
        if match:
            clean = match.group(1)

        res = json.loads(clean)

        # Ensure required keys exist
        for key in ["Parties", "Carrier", "Cargo"]:
            if key not in res:
                res[key] = []

        # Routing is handled separately — initialize empty
        res["Routing"] = []

        # Filter out HBL fields if direct file
        if is_direct_file and "Carrier" in res:
            res["Carrier"] = [item for item in res["Carrier"] if item.get("field") not in ("HBL_Number", "HBL_Type")]

        # Add is_oocl flag
        jdx_carrier = system_data.get("Carrier", {}).get("Carrier", "")
        doc_carrier = mbl_data.get("carrier_name", "") or mbl_data.get("carrier_code", "")
        is_manual = (_normalize_carrier_name(jdx_carrier) in ["OOCL", "CMACGM", "CMA", "CMA CGM"] or 
                     _normalize_carrier_name(doc_carrier) in ["OOCL", "CMACGM", "CMA", "CMA CGM"])
        res["is_oocl"] = is_manual

        log.info("AI comparison (Parties/Carrier/Cargo) completed successfully.")
        return res

    except Exception as e:
        log.error("AI comparison failed: %s — falling back to string", e)
        return _compare_string(
            system_data, hbl_data, mbl_data, tracking_results, routing_data
        )


def _compare_routing_with_ai(routing_data: dict, tracking_results: dict) -> list:
    """AI-powered routing comparison per container. Falls back to Python on error."""
    if not routing_data or not tracking_results:
        return []

    try:
        from google import genai
        from google.genai import types

        api_key = os.getenv("GEMINI_API_KEY", "")
        if not api_key:
            log.warning("No GEMINI_API_KEY — falling back to Python routing comparison")
            return _compare_routing_string(routing_data, tracking_results)

        client = genai.Client(api_key=api_key)

        all_routing = []
        for cno, route in routing_data.items():
            track = tracking_results.get(cno, {})
            if not track:
                continue

            has_on_carriage = bool(track.get("carrier_on_carriage"))
            terminal_rule = "SKIP this field entirely — do NOT include Terminal in output at all, because carrier_on_carriage data exists and the actual final delivery point is the on-carriage destination, not the ocean terminal." if has_on_carriage else "Jordex Destination.Terminal vs tracking pod_terminal"

            prompt = f"""You are a logistics routing comparator.
Compare the Jordex routing data for container {cno} against the carrier tracking result.

JORDEX ROUTING DATA (from view_routing.json):
{json.dumps(route, indent=2)}

CARRIER TRACKING RESULT:
{json.dumps(track, indent=2, default=str)}

═══════════════════════════════════════════
COMPARISON RULES:
═══════════════════════════════════════════

STATUSES:
- "MATCH"    → values represent the same data
- "MISMATCH" → values are factually different
- "ADDED"    → Jordex field is empty but tracking has data

FIELDS TO COMPARE:
1. Departure: Jordex Lane.Departure_Update or Lane.Departure_Original vs tracking etd
2. Arrival: Jordex Lane.Arrival_Update or Lane.Arrival_Original vs tracking eta
3. Port_of_Loading: Jordex Lane.Port_of_Loading vs tracking pol
4. Port_of_Discharge: Jordex Lane.Port_of_Discharge vs tracking pod
5. Voyage: Jordex Lane.Voyage vs tracking arrival_voyage or loaded_voyage
6. Terminal: {terminal_rule}

COMPLEX FIELDS (Transits and On-Carriage):
If tracking data has `transshipments` or `carrier_on_carriage`, compare them against Jordex `Lane.Transits` and `Lane.Carrier_On_Carriage`.
- For each transshipment, output 3 SEPARATE fields exactly like this:
  1. {{"field": "Transshipment_Port (<tracking port name>)", "document_value": "<tracking port>", "jordex_value": "<jordex port or empty>", "status": "..."}}
  2. {{"field": "Transshipment_ETA (<tracking port name>)", "document_value": "<tracking eta>", "jordex_value": "<jordex eta or empty>", "status": "..."}}
  3. {{"field": "Transshipment_ETD (<tracking port name>)", "document_value": "<tracking etd>", "jordex_value": "<jordex etd or empty>", "status": "..."}}
- For each on-carriage, output 2 SEPARATE fields exactly like this:
  1. {{"field": "Carrier_On_Carriage_Place (<tracking arrival place>)", "document_value": "<tracking place>", "jordex_value": "<jordex place or empty>", "status": "..."}}
  2. {{"field": "Carrier_On_Carriage_Date (<tracking arrival place>)", "document_value": "<tracking date>", "jordex_value": "<jordex date or empty>", "status": "..."}}

DATE RULES: Dates must match exactly. Any difference in dates (even 1 day) → MISMATCH.
PORT RULES: Same port city → MATCH even if format differs (e.g., "ROTTERDAM" vs "Rotterdam, Netherlands", or "SHANGHAI" vs "Shanghai Pt, China").
TERMINAL RULES: A MATCH occurs if the terminal names conceptually refer to the exact same terminal.

OUTPUT FORMAT (RETURN ONLY VALID JSON ARRAY):
[
  {{"field": "Departure", "jordex_value": "...", "document_value": "...", "status": "MATCH/MISMATCH/ADDED"}},
  {{"field": "Arrival", "jordex_value": "...", "document_value": "...", "status": "..."}},
  {{"field": "Port_of_Loading", "jordex_value": "...", "document_value": "...", "status": "..."}},
  {{"field": "Port_of_Discharge", "jordex_value": "...", "document_value": "...", "status": "..."}},
  {{"field": "Voyage", "jordex_value": "...", "document_value": "...", "status": "..."}},
  {{"field": "Terminal", "jordex_value": "...", "document_value": "...", "status": "..."}},
  {{"field": "Transshipment_Port (SHANGHAI)", "jordex_value": "...", "document_value": "...", "status": "..."}},
  {{"field": "Transshipment_ETA (SHANGHAI)", "jordex_value": "...", "document_value": "...", "status": "..."}},
  {{"field": "Transshipment_ETD (SHANGHAI)", "jordex_value": "...", "document_value": "...", "status": "..."}}
]

Return ONLY the JSON array. No explanation, no markdown fences."""

            import time
            response = None
            for attempt in range(3):
                try:
                    response = client.models.generate_content(
                        model=os.getenv("GEMINI_MODEL_SMART", "gemini-2.5-flash"),
                        contents=prompt,
                        config=types.GenerateContentConfig(
                            temperature=0.1, max_output_tokens=4096
                        ),
                    )
                    break
                except Exception as e:
                    if attempt == 2:
                        raise e
                    time.sleep(2)

            raw = (response.text or "").strip() if response else ""
            log.info("AI routing raw response length: %d chars", len(raw))
            clean = raw.replace("```json", "").replace("```", "").strip()
            match_obj = re.search(r'(\[.*\])', clean, re.DOTALL)
            if match_obj:
                clean = match_obj.group(1)
            else:
                log.warning("AI routing: No JSON array found in response. Raw: %s", raw[:500])

            try:
                fields = json.loads(clean)
                log.info("AI routing parsed %d fields for %s", len(fields), cno)
            except Exception as parse_err:
                log.error("AI routing JSON parse failed for %s: %s. Raw: %s", cno, parse_err, raw[:500])
                fields = []

            # ── Fallback: if AI returned empty, use string comparison for this container ──
            if not fields:
                log.warning("AI returned empty fields for %s — falling back to string comparison.", cno)
                fallback = _compare_routing_string({cno: route}, tracking_results)
                if fallback:
                    all_routing.append(fallback[0])
                    continue

            # Safe strip and handle Transhipments
            for f in fields:
                if 'jordex_value' in f:
                    f['jordex_value'] = str(f['jordex_value'] or "").strip()
                if 'document_value' in f:
                    f['document_value'] = str(f['document_value'] or "").strip()
            # Inject raw_data into AI-generated complex fields
            t_data = track.get("transshipments", [])
            o_data = track.get("carrier_on_carriage", [])
            for f in fields:
                if f.get("field", "").startswith("Transshipment_Port"):
                    m = re.search(r'\((.*)\)', f["field"])
                    if m:
                        p_name = m.group(1).upper().strip()
                        for ts in t_data:
                            if str(ts.get("port") or "").upper().strip() == p_name:
                                f["raw_data"] = json.dumps(ts)
                                break
                elif f.get("field", "").startswith("Carrier_On_Carriage"):
                    m = re.search(r'\((.*)\)', f["field"])
                    if m:
                        p_name = m.group(1).upper().strip()
                        for oc in o_data:
                            if str(oc.get("arrival_place") or "").upper().strip() == p_name:
                                f["raw_data"] = json.dumps(oc)
                                break

            normal_fields = []
            transits_grouped = []
            coc_grouped = []
            
            t_map = {}
            c_map = {}
            
            for f in fields:
                fname = f.get("field", "")
                if fname.startswith("Transshipment_"):
                    m = re.search(r'\((.*?)\)', fname)
                    key = m.group(1) if m else "Unknown"
                    if key not in t_map:
                        t_map[key] = []
                        transits_grouped.append(t_map[key])
                    t_map[key].append(f)
                elif fname.startswith("Carrier_On_Carriage_"):
                    m = re.search(r'\((.*?)\)', fname)
                    key = m.group(1) if m else "Unknown"
                    if key not in c_map:
                        c_map[key] = []
                        coc_grouped.append(c_map[key])
                    c_map[key].append(f)
                else:
                    normal_fields.append(f)
                    
            all_routing.append({
                "container": cno, 
                "fields": normal_fields,
                "transits": transits_grouped,
                "carrier_on_carriage": coc_grouped
            })

        log.info("AI routing comparison completed for %d container(s).", len(all_routing))
        return all_routing

    except Exception as e:
        log.error("AI routing comparison failed: %s — falling back to Python", e)
        return _compare_routing_string(routing_data, tracking_results)


def _compare_routing_string(routing_data: dict, tracking_results: dict) -> list:
    """Pure Python routing comparison (fallback)."""
    result = []
    for cno, route in routing_data.items():
        track = tracking_results.get(cno, {})
        if not track:
            continue

        lane = route.get("Lane", {})
        routing_entry = {"container": cno, "fields": []}

        has_on_carriage = bool(track.get("carrier_on_carriage"))
        route_pairs = [
            ("Departure", lane.get("Departure_Update") or lane.get("Departure_Original", ""),
             track.get("etd", "")),
            ("Arrival", lane.get("Arrival_Update") or lane.get("Arrival_Original", ""),
             track.get("eta", "")),
            ("Port_of_Loading", lane.get("Port_of_Loading", ""),
             track.get("pol", "")),
            ("Port_of_Discharge", lane.get("Port_of_Discharge", ""),
             track.get("pod", "")),
            ("Voyage", lane.get("Voyage", ""),
             track.get("arrival_voyage") or track.get("loaded_voyage", "")),
        ]
        if not has_on_carriage:
            route_pairs.append(
                ("Terminal", route.get("Destination", {}).get("Terminal", ""),
                 track.get("pod_terminal", ""))
            )

        for field, jdx_val, doc_val in route_pairs:
            status = _field_status(jdx_val, doc_val)
            routing_entry["fields"].append({
                "field": field,
                "jordex_value": jdx_val,
                "document_value": doc_val,
                "status": status,
            })

        transshipments = track.get("transshipments", [])
        for ts in transshipments:
            ts_port = ts.get("port")
            if ts_port:
                eta = ts.get("eta") or ""
                etd = ts.get("etd") or ""
                vessel = ts.get("vessel_in") or ""
                routing_entry["fields"].append({
                    "field": f"Transshipment_Port ({ts_port})",
                    "jordex_value": "",
                    "document_value": f"ETA: {eta}, ETD: {etd}, Vessel: {vessel}",
                    "status": "Added MARK"
                })

        result.append(routing_entry)
    return result


# ═══════════════════════════════════════════════════════════════════════
#  SEARCH + OPEN SHIPMENT
# ═══════════════════════════════════════════════════════════════════════

# def search_and_open_shipment(page: Page, ref_no: str, date_str: str,
#                              log_fn=None) -> bool:
#     """Use Jordex search bar to find and open a shipment."""
#     def status(msg):
#         if log_fn:
#             log_fn(msg)
#         log.info(msg)

#     status(f"Searching for {ref_no}...")

#     filled = False
#     try:
#         search_input = page.locator(
#             "input.el-input__inner[placeholder='Search']"
#         ).first
#         if search_input.is_visible(timeout=5000):
#             search_input.click()
#             page.wait_for_timeout(300)
#             search_input.fill("")
#             search_input.fill(ref_no)
#             page.wait_for_timeout(500)
#             search_input.press("Enter")
#             filled = True
#     except Exception:
#         pass

#     if not filled:
#         filled = page.evaluate(f"""() => {{
#             const inps = [...document.querySelectorAll('input.el-input__inner')];
#             const s = inps.find(i => (i.placeholder||'').toLowerCase().includes('search'));
#             if (s) {{
#                 s.focus(); s.value = '{ref_no}';
#                 s.dispatchEvent(new Event('input', {{bubbles:true}}));
#                 s.dispatchEvent(new KeyboardEvent('keydown',
#                     {{key:'Enter',keyCode:13,bubbles:true}}));
#                 return true;
#             }}
#             return false;
#         }}""")

#     if not filled:
#         status(f"Search bar not found for {ref_no}")
#         return False

#     # Wait for results
#     page.wait_for_timeout(2000)
#     try:
#         page.locator(".el-loading-mask").wait_for(state="hidden", timeout=15000)
#     except Exception:
#         pass
#     page.wait_for_timeout(2000)

#     # Find the row
#     target_row = None
#     rows = get_all_rows_for_date(page, date_str)
#     for r in rows:
#         if extract_ref_number(r).strip() == ref_no:
#             target_row = r
#             break

#     if not target_row:
#         try:
#             all_rows = page.locator(".el-table__body tr").all()
#             for r in all_rows:
#                 if not r.is_visible():
#                     continue
#                 if extract_ref_number(r).strip() == ref_no:
#                     target_row = r
#                     break
#         except Exception:
#             pass

#     if not target_row:
#         status(f"Could not find row for {ref_no}")
#         return False

#     target_row.click(timeout=10000)
#     page.wait_for_load_state("load", timeout=30000)
#     _apply_zoom(page)
#     page.wait_for_selector(".el-tabs__item", timeout=20000)
#     return True

def search_and_open_shipment(page: Page, ref_no: str, date_str: str,
                             log_fn=None) -> bool:
    """
    Use Jordex search bar to find and open a shipment.
    After searching, scans ALL visible rows for the ref number
    (not filtered by date, since date format may differ in search results).
    """
    def status(msg):
        if log_fn:
            log_fn(msg)
        log.info(msg)

    status(f"Searching for {ref_no}...")

    # ── Fill search bar ──
    filled = False
    try:
        search_input = page.locator(
            "input.el-input__inner[placeholder='Search']"
        ).first
        if search_input.is_visible(timeout=5000):
            search_input.click()
            page.wait_for_timeout(300)
            search_input.fill("")
            search_input.fill(ref_no)
            page.wait_for_timeout(500)
            search_input.press("Enter")
            filled = True
    except Exception:
        pass

    if not filled:
        filled = page.evaluate(f"""() => {{
            const inps = [...document.querySelectorAll('input.el-input__inner')];
            const s = inps.find(i => (i.placeholder||'').toLowerCase().includes('search'));
            if (s) {{
                s.focus();
                s.value = '{ref_no}';
                s.dispatchEvent(new Event('input', {{bubbles:true}}));
                s.dispatchEvent(new KeyboardEvent('keydown',
                    {{key:'Enter',keyCode:13,bubbles:true}}));
                return true;
            }}
            return false;
        }}""")

    if not filled:
        status(f"Search bar not found for {ref_no}")
        return False

    # ── Wait for table to reload ──
    page.wait_for_timeout(2000)
    try:
        page.locator(".el-loading-mask").wait_for(state="visible", timeout=3000)
    except Exception:
        pass
    try:
        page.locator(".el-loading-mask").wait_for(state="hidden", timeout=15000)
    except Exception:
        pass
    page.wait_for_timeout(2000)

    # ── Find row: scan ALL visible rows for ref number ──
    # Do NOT filter by date — search results may show different date format
    target_row = _find_row_by_ref(page, ref_no)

    if not target_row:
        status(f"Could not find row for {ref_no}")
        return False

    status(f"Found row for {ref_no} — opening...")
    try:
        target_row.scroll_into_view_if_needed(timeout=5000)
        target_row.click(timeout=10000)
        page.wait_for_load_state("load", timeout=30000)
        _apply_zoom(page)
        page.wait_for_selector(".el-tabs__item", timeout=20000)
        return True
    except Exception as e:
        log.warning("Row click failed for %s: %s", ref_no, e)
        # Force click fallback
        try:
            target_row.evaluate("el => el.click()")
            page.wait_for_load_state("load", timeout=20000)
            _apply_zoom(page)
            page.wait_for_selector(".el-tabs__item", timeout=15000)
            return True
        except Exception as e2:
            log.error("Force click also failed for %s: %s", ref_no, e2)
            return False


def _find_row_by_ref(page: Page, ref_no: str) -> object:
    """
    Scan all visible table rows and return the one matching ref_no.
    Tries multiple selectors and matching strategies.
    """
    ref_upper = ref_no.strip().upper()

    row_selectors = [
        ".el-table__body tr",
        ".el-table__body .el-table__row",
        "table tbody tr",
        "tr.selectable",
    ]

    for sel in row_selectors:
        try:
            rows = page.locator(sel).all()
            if not rows:
                continue
            for row in rows:
                try:
                    if not row.is_visible(timeout=500):
                        continue
                    text = row.inner_text(timeout=1000).strip().upper()
                    if not text:
                        continue
                    # Direct match
                    if ref_upper in text:
                        log.info("  Row match (text contains): %s", ref_upper)
                        return row
                    # Pattern match — extract any OI/01PKG number from row
                    found = REF_PATTERN.findall(text)
                    for f in found:
                        if f.upper().replace(" ", "_") == ref_upper.replace(" ", "_"):
                            log.info("  Row match (pattern): %s", f)
                            return row
                except Exception:
                    continue
        except Exception:
            continue

    # ── JS fallback: search every cell for the ref ──
    log.info("  DOM scan fallback for %s...", ref_upper)
    try:
        row_idx = page.evaluate("""(refNo) => {
            const rows = document.querySelectorAll(
                '.el-table__body tr, .el-table__body .el-table__row, table tbody tr'
            );
            for (let i = 0; i < rows.length; i++) {
                const text = (rows[i].innerText || '').toUpperCase().trim();
                if (text.includes(refNo)) return i;
            }
            return -1;
        }""", ref_upper)

        if row_idx >= 0:
            # Re-locate by index
            for sel in row_selectors:
                try:
                    rows = page.locator(sel).all()
                    if rows and row_idx < len(rows):
                        r = rows[row_idx]
                        if r.is_visible(timeout=500):
                            log.info("  Row match (JS idx %d)", row_idx)
                            return r
                except Exception:
                    continue
    except Exception as e:
        log.warning("JS fallback failed: %s", e)

    return None

def go_back_to_list(page: Page):
    """Return to shipment list."""
    try:
        if page.locator(".el-table__body").is_visible(timeout=1000) and not page.locator("text='Parties'").is_visible():
            return
            
        breadcrumb = page.locator(".el-breadcrumb__item:has-text('Shipments')").first
        if breadcrumb.is_visible(timeout=1000):
            breadcrumb.click()
            page.wait_for_timeout(2000)
        else:
            page.go_back(timeout=5000)
            page.wait_for_timeout(2000)
            
        _apply_zoom(page)
    except Exception:
        try:
            if "shipments/ocean" not in page.url:
                page.goto(
                    "https://jit.jordex.com/shipments/ocean",
                    wait_until="load"
                )
            else:
                apply_filters(page)
            page.wait_for_selector(
                ".el-table__body", state="visible", timeout=15000
            )
        except Exception as e:
            log.error("Could not return to list: %s", e)


# ═══════════════════════════════════════════════════════════════════════
#  COMPLETE TASK IN JORDEX
# ═══════════════════════════════════════════════════════════════════════
def complete_task_logic(page: Page, stage: str) -> bool:
    """Handles task completion. Use _complete_task_with_retry for full flow."""
    if stage == "full":
        return _complete_task_with_retry(page, max_retries=3)

    try:
        if stage == "prepare":
            try:
                task_btn = page.get_by_text('Check shipment', exact=True).first
                if task_btn.is_visible(timeout=5000):
                    task_btn.click()
                else:
                    page.evaluate(
                        "() => [...document.querySelectorAll('p')]"
                        ".find(el => el.textContent.trim() === 'Check shipment')?.click()"
                    )
            except Exception:
                page.evaluate(
                    "() => [...document.querySelectorAll('p')]"
                    ".find(el => el.textContent.trim() === 'Check shipment')?.click()"
                )
            page.wait_for_timeout(3000)

            # Wait for drawer with retry
            drawer_open = False
            for retry in range(3):
                try:
                    page.wait_for_selector("textarea, input[placeholder='Task status']",
                                           state="visible", timeout=5000)
                    drawer_open = True
                    break
                except Exception:
                    log.warning("Drawer not open, retry %d...", retry + 1)
                    page.wait_for_timeout(2000)
                    try:
                        page.locator("p:text-is('Check shipment')").first.evaluate("el => el.click()")
                    except Exception:
                        pass

            if not drawer_open:
                log.error("Check shipment drawer failed to open after retries")
                return False

            status_input = page.locator('input[placeholder="Task status"]').first
            if status_input.is_visible(timeout=3000):
                status_input.click()
            else:
                page.evaluate(
                    "() => document.querySelector('input[placeholder=\"Task status\"]')?.click()"
                )
            page.wait_for_timeout(1000)

            try:
                completed_option = page.get_by_text('Completed', exact=True).first
                if completed_option.is_visible(timeout=3000):
                    completed_option.click()
                else:
                    page.evaluate(
                        "() => [...document.querySelectorAll('.el-select-dropdown__item span')]"
                        ".find(el => el.textContent.trim() === 'Completed')?.click()"
                    )
            except Exception:
                page.evaluate(
                    "() => [...document.querySelectorAll('.el-select-dropdown__item span')]"
                    ".find(el => el.textContent.trim() === 'Completed')?.click()"
                )
            page.wait_for_timeout(1000)
            return True

        elif stage == "commit":
            return _click_save(page)

    except Exception as e:
        log.error("complete_task_logic (%s) failed: %s", stage, e)
        return False
    return False


# ═══════════════════════════════════════════════════════════════════════
#  PROCESS ONE SHIPMENT (FULL PIPELINE)
# ═══════════════════════════════════════════════════════════════════════

def merge_hbl_data(hbl_list: list) -> tuple:
    """
    Merge multiple HBL extraction results into a unified structure.

    Returns: (merged_hbl_data: dict, hbl_numbers: list, merge_type: str)
      merge_type: 'single', 'same_hbl', 'same_containers_sum', 'different_containers'
    """
    if not hbl_list:
        return {}, [], "single"
    if len(hbl_list) == 1:
        ref = hbl_list[0].get("reference_number", "")
        return hbl_list[0], [ref] if ref else [], "single"

    # Collect all HBL numbers
    hbl_numbers = []
    for h in hbl_list:
        ref = (h.get("reference_number") or "").strip()
        if ref and ref not in hbl_numbers:
            hbl_numbers.append(ref)

    # CASE 1: All HBL numbers identical → use first one
    if len(hbl_numbers) <= 1:
        log.info("Multiple HBL files but same HBL number — using first.")
        return hbl_list[0], hbl_numbers, "same_hbl"

    # Collect all containers across all HBLs
    all_containers = []
    for h in hbl_list:
        for c in h.get("containers", []):
            cno = (c.get("container_no") or "").strip().upper()
            if cno:
                all_containers.append((cno, c))

    # Get unique container numbers
    unique_cnos = list(dict.fromkeys([cno for cno, _ in all_containers]))

    # CASE 2: Different HBL numbers but ALL share the same container(s) → sum quantities
    hbl_container_sets = []
    for h in hbl_list:
        cnos = set()
        for c in h.get("containers", []):
            cn = (c.get("container_no") or "").strip().upper()
            if cn:
                cnos.add(cn)
        hbl_container_sets.append(cnos)

    all_same_containers = all(s == hbl_container_sets[0] for s in hbl_container_sets) if hbl_container_sets else False

    if all_same_containers and len(unique_cnos) > 0:
        log.info("Multiple HBLs, different numbers, same containers — summing package data.")
        merged = dict(hbl_list[0])  # copy base structure
        merged_containers = []

        for target_cno in unique_cnos:
            # Gather all entries for this container across HBLs
            entries = [c for cno, c in all_containers if cno == target_cno]
            if not entries:
                continue

            base = dict(entries[0])
            total_qty = 0
            total_weight = 0.0
            total_volume = 0.0
            pkg_type = ""

            for e in entries:
                # Qty
                raw_qty = re.sub(r"[^\d.]", "", str(e.get("package_qty") or "0").replace(",", ""))
                try:
                    total_qty += int(float(raw_qty)) if raw_qty else 0
                except ValueError:
                    pass

                # Weight
                raw_wt = re.sub(r"[^\d.]", "", str(e.get("gross_weight") or "0").replace(",", ""))
                try:
                    total_weight += float(raw_wt) if raw_wt else 0
                except ValueError:
                    pass

                # Volume
                raw_vol = re.sub(r"[^\d.]", "", str(e.get("measurement") or "0").replace(",", ""))
                try:
                    total_volume += float(raw_vol) if raw_vol else 0
                except ValueError:
                    pass

                # Package type — take first non-empty
                if not pkg_type:
                    pkg_type = (e.get("package_type") or "").strip()

            base["package_qty"] = str(total_qty) if total_qty else base.get("package_qty")
            base["gross_weight"] = f"{total_weight:.3f} KGS" if total_weight else base.get("gross_weight")
            base["measurement"] = f"{total_volume:.3f} CBM" if total_volume else base.get("measurement")
            if pkg_type:
                base["package_type"] = pkg_type

            merged_containers.append(base)

        merged["containers"] = merged_containers
        return merged, hbl_numbers, "same_containers_sum"

    # CASE 3: Different HBL numbers, different containers → combine all
    log.info("Multiple HBLs, different numbers, different containers — combining all.")
    merged = dict(hbl_list[0])
    combined_containers = []
    seen_cnos = set()

    for h in hbl_list:
        for c in h.get("containers", []):
            cno = (c.get("container_no") or "").strip().upper()
            if cno and cno not in seen_cnos:
                combined_containers.append(c)
                seen_cnos.add(cno)

    merged["containers"] = combined_containers
    return merged, hbl_numbers, "different_containers"

def _complete_task_with_retry(page: Page, max_retries: int = 3) -> bool:
    """Click Check shipment, set Completed, Save — with retries for slow-loading drawer."""
    for attempt in range(1, max_retries + 1):
        try:
            log.info("Complete task attempt %d/%d", attempt, max_retries)

            # Wait for loading masks to clear
            try:
                page.locator(".el-loading-mask:visible").wait_for(state="hidden", timeout=5000)
            except Exception:
                pass

            # Click "Check shipment"
            clicked = False
            for sel in [
                "p:text-is('Check shipment')",
                ".mf-tasks-title:text-is('Check shipment')",
            ]:
                try:
                    el = page.locator(sel).first
                    if el.is_visible(timeout=3000):
                        el.click(timeout=5000)
                        clicked = True
                        break
                except Exception:
                    continue

            if not clicked:
                page.evaluate(
                    "() => [...document.querySelectorAll('p')]"
                    ".find(el => el.textContent.trim() === 'Check shipment')?.click()"
                )

            # Wait for drawer to open — textarea is the signal
            try:
                page.wait_for_selector("textarea", state="visible", timeout=5000)
            except Exception:
                log.warning("Drawer did not open on attempt %d, retrying...", attempt)
                page.wait_for_timeout(2000)
                # Force click fallback
                try:
                    page.locator("p:text-is('Check shipment')").first.evaluate("el => el.click()")
                    page.wait_for_selector("textarea", state="visible", timeout=5000)
                except Exception:
                    if attempt < max_retries:
                        page.wait_for_timeout(3000)
                        continue
                    else:
                        log.error("All retries exhausted for opening drawer")
                        return False

            page.wait_for_timeout(1500)

            # Set status to Completed
            status_input = page.locator('input[placeholder="Task status"]').first
            if status_input.is_visible(timeout=3000):
                status_input.click()
            else:
                page.evaluate(
                    "() => document.querySelector('input[placeholder=\"Task status\"]')?.click()"
                )
            page.wait_for_timeout(1000)

            # Select "Completed"
            sel_ok = False
            try:
                opt = page.get_by_text('Completed', exact=True).first
                if opt.is_visible(timeout=3000):
                    opt.click()
                    sel_ok = True
            except Exception:
                pass
            if not sel_ok:
                page.evaluate(
                    "() => [...document.querySelectorAll('.el-select-dropdown__item span')]"
                    ".find(el => el.textContent.trim() === 'Completed')?.click()"
                )
            page.wait_for_timeout(1000)

            # Save
            saved = _click_save(page)
            if saved:
                log.info("Complete task succeeded on attempt %d", attempt)
                return True
            else:
                log.warning("Save failed on attempt %d", attempt)
                if attempt < max_retries:
                    page.wait_for_timeout(2000)
                    continue
                return False

        except Exception as e:
            log.error("Complete task attempt %d error: %s", attempt, e)
            if attempt < max_retries:
                page.wait_for_timeout(3000)
            else:
                return False

    return False
def process_single_shipment(page: Page, row, ref_no: str, date_str: str,
                            shipment_key: str, checked: set, no_doc: dict,
                            log_status) -> str:
    """
    Returns: 'processed', 'no_doc', 'old_doc_support', or 'skipped'
    """
    save_dir = os.path.join(SHIPMENTS_ROOT, shipment_key)
    comparison_path = os.path.join(save_dir, "Comparison_Result.json")

    # ── Stale no_doc re-check (6 hour window) ──
    if shipment_key in no_doc:
        entry = no_doc[shipment_key]
        last_checked_str = entry.get("last_checked", "")
        try:
            last_dt = datetime.fromisoformat(last_checked_str)
            hours_ago = (datetime.now() - last_dt).total_seconds() / 3600
            if hours_ago < 6:
                log.info("Skipping %s — checked %.1f hours ago (< 6h).", ref_no, hours_ago)
                return "skipped"
            else:
                log.info("Re-checking %s — last checked %.1f hours ago.", ref_no, hours_ago)
        except Exception:
            pass
    elif shipment_key in checked:
        log.info("Skipping %s — already fully checked.", ref_no)
        return "skipped"

    # Click to open shipment
    opened = False
    for click_attempt in range(3):
        try:
            row.click(timeout=10000)
            page.wait_for_load_state("load", timeout=30000)
            _apply_zoom(page)
            page.get_by_text("Parties", exact=True).first.wait_for(state="visible", timeout=20000)
            opened = True
            break
        except Exception as e:
            log.warning("Click attempt %d failed for %s: %s", click_attempt + 1, ref_no, e)
            if click_attempt < 2:
                try:
                    row.evaluate("el => el.click()")
                except Exception:
                    pass
                page.wait_for_timeout(3000)

    if not opened:
        go_back_to_list(page)
        return "skipped"

    try:
        # STEP 1: Check + Download Documents
        log_status(f"Ref {ref_no}: Checking documents...")
        docs_found, original_paths, save_dir = process_shipment_documents(
            page, date_str, ref_no
        )

        if not docs_found:
            log_status(f"Ref {ref_no}: No documents found.")

            # ── Was in no_doc, still no docs → update timestamp and keep ──
            if shipment_key in no_doc:
                log_status(f"Ref {ref_no}: Re-check confirms still no docs.")
                add_no_doc_entry(no_doc, shipment_key, ref_no, date_str)
                save_no_doc(no_doc)
                return "no_doc"

            if is_past_date(date_str):
                log_status(f"Ref {ref_no}: Past date. Scraping for Old Doc Support...")
                if save_dir is None:
                    save_dir = make_shipment_folder(date_str, ref_no)
                if not os.path.exists(save_dir):
                    os.makedirs(save_dir, exist_ok=True)

                system_data = {
                    "Parties": scrape_parties(page),
                    "Carrier": scrape_carrier(page),
                    "Cargo": scrape_cargo(page, ref_no),
                }

                sys_path = os.path.join(save_dir, "System_Data.json")
                with open(sys_path, "w", encoding="utf-8") as f:
                    json.dump(system_data, f, indent=2, ensure_ascii=False)

                missing = []
                carrier_data = system_data.get("Carrier", {})
                cargo_data = system_data.get("Cargo", [])

                hbl = carrier_data.get("HBL_Number", "").strip()
                mbl = carrier_data.get("MBL_Number", "").strip()
                if not hbl and not mbl:
                    missing.append("HBL, MBL Number")
                elif not hbl:
                    missing.append("HBL Number")
                elif not mbl:
                    missing.append("MBL Number")

                containers = [c.get("Container_No", "").strip() for c in cargo_data if c.get("Container_No", "").strip()]
                # Check load type — LCL shipments don't have container numbers
                is_lcl_shipment = any(
                    "LCL" in str(c.get("Load_Type", "")).upper()
                    for c in cargo_data
                )
                if not is_lcl_shipment and not containers:
                    missing.append("Container Number")

                carrier_code = carrier_data.get("Carrier", "").strip()
                vessel = carrier_data.get("Vessel_Name", "").strip()
                if not carrier_code and not vessel:
                    missing.append("Carrier and Vessel")
                elif not carrier_code:
                    missing.append("Carrier")
                elif not vessel:
                    missing.append("Vessel")

                comment = (", ".join(missing) + " and Documents Not Available") if missing else "No Documents Available"

                res_data = {
                    "comment": comment,
                    "status": "pending_old_doc_support",
                    "ref_no": ref_no,
                    "date_str": date_str,
                    "processed_at": datetime.now().isoformat()
                }
                res_path = os.path.join(save_dir, "result.json")
                with open(res_path, "w", encoding="utf-8") as f:
                    json.dump(res_data, f, indent=2, ensure_ascii=False)

                add_no_doc_entry(no_doc, shipment_key, ref_no, date_str, old_doc_support=True)
                save_no_doc(no_doc)
                return "old_doc_support"
            else:
                add_no_doc_entry(no_doc, shipment_key, ref_no, date_str)
                save_no_doc(no_doc)
                return "no_doc"

        # ── If was in no_doc but now has docs → remove from no_doc ──
        if shipment_key in no_doc:
            log_status(f"Ref {ref_no}: Documents found on re-check! Processing fully.")
            remove_no_doc_entry(no_doc, shipment_key)
            save_no_doc(no_doc)

        write_processing_log(save_dir, ref_no, date_str, "docs_downloaded",
                             f"{len(original_paths)} original files")

        executor = concurrent.futures.ThreadPoolExecutor(max_workers=5)

        # STEP 2: Document Extraction (background)
        log_status(f"Ref {ref_no}: Starting document extraction...")
        extraction_future = executor.submit(
            extract_documents, original_paths, save_dir, log_status
        )

        # STEP 3: DOM-scrape System Data
        log_status(f"Ref {ref_no}: Scraping Jordex system data...")
        system_data = {
            "Parties": scrape_parties(page),
            "Carrier": scrape_carrier(page),
            "Cargo": scrape_cargo(page, ref_no),
        }

        sys_path = os.path.join(save_dir, "System_Data.json")
        with open(sys_path, "w", encoding="utf-8") as f:
            json.dump(system_data, f, indent=2, ensure_ascii=False)
        write_processing_log(save_dir, ref_no, date_str, "system_scraped")

        # ── Identify PRIMARY container (first in Jordex cargo list) ──
        all_container_nos = [
            c.get("Container_No", "").strip()
            for c in system_data.get("Cargo", [])
            if c.get("Container_No", "").strip()
        ]
        primary_container = all_container_nos[0] if all_container_nos else None

        carrier_str = system_data.get("Carrier", {}).get("Carrier", "")
        matched_carrier = None
        if Tracking:
            matched_carrier = Tracking.find_carrier_code(carrier_str)

        hbl_data = {}
        mbl_data = {}
        hbl_numbers = []
        merge_type = "single"
        extraction_done = False

        # If carrier not matched from Jordex, wait for extraction
        if Tracking and not matched_carrier:
            log_status(f"Ref {ref_no}: Carrier '{carrier_str}' not matched. Waiting for extraction...")
            try:
                extraction = extraction_future.result(timeout=300)
                hbl_list = extraction.get("hbl", [])
                mbl_data = extraction.get("mbl") or {}
                extraction_done = True
                write_processing_log(save_dir, ref_no, date_str, "extracted")
                mbl_carrier = mbl_data.get("carrier_name", "")
                matched_carrier = Tracking.find_carrier_code(mbl_carrier)
            except Exception as e:
                log.error(f"Extraction failed for {ref_no}: {e}")

        # STEP 4: Tracking — ONLY primary container
        tracking_futures = {}
        resolved_carrier = _normalize_carrier_name(matched_carrier) if matched_carrier else ""

        if resolved_carrier == "OOCL":
            log_status(f"Ref {ref_no}: OOCL — skipping auto tracking.")
        elif matched_carrier and matched_carrier.upper() in SUPPORTED_CARRIERS and primary_container and Tracking:
            log_status(f"Ref {ref_no}: Tracking primary container {primary_container} only.")
            tracking_futures[primary_container] = executor.submit(
                Tracking.track_shipment_thread_safe, matched_carrier,
                primary_container, save_dir, log_status
            )

        # STEP 5: Scrape routing
        log_status(f"Ref {ref_no}: Scraping routing data...")
        routing_data = {}
        for attempt in range(3):
            try:
                routing_data = scrape_routing(page, save_dir, all_container_nos)
                if routing_data:
                    break
            except Exception as e:
                log.warning(f"Routing scrape attempt {attempt + 1} failed: {e}")
                time.sleep(2)
        write_processing_log(save_dir, ref_no, date_str, "routing_scraped")
        # STEP 6: Wait for background tasks
        log_status(f"Ref {ref_no}: Waiting for background tasks...")

        if not extraction_done:
            try:
                extraction = extraction_future.result(timeout=300)
                write_processing_log(save_dir, ref_no, date_str, "extracted")
            except Exception as e:
                log.error(f"Extraction failed for {ref_no}: {e}")
                extraction = {"hbl": [], "mbl": None}

        # ── Merge multiple HBLs ──
        hbl_list = extraction.get("hbl", [])
        mbl_data = extraction.get("mbl") or {}

        if hbl_list:
            hbl_data, hbl_numbers, merge_type = merge_hbl_data(hbl_list)
            log_status(f"Ref {ref_no}: HBL merge type: {merge_type}, HBL count: {len(hbl_list)}, unique numbers: {hbl_numbers}")
        else:
            hbl_data = {}
            hbl_numbers = []

        tracking_results = {}
        for cno, fut in tracking_futures.items():
            try:
                tracking_results[cno] = fut.result(timeout=300)
            except Exception as e:
                log.error(f"Tracking failed for {cno}: {e}")

        executor.shutdown(wait=False)

        # Save extraction data
        extract_path = os.path.join(save_dir, "Extraction_Data.json")
        try:
            with open(extract_path, "w", encoding="utf-8") as f:
                json.dump({
                    "hbl": hbl_data, "mbl": mbl_data,
                    "all_hbls": hbl_list,
                    "hbl_numbers": hbl_numbers,
                    "merge_type": merge_type,
                }, f, indent=2, ensure_ascii=False)
        except Exception as e:
            log.error("Failed to write Extraction_Data.json: %s", e)

        # Direct file detection
        is_direct_file = not bool(hbl_list)
        if is_direct_file:
            log_status(f"Ref {ref_no}: DIRECT FILE (MBL only).")
            hbl_data = mbl_data.copy()

        # STEP 7: Comparison
        log_status(f"Ref {ref_no}: Running AI comparison...")
        comparison = compare_data(
            system_data, hbl_data, mbl_data,
            tracking_results, routing_data, is_direct_file
        )

        # ── Routing comparison only for primary container ──
        log_status(f"Ref {ref_no}: Running routing comparison...")
        if primary_container and primary_container in routing_data:
            primary_routing = {primary_container: routing_data[primary_container]}
        else:
            primary_routing = routing_data
        routing_result = _compare_routing_with_ai(primary_routing, tracking_results)
        comparison["Routing"] = routing_result

        if is_direct_file:
            comparison["Shipment_Type"] = "DIRECT File"

        # ── Store multi-HBL number for carrier tab ──
        if len(hbl_numbers) > 1:
            comparison["multi_hbl_numbers"] = "/".join(hbl_numbers)
            comparison["merge_type"] = merge_type

        comparison_path = os.path.join(save_dir, "Comparison_Result.json")
        try:
            with open(comparison_path, "w", encoding="utf-8") as f:
                json.dump(comparison, f, indent=2, ensure_ascii=False)
        except Exception as ce:
            log.error("Failed to write Comparison_Result.json: %s", ce)

        write_processing_log(save_dir, ref_no, date_str, "compared", "AI")
        write_processing_log(save_dir, ref_no, date_str, "completed")
        log_status(f"Ref {ref_no}: Processing complete.")

        checked.add(shipment_key)
        save_checked(checked)
        remove_no_doc_entry(no_doc, shipment_key)
        save_no_doc(no_doc)

        return "processed"

    finally:
        go_back_to_list(page)
        page.wait_for_timeout(2000)

# ═══════════════════════════════════════════════════════════════════════
#  UPDATE QUEUE (called from Documents page)
# ═══════════════════════════════════════════════════════════════════════

def queue_update(folder: str, fields: dict, action: str = "update"):
    """Add an update/complete action to the pool queue."""
    with _update_queue_lock:
        _update_queue.append({
            "folder": folder,
            "fields": fields,
            "action": action,
            "queued_at": datetime.now().isoformat(),
        })
        _update_status[folder] = {
            "status": "queued",
            "message": "Waiting for free browser...",
            "logs": [],
        }
    _ensure_queue_dispatcher()


def get_update_status(folder: str) -> dict:
    return _update_status.get(folder, {"status": "none"})

def _ensure_queue_dispatcher():
    global _queue_dispatcher_thread
    if _queue_dispatcher_thread and _queue_dispatcher_thread.is_alive():
        return
    _queue_dispatcher_thread = threading.Thread(
        target=_dispatch_update_queue, daemon=True
    )
    _queue_dispatcher_thread.start()

def _dispatch_update_queue():
    """
    Dispatcher: picks items from queue and assigns each to a pool browser thread.
    Runs until queue is empty.
    """
    while True:
        with _update_queue_lock:
            if not _update_queue:
                break
            item = _update_queue.pop(0)

        # Acquire a free browser slot (blocks until one is free)
        slot = _browser_pool.acquire()
        if slot is None:
            # Put item back and give up
            with _update_queue_lock:
                _update_queue.insert(0, item)
            log.error("No browser slot available — dropping dispatch")
            break

        # Process in the slot's own thread
        t = threading.Thread(
            target=_process_update_in_slot,
            args=(slot, item),
            daemon=True,
        )
        t.start()

def _process_update_in_slot(slot: dict, item: dict):
    """Run one update item in the given browser slot."""
    folder = item["folder"]
    fields = item["fields"]
    action = item["action"]

    def _log_update(msg):
        log.info("[UPDATE-B%d] %s", slot["id"] + 1, msg)
        if not folder.startswith("__"):
            log_oi_event(folder, "update", msg)
            
        with _update_queue_lock:
            if folder in _update_status:
                _update_status[folder]["message"] = msg
                _update_status[folder].setdefault("logs", []).append(
                    f"[{datetime.now().strftime('%H:%M:%S')}] {msg}"
                )

    try:
        _update_status[folder] = {
            "status": "running",
            "message": f"Browser {slot['id'] + 1} starting...",
            "logs": [],
        }

        # Ensure browser is alive
        if not _browser_pool.ensure_slot_ready(slot):
            _update_status[folder]["status"] = "failed"
            _update_status[folder]["message"] = "Browser failed to start"
            return

        page = slot["page"]

        # Navigate back to list + reapply filters before each update
        try:
            apply_filters(page)
            page.wait_for_timeout(2000)
            try:
                page.locator(".el-loading-mask").wait_for(state="hidden", timeout=10000)
            except Exception:
                pass
        except Exception as fe:
            _log_update(f"Filter reset failed: {fe}")

        if action == "old_doc_batch":
            _log_update("Starting batch Old Doc Support submission...")
            process_old_doc_batch(page, _log_update, fields.get("selected_folders", []))
            _update_status[folder]["status"] = "completed"
            _log_update("Batch submission complete.")
            return

        parts = folder.split("__", 1)
        if len(parts) != 2:
            _update_status[folder] = {"status": "failed", "message": "Invalid folder name"}
            return

        date_str = parts[0].replace("-", " ")
        ref_no   = parts[1]

        opened = search_and_open_shipment(page, ref_no, date_str, log_fn=_log_update)
        if not opened:
            _update_status[folder]["status"] = "failed"
            _update_status[folder]["message"] = f"Could not find {ref_no}"
            return

        # ── COMPLETE action ──
        # ── COMPLETE action ──
        if action == "complete":
            _log_update("Marking as Completed...")
            ok = _complete_task_with_retry(page, max_retries=3)
            if ok:
                _log_update("Completed.")
                _update_status[folder]["status"] = "completed"
            else:
                _log_update("Complete failed after retries.")
                _update_status[folder]["status"] = "failed"

        # ── UPDATE action (no status change) ──
        elif action == "update":
            _run_field_updates(page, folder, fields, _log_update)
            _update_status[folder]["status"] = "completed"
            _log_update("Update complete.")
            _update_comparison_json(folder, fields, _log_update)

        # ── UPDATE_AND_COMPLETE action ──
        elif action == "update_and_complete":
            if fields:
                _log_update("Running field updates before completion...")
                _run_field_updates(page, folder, fields, _log_update)
                _update_comparison_json(folder, fields, _log_update)
            _log_update("Marking as Completed...")
            ok = _complete_task_with_retry(page, max_retries=3)
            if ok:
                _log_update("Update and complete finished.")
                _update_status[folder]["status"] = "completed"
            else:
                _log_update("Complete step failed after retries.")
                _update_status[folder]["status"] = "failed"

    except Exception as e:
        log.error("Update failed for %s in browser %d: %s", folder, slot["id"] + 1, e)
        _update_status[folder]["status"] = "failed"
        _update_status[folder]["message"] = str(e)
    finally:
        # Always go back to list before releasing slot
        try:
            go_back_to_list(page)
            page.wait_for_timeout(1500)
        except Exception:
            pass

        with _update_queue_lock:
            queue_empty = len(_update_queue) == 0

        if queue_empty:
            try:
                slot["page"].context.close()
            except Exception:
                pass
            slot["page"] = None
            log.info("Queue empty. Browser %d closed.", slot["id"] + 1)
            
        _browser_pool.release(slot)
        if not queue_empty:
            log.info("Browser %d released.", slot["id"] + 1)
def _run_field_updates(page, folder: str, fields: dict, _log_update):
    """
    Extracted field-update logic (tab loop).
    Identical to old _run_update_queue tab logic, just separated.
    """
    import re
    for tab, tab_fields in fields.items():
        tab_upper = tab.upper()

        if tab_upper == "CARRIER":
            _log_update("Switching to Carrier tab...")
            try:
                page.get_by_role("tab", name="Carrier").click(timeout=5000)
                page.wait_for_timeout(2000)
            except Exception as e:
                _log_update(f"Could not open Carrier tab: {e}")
                continue

            BL_TYPE_MAP = {"ORIGINAL": "Original", "SEA WAYBILL": "Sea Waybill"}

            for field_key, new_val in tab_fields.items():
                if not new_val or _is_empty(new_val):
                    continue
                _log_update(f"  Updating {field_key} -> {new_val}")

                if field_key == "Carrier":
                    jordex_code = new_val
                    if Tracking:
                        jordex_code = Tracking.find_carrier_jordex_code(new_val)
                    _fill_search_select(page, "carrier", jordex_code)
                elif field_key == "Vessel_Name":
                    _select_vessel_dom(page, new_val)
                elif field_key == "MBL_Type":
                    mapped = BL_TYPE_MAP.get(new_val.upper().strip(), new_val)
                    _fill_dropdown_by_label(page, "MB/L Type", mapped)
                elif field_key == "MBL_Number":
                    _fill_text_by_id(page, "masterBLNumber", new_val)
                elif field_key == "HBL_Type":
                    _fill_dropdown_by_label(page, "HB/L Type", new_val)
                elif field_key == "HBL_Number":
                    _fill_text_by_id(page, "houseBLNumber", new_val)

            _click_save(page)
            _log_update("Carrier saved.")

        elif tab_upper == "CARGO":
            _log_update("Switching to Cargo tab...")
            try:
                cargo_tab = page.get_by_role("tab", name="Cargo").first
                if not cargo_tab.is_visible(timeout=3000):
                    cargo_tab = page.locator(".el-tabs__item:has-text('Cargo')").first
                cargo_tab.click(timeout=5000)
                page.wait_for_timeout(3000)
            except Exception as ce:
                _log_update(f"Could not open Cargo tab: {ce}")
                continue

            # Detect container-keyed vs flat format
            is_container_keyed = any(isinstance(v, dict) for v in tab_fields.values())

            if is_container_keyed:
                containers_to_update = {k: v for k, v in tab_fields.items() if isinstance(v, dict)}
            else:
                comp_path = os.path.join(SHIPMENTS_ROOT, folder, "Comparison_Result.json")
                containers_to_update = {}
                if os.path.exists(comp_path):
                    try:
                        with open(comp_path, "r") as cf:
                            comp_data = json.load(cf)
                        for c_entry in comp_data.get("Cargo", []):
                            cno = c_entry.get("container", "")
                            c_fields = {}
                            for f in c_entry.get("fields", []):
                                fname = f.get("field", "")
                                if fname in tab_fields and f.get("status") in ("MISMATCH", "ADDED"):
                                    c_fields[fname] = tab_fields[fname]
                            if c_fields:
                                containers_to_update[cno] = c_fields
                    except Exception:
                        pass

            for cno, fields_to_update in containers_to_update.items():
                if not fields_to_update:
                    continue

                _log_update(f"  Cargo: updating {cno} — {list(fields_to_update.keys())}")

                # Ensure on Cargo tab
                try:
                    if not page.locator(".el-tabs__item.is-active:has-text('Cargo')").first.is_visible(timeout=1000):
                        page.locator(".el-tabs__item:has-text('Cargo')").first.click()
                        page.wait_for_timeout(2000)
                except Exception:
                    pass

                # Click container row
                row_clicked = False
                if cno:
                    try:
                        cargo_rows = page.locator(
                            ".el-table__body .el-table__row, tr.selectable"
                        ).all()
                        for cr in cargo_rows:
                            if cr.is_visible() and cno in (cr.inner_text(timeout=1000) or ""):
                                cr.scroll_into_view_if_needed(timeout=2000)
                                cr.click(timeout=5000)
                                page.wait_for_timeout(2500)
                                row_clicked = True
                                break
                    except Exception as re:
                        _log_update(f"  Could not click row {cno}: {re}")

                if not row_clicked:
                    _log_update(f"  Skipping {cno} — row not found")
                    continue

                # Packages sub-tab
                try:
                    pkg_tab = page.get_by_role("tab", name="Packages").first
                    if pkg_tab.is_visible(timeout=3000):
                        pkg_tab.click()
                        page.wait_for_timeout(1500)
                except Exception:
                    pass

                # Container_Type
                if "Container_Type" in fields_to_update:
                    _fill_dropdown_by_label(page, "Container Type", fields_to_update["Container_Type"])

                # Container_No
                if "Container_No" in fields_to_update:
                    val = fields_to_update["Container_No"]
                    filled = False
                    for sel_id in ["containerNumber", "containerNo"]:
                        if _fill_text_by_id(page, sel_id, val):
                            filled = True
                            break
                    if not filled:
                        page.evaluate("""(val) => {
                            for (const label of document.querySelectorAll('.el-form-item__label')) {
                                if (label.textContent.trim().toLowerCase().includes('container n')) {
                                    const item = label.closest('.el-form-item');
                                    if (!item) continue;
                                    const inp = item.querySelector('input.el-input__inner:not([readonly])');
                                    if (inp) {
                                        const setter = Object.getOwnPropertyDescriptor(
                                            window.HTMLInputElement.prototype, 'value').set;
                                        setter.call(inp, val);
                                        inp.dispatchEvent(new Event('input', {bubbles: true}));
                                        inp.dispatchEvent(new Event('change', {bubbles: true}));
                                        return;
                                    }
                                }
                            }
                        }""", val)

                # Package fields
                pkg_fields = {k: v for k, v in fields_to_update.items()
                              if k in ("Qty", "Package_Type", "Total_Gross_Weight", "Volume")}
                if pkg_fields:
                    try:
                        page.evaluate("""() => {
                            const rows = document.querySelectorAll('table tbody tr');
                            if (rows.length > 0) rows[0].click();
                        }""")
                        page.wait_for_timeout(1500)
                        try:
                            page.wait_for_selector(".el-form-item", timeout=5000)
                        except Exception:
                            pass

                        if "Qty" in pkg_fields:
                            qty_val = str(pkg_fields["Qty"])
                            filled = False
                            for sel_id in ["qty", "quantity"]:
                                try:
                                    inp = page.locator(f"#{sel_id}").first
                                    if inp.is_visible(timeout=1500) and inp.get_attribute("readonly") is None:
                                        inp.click()
                                        inp.fill("")
                                        inp.fill(qty_val)
                                        page.wait_for_timeout(300)
                                        filled = True
                                        break
                                except Exception:
                                    continue
                            if not filled:
                                page.evaluate("""([val]) => {
                                    for (const label of document.querySelectorAll('.el-form-item__label')) {
                                        const t = label.textContent.trim().toLowerCase();
                                        if (t.includes('qty') || t.includes('quantity')) {
                                            const item = label.closest('.el-form-item');
                                            if (!item) continue;
                                            const inp = item.querySelector('input.el-input__inner:not([readonly])');
                                            if (!inp) continue;
                                            const setter = Object.getOwnPropertyDescriptor(
                                                window.HTMLInputElement.prototype, 'value').set;
                                            setter.call(inp, '');
                                            inp.dispatchEvent(new Event('input', {bubbles: true}));
                                            setter.call(inp, val);
                                            inp.dispatchEvent(new Event('input', {bubbles: true}));
                                            inp.dispatchEvent(new Event('change', {bubbles: true}));
                                        }
                                    }
                                }""", [qty_val])

                        if "Package_Type" in pkg_fields:
                            pkg_val = _normalize_pkg_type(pkg_fields["Package_Type"])
                            filled = False
                            for sel_id in ["packageType", "unit"]:
                                try:
                                    inp = page.locator(f"#{sel_id}").first
                                    if inp.is_visible(timeout=1500):
                                        if inp.get_attribute("readonly") is not None:
                                            inp.click()
                                            page.wait_for_timeout(800)
                                            _pick_dropdown_option(page, pkg_val)
                                        else:
                                            inp.click()
                                            inp.fill("")
                                            inp.fill(pkg_val)
                                        filled = True
                                        break
                                except Exception:
                                    continue
                            if not filled:
                                clicked = page.evaluate("""() => {
                                    for (const label of document.querySelectorAll('.el-form-item__label')) {
                                        const t = label.textContent.trim().toLowerCase();
                                        if (t === 'package' || t === 'unit' || t === 'package type') {
                                            const item = label.closest('.el-form-item');
                                            if (!item) continue;
                                            const inp = item.querySelector('input.el-input__inner[readonly]');
                                            if (inp) { inp.click(); return true; }
                                        }
                                    }
                                    return false;
                                }""")
                                if clicked:
                                    page.wait_for_timeout(800)
                                    _pick_dropdown_option(page, pkg_val)

                        if "Total_Gross_Weight" in pkg_fields:
                            wt_val = str(pkg_fields["Total_Gross_Weight"])
                            filled = False
                            for sel_id in ["weight", "grossWeight", "totalGrossWeight"]:
                                try:
                                    inp = page.locator(f"#{sel_id}").first
                                    if inp.is_visible(timeout=1500) and inp.get_attribute("readonly") is None:
                                        inp.click()
                                        inp.fill("")
                                        inp.fill(wt_val)
                                        page.wait_for_timeout(300)
                                        filled = True
                                        break
                                except Exception:
                                    continue
                            if not filled:
                                page.evaluate("""([val]) => {
                                    for (const label of document.querySelectorAll('.el-form-item__label')) {
                                        const t = label.textContent.trim().toLowerCase();
                                        if (t.includes('weight') || t.includes('gross')) {
                                            const item = label.closest('.el-form-item');
                                            if (!item) continue;
                                            const inp = item.querySelector('input.el-input__inner:not([readonly])');
                                            if (!inp) continue;
                                            const setter = Object.getOwnPropertyDescriptor(
                                                window.HTMLInputElement.prototype, 'value').set;
                                            setter.call(inp, '');
                                            inp.dispatchEvent(new Event('input', {bubbles: true}));
                                            setter.call(inp, val);
                                            inp.dispatchEvent(new Event('input', {bubbles: true}));
                                            inp.dispatchEvent(new Event('change', {bubbles: true}));
                                        }
                                    }
                                }""", [wt_val])

                        if "Volume" in pkg_fields:
                            vol_val = str(pkg_fields["Volume"])
                            filled = False
                            for sel_id in ["volume", "totalVolume"]:
                                try:
                                    inp = page.locator(f"#{sel_id}").first
                                    if inp.is_visible(timeout=1500) and inp.get_attribute("readonly") is None:
                                        inp.click()
                                        inp.fill("")
                                        inp.fill(vol_val)
                                        page.wait_for_timeout(300)
                                        filled = True
                                        break
                                except Exception:
                                    continue
                            if not filled:
                                page.evaluate("""([val]) => {
                                    for (const label of document.querySelectorAll('.el-form-item__label')) {
                                        const t = label.textContent.trim().toLowerCase();
                                        if (t.includes('volume') || t.includes('cbm')) {
                                            const item = label.closest('.el-form-item');
                                            if (!item) continue;
                                            const inp = item.querySelector('input.el-input__inner:not([readonly])');
                                            if (!inp) continue;
                                            const setter = Object.getOwnPropertyDescriptor(
                                                window.HTMLInputElement.prototype, 'value').set;
                                            setter.call(inp, '');
                                            inp.dispatchEvent(new Event('input', {bubbles: true}));
                                            setter.call(inp, val);
                                            inp.dispatchEvent(new Event('input', {bubbles: true}));
                                            inp.dispatchEvent(new Event('change', {bubbles: true}));
                                        }
                                    }
                                }""", [vol_val])

                        # Save package popup — try footer buttons first, then generic save
                        try:
                            popup_save = page.locator(
                                ".mf-form__footer-buttons .el-button--primary"
                            ).last
                            if popup_save.is_visible(timeout=3000):
                                popup_save.click()
                                page.wait_for_timeout(2000)
                            else:
                                # Fallback: click any visible primary save button in dialog/panel
                                dialog_save = page.locator(
                                    ".el-dialog .el-button--primary, "
                                    ".el-drawer .el-button--primary, "
                                    "button.el-button--primary:visible"
                                ).last
                                if dialog_save.is_visible(timeout=2000):
                                    dialog_save.click()
                                    page.wait_for_timeout(2000)
                            try:
                                ok = page.locator("button:has-text('OK'):visible").first
                                if ok.is_visible(timeout=2000):
                                    ok.click()
                                    page.wait_for_timeout(1000)
                            except Exception:
                                pass
                        except Exception:
                            pass
                        # Also click main cargo detail Save after returning from package popup
                        page.wait_for_timeout(500)
                        _click_save(page)
                        page.wait_for_timeout(1000)

                    except Exception as pe:
                        _log_update(f"  Package update failed for {cno}: {pe}")

                # Goods Description
                if "Goods_Description" in fields_to_update:
                    try:
                        goods_tab = page.locator("#tab-goods, [id*='tab-goods']").first
                        if not goods_tab.is_visible(timeout=2000):
                            goods_tab = page.get_by_role("tab", name="Goods").first
                        if goods_tab.is_visible(timeout=2000):
                            goods_tab.click()
                            page.wait_for_timeout(1000)
                            ta = page.locator("textarea:visible").first
                            if ta.is_visible(timeout=2000) and ta.get_attribute("readonly") is None:
                                ta.click()
                                ta.fill("")
                                ta.fill(fields_to_update["Goods_Description"])
                    except Exception as ge:
                        _log_update(f"  Goods fill failed: {ge}")

                _click_save(page)
                _log_update(f"  Container {cno} saved.")

                # Back to cargo list
                try:
                    page.locator(".el-tabs__item:has-text('Cargo')").first.click()
                    page.wait_for_timeout(2000)
                except Exception:
                    pass

            _log_update("Cargo updates complete.")

        elif tab_upper == "PARTIES":
            _log_update("Parties update not yet automated.")

       # =====================================================================
# REPLACE the entire  elif tab_upper == "ROUTING":  block
# in _run_field_updates()
# =====================================================================

        elif tab_upper == "ROUTING":
            _log_update("Processing Routing updates...")
            
            # Click "View routing"
            try:
                btn = page.locator(".routing-sidebar__routing-label").first
                btn.wait_for(state="visible", timeout=10000)
                try:
                    btn.click(timeout=5000)
                except Exception:
                    page.evaluate("document.querySelector('.routing-sidebar__routing-label')?.click()")
                page.wait_for_load_state("load", timeout=30000)
                page.wait_for_timeout(2000)
            except Exception as e:
                _log_update(f"Could not open routing sidebar: {e}")
                continue

            # Select correct container in sidebar if specified
            target_container = tab_fields.pop("__container__", None)
            if target_container:
                try:
                    sidebar_blocks = page.locator(".cargo-tab__content .cargo-tab__block").all()
                    for idx, block in enumerate(sidebar_blocks):
                        if block.is_visible() and target_container in (block.inner_text(timeout=1000) or ""):
                            _log_update(f"  Selecting container {target_container} in sidebar...")
                            block.scroll_into_view_if_needed(timeout=2000)
                            block.click(timeout=3000)
                            page.wait_for_timeout(2000)
                            break
                except Exception as ce:
                    _log_update(f"  Could not select container: {ce}")

            # Ensure Lane tab
            try:
                lane_tab = page.locator("#tab-lane-1, .el-tabs__item:has-text('Lane')").first
                if lane_tab.is_visible(timeout=5000):
                    lane_tab.click()
                    page.wait_for_timeout(1000)
            except Exception:
                pass

            # Process each field
            for field_key, new_val in tab_fields.items():
                if not new_val or _is_empty(new_val):
                    continue
                _log_update(f"  Updating Routing {field_key} -> {new_val}")
                
                # Lane tab simple fields
                if field_key in ["Departure", "Arrival", "Port_of_Loading", "Port_of_Discharge", "Voyage"]:
                    try:
                        lane_tab = page.locator("#tab-lane-1, .el-tabs__item:has-text('Lane')").first
                        if lane_tab.is_visible(timeout=5000):
                            lane_tab.click()
                            page.wait_for_timeout(1000)
                    except Exception:
                        pass
                    
                    if field_key == "Departure":
                        _fill_text_by_id(page, "departure-end", new_val)
                    elif field_key == "Arrival":
                        _fill_text_by_id(page, "arrival-end", new_val)
                    elif field_key == "Voyage":
                        _fill_text_by_id(page, "voyage", new_val)
                    elif field_key == "Port_of_Loading":
                        inp = page.locator("#portOfLoading").first
                        if not inp.is_visible(timeout=1000):
                            inp = page.locator("div").filter(has_text=re.compile(r"Port of loading|Port Of Loading", re.IGNORECASE)).locator("input[placeholder*='search port']").last
                        if inp.is_visible(timeout=1000):
                            _fill_search_select_loc(page, inp, new_val)
                    elif field_key == "Port_of_Discharge":
                        inp = page.locator("#portOfDischarge").first
                        if not inp.is_visible(timeout=1000):
                            inp = page.locator("div").filter(has_text=re.compile(r"Port of discharge|Port Of Discharge", re.IGNORECASE)).locator("input[placeholder*='search port']").last
                        if inp.is_visible(timeout=1000):
                            _fill_search_select_loc(page, inp, new_val)
                            
                # ──────────────────────────────────────────────────────────
                #  TRANSIT DATE UPDATE
                #
                #  Recording flow:
                #    1. Click the date field (TBD or existing date)
                #    2. Calendar opens -> type date in "Select date" placeholder
                #    3. Click OK button to confirm
                #    4. (Save clicked once at the end for all fields)
                #
                #  Layout per transit row:
                #    [Arrival date] [Harbour/port] [Departure date] [Voyage]
                # ──────────────────────────────────────────────────────────
                elif field_key.startswith("Transshipment_ETA") or field_key.startswith("Transshipment_ETD"):
                    try:
                        lane_tab = page.locator("#tab-lane-1, .el-tabs__item:has-text('Lane')").first
                        if lane_tab.is_visible(timeout=5000):
                            lane_tab.click()
                            page.wait_for_timeout(1000)
                    except Exception:
                        pass

                    m = re.search(r'\((.*?)\)', field_key)
                    target_port = m.group(1).upper().strip() if m else ""
                    is_eta = "ETA" in field_key
                    filled_ok = False

                    try:
                        # Find the target date field by locating the port input first,
                        # then clicking the date field next to it.
                        # This works whether the date is TBD or already filled.
                        
                        port_inputs = page.locator("input[placeholder*='search port']:visible").all()
                        _log_update(f"  Found {len(port_inputs)} visible port inputs.")
                        
                        target_date_field = None
                        
                        for pi in port_inputs:
                            try:
                                val = (pi.input_value(timeout=500) or "").upper().strip()
                                if target_port and target_port in val:
                                    _log_update(f"  Found port '{val}' matching '{target_port}'.")
                                    
                                    # Get the parent transit row container
                                    # Transit row: [Arrival] [Harbour] [Departure] [Voyage]
                                    # The port input is the Harbour field.
                                    # We need Arrival (before) or Departure (after).
                                    #
                                    # Use Playwright locator relative to the parent row.
                                    # The port input's parent chain has .el-form-item elements.
                                    # Go up to the transit row container, then find date inputs.
                                    
                                    # Strategy: get all inputs in the same row by using
                                    # the parent container that has all 4 fields.
                                    # From DOM: div:nth-child(N) > div:nth-child(2) > div:nth-child(3) > .el-form-item
                                    # Simpler: find all visible date inputs (with calendar icon)
                                    # that are siblings of this port input.
                                    
                                    # Easiest approach: collect ALL visible inputs on page,
                                    # find this port input's position, offset to get date.
                                    all_inputs = page.locator("input.el-input__inner:visible").all()
                                    
                                    for gi, inp in enumerate(all_inputs):
                                        try:
                                            v = (inp.input_value(timeout=300) or "").strip()
                                            ph = (inp.get_attribute("placeholder") or "").lower()
                                            if val.lower().strip() in v.lower().strip() and "search port" in ph:
                                                # Found port at index gi
                                                # ETA = gi-1 (Arrival), ETD = gi+1 (Departure)
                                                date_idx = (gi - 1) if is_eta else (gi + 1)
                                                if 0 <= date_idx < len(all_inputs):
                                                    target_date_field = all_inputs[date_idx]
                                                    _log_update(f"  Date field at input index {date_idx} (port at {gi}).")
                                                break
                                        except Exception:
                                            continue
                                    break
                            except Exception:
                                continue
                        
                        if target_date_field:
                            # Click the date field — opens calendar
                            _log_update(f"  Clicking date field...")
                            target_date_field.click(timeout=5000)
                            page.wait_for_timeout(500)
                            
                            # Type the date in the "Select date" input inside the calendar
                            select_date = page.get_by_placeholder("Select date")
                            if select_date.is_visible(timeout=3000):
                                select_date.click()
                                select_date.fill("")
                                select_date.fill(new_val)
                                page.wait_for_timeout(300)
                                
                                # Click OK button to confirm (NOT Enter)
                                ok_btn = page.get_by_role("button", name="OK")
                                if ok_btn.is_visible(timeout=3000):
                                    ok_btn.click()
                                    page.wait_for_timeout(500)
                                    filled_ok = True
                                    _log_update(f"  Updated {field_key} -> {new_val}")
                                else:
                                    # Fallback: press Enter
                                    page.keyboard.press("Enter")
                                    page.wait_for_timeout(500)
                                    filled_ok = True
                                    _log_update(f"  Updated {field_key} -> {new_val} (Enter fallback)")
                            else:
                                _log_update(f"  Calendar 'Select date' input not visible.")
                        else:
                            _log_update(f"  Could not find date field for port '{target_port}'.")

                    except Exception as e:
                        _log_update(f"  Failed to update {field_key}: {e}")

                    if not filled_ok:
                        _log_update(f"  WARNING: {field_key} was NOT updated.")

                # Destination tab fields
                elif field_key == "Terminal":
                    try:
                        dest_tab = page.locator("#tab-destination-2")
                        if not dest_tab.is_visible(timeout=3000):
                            dest_tab = page.get_by_role("tab", name="Destination")
                        dest_tab.click()
                        page.wait_for_timeout(1000)
                    except Exception:
                        pass
                    try:
                        dest_pane = page.locator("#pane-destination, .el-tab-pane").filter(has_text=re.compile(r"Destination", re.IGNORECASE)).first
                        addr_select = dest_pane.locator(".address-select__body--select").first
                        if addr_select.is_visible(timeout=3000):
                            try:
                                page.evaluate('''() => {
                                    const destPane = document.querySelector("#pane-destination") || Array.from(document.querySelectorAll(".el-tab-pane")).find(e => e.textContent.includes("Destination"));
                                    if (destPane) {
                                        const sel = destPane.querySelector(".address-select__body--select");
                                        if (sel) {
                                            const btns = Array.from(sel.querySelectorAll("button"));
                                            const toolbarBtns = Array.from(sel.querySelectorAll(".address-select__toolbar button"));
                                            for (let b of btns) { if (!toolbarBtns.includes(b)) b.click(); }
                                        }
                                    }
                                }''')
                                page.wait_for_timeout(1000)
                            except Exception:
                                pass
                            address_btn = addr_select.locator(".address-select__toolbar button").first
                            if address_btn.is_visible(timeout=3000):
                                address_btn.click()
                                page.wait_for_timeout(1500)
                                search_box = page.get_by_role("textbox", name="Search").first
                                if search_box.is_visible(timeout=3000):
                                    search_box.click()
                                    search_box.fill("")
                                    search_box.fill(new_val)
                                    page.wait_for_timeout(1500)
                                    try:
                                        page.locator(".el-dialog__body tbody tr").first.wait_for(state="visible", timeout=3000)
                                    except Exception:
                                        pass
                                    rows = page.locator(".el-dialog__body tbody tr").all()
                                    if rows:
                                        clicked = False
                                        target_norm = new_val.strip().lower().replace(" ", "")
                                        for r in rows:
                                            if r.is_visible():
                                                try:
                                                    text = r.locator("td").first.inner_text(timeout=500).strip().lower().replace(" ", "")
                                                except Exception:
                                                    text = r.inner_text(timeout=500).strip().lower().replace(" ", "")
                                                if text and (target_norm in text or text in target_norm):
                                                    r.click(); page.wait_for_timeout(1000); clicked = True; break
                                        if not clicked and rows:
                                            for r in rows:
                                                if r.is_visible(): r.click(); page.wait_for_timeout(1000); break
                                    try:
                                        save_btn = page.locator(".el-dialog button:has-text('Save')").first
                                        if save_btn.is_visible(timeout=2000): save_btn.click(); page.wait_for_timeout(1000)
                                    except Exception:
                                        pass
                    except Exception as e:
                        _log_update(f"  Failed to update Terminal: {e}")
                    
            # ── Transit & On-Carriage: adding NEW rows ──
            try:
                transits_to_add = []
                on_carriage_to_add = []
                for field_key, raw_val in tab_fields.items():
                    if not raw_val: continue
                    if field_key.startswith("Transshipment_Port"):
                        try: transits_to_add.append(json.loads(raw_val))
                        except Exception: pass
                    elif field_key.startswith("Carrier_On_Carriage"):
                        try: on_carriage_to_add.append(json.loads(raw_val))
                        except Exception: pass

                if transits_to_add or on_carriage_to_add:
                    try:
                        lane_tab = page.locator("#tab-lane-1, .el-tabs__item:has-text('Lane')").first
                        if lane_tab.is_visible(timeout=5000):
                            lane_tab.click()
                            page.wait_for_timeout(1000)
                    except Exception:
                        pass
                
                for t in transits_to_add:
                    _log_update(f"  Adding Transit port: {t.get('port')}")
                    page.get_by_role("button", name="+ Transit port").click()
                    page.wait_for_timeout(1000)
                    
                    # ETA (Arrival)
                    eta_val = t.get("eta") or t.get("arrival_date") or ""
                    if eta_val:
                        # Use the recording selector for new transit row
                        arrival_field = page.locator("div").filter(has_text=re.compile(r"^ArrivalHarbourDepartureVoyage$")).get_by_placeholder("TBD").first
                        if arrival_field.is_visible(timeout=3000):
                            arrival_field.click()
                            page.wait_for_timeout(500)
                            di = page.get_by_placeholder("Select date")
                            if di.is_visible(timeout=3000):
                                di.click(); di.fill(eta_val); page.wait_for_timeout(300)
                                ok = page.get_by_role("button", name="OK")
                                if ok.is_visible(timeout=2000): ok.click()
                                else: page.keyboard.press("Enter")
                                page.wait_for_timeout(500)
                            
                    # Harbour (Port)
                    port_val = t.get("port") or t.get("location") or ""
                    if port_val:
                        search_boxes = page.locator("div").filter(has_text=re.compile(r"^ArrivalHarbourDepartureVoyage$")).get_by_placeholder("Type to search port").all()
                        if search_boxes:
                            _fill_search_select_loc(page, search_boxes[-1], port_val)
                            
                    # ETD (Departure)
                    etd_val = t.get("etd") or t.get("departure_date") or ""
                    if etd_val:
                        dep_field = page.locator("div").filter(has_text=re.compile(r"^ArrivalHarbourDepartureVoyage$")).get_by_placeholder("TBD").nth(1)
                        if dep_field.is_visible(timeout=3000):
                            dep_field.click()
                            page.wait_for_timeout(500)
                            di = page.get_by_placeholder("Select date")
                            if di.is_visible(timeout=3000):
                                di.click(); di.fill(etd_val); page.wait_for_timeout(300)
                                ok = page.get_by_role("button", name="OK")
                                if ok.is_visible(timeout=2000): ok.click()
                                else: page.keyboard.press("Enter")
                                page.wait_for_timeout(500)
                            
                    # Voyage
                    voy_val = t.get("voyage_out") or t.get("voyage") or ""
                    if voy_val:
                        text_boxes = page.locator("div").filter(has_text=re.compile(r"^ArrivalHarbourDepartureVoyage$")).get_by_role("textbox").all()
                        if text_boxes:
                            text_boxes[-1].click(); page.wait_for_timeout(500)
                            text_boxes[-1].fill(voy_val); page.wait_for_timeout(300)
                            
                    # Origin Voyage (voyage_in)
                    voy_in = t.get("voyage_in")
                    if voy_in:
                        text_boxes = page.locator("div").filter(has_text=re.compile(r"^ArrivalHarbourDepartureVoyage$|^DepartureVoyage$")).get_by_role("textbox").all()
                        if text_boxes:
                            text_boxes[0].click(); page.wait_for_timeout(500)
                            text_boxes[0].fill(voy_in); page.wait_for_timeout(300)

                for on_c in on_carriage_to_add:
                    _log_update(f"  Adding Carrier On-Carriage: {on_c.get('arrival_place')}")
                    page.get_by_role("button", name="+ Add carrier on-carriage").click()
                    page.wait_for_timeout(1000)
                    arr_date = on_c.get("arrival_date")
                    if arr_date:
                        tbd_boxes = page.get_by_role("textbox", name="TBD").all()
                        if tbd_boxes:
                            tbd_boxes[-1].click(); page.wait_for_timeout(500)
                            di = page.get_by_placeholder("Select date")
                            if di.is_visible(timeout=3000):
                                di.click(); di.fill(arr_date); page.wait_for_timeout(300)
                                ok = page.get_by_role("button", name="OK")
                                if ok.is_visible(timeout=2000): ok.click()
                                else: page.keyboard.press("Enter")
                                page.wait_for_timeout(500)
                    arr_place = on_c.get("arrival_place")
                    if arr_place:
                        search_boxes = page.get_by_role("textbox", name="Type to search port").all()
                        if search_boxes:
                            _fill_search_select_loc(page, search_boxes[-1], arr_place)

            except Exception as e:
                _log_update(f"  Transit addition failed: {e}")

            # ══════════════════════════════════════════
            #  MANDATORY SAVE — click the orange Save button
            # ══════════════════════════════════════════
            _log_update("  Clicking Save button...")
            try:
                save_btn = page.get_by_role("button", name="Save")
                if save_btn.is_visible(timeout=5000):
                    save_btn.click()
                    page.wait_for_timeout(2500)
                    # Handle OK confirmation dialog if it appears
                    try:
                        ok = page.locator("button:has-text('OK'):visible").first
                        if ok.is_visible(timeout=2000):
                            ok.click()
                            page.wait_for_timeout(1000)
                    except Exception:
                        pass
                    _log_update("  Routing saved.")
                else:
                    _log_update("  Save button not visible — trying fallback...")
                    _click_save(page)
                    _log_update("  Routing saved (fallback).")
            except Exception as se:
                _log_update(f"  Save error: {se}")
                _click_save(page)

            _log_update("Routing updates complete.")


def process_old_doc_batch(page: Page, _log_update, selected_folders: list):
    """Processes a batch of pending Old Doc Support items."""
    _log_update("Collecting pending Old Doc Support items...")
    
    pending_items = {}
    min_date_dt = None
    max_date_dt = None
    
    for folder in os.listdir(SHIPMENTS_ROOT):
        if selected_folders and folder not in selected_folders:
            continue
            
        res_path = os.path.join(SHIPMENTS_ROOT, folder, "result.json")
        if os.path.exists(res_path):
            try:
                with open(res_path, "r", encoding="utf-8") as f:
                    res_data = json.load(f)
                if res_data.get("status") == "pending_old_doc_support":
                    ref = res_data.get("ref_no")
                    date_str = res_data.get("date_str")
                    if ref and date_str:
                        pending_items[ref] = {
                            "folder": folder,
                            "comment": res_data.get("comment", ""),
                            "date_str": date_str
                        }
                        
                        try:
                            clean = date_str.replace("-", " ")
                            dt = datetime.strptime(clean, "%d %b %Y").date()
                            if min_date_dt is None or dt < min_date_dt:
                                min_date_dt = dt
                            if max_date_dt is None or dt > max_date_dt:
                                max_date_dt = dt
                        except Exception:
                            pass
            except Exception as e:
                _log_update(f"Error reading result.json in {folder}: {e}")
                
    if not pending_items:
        _log_update("No pending Old Doc Support items found to process.")
        return
        
    _log_update(f"Processing {len(pending_items)} items via search...")
    
    try:
        from Login import apply_filters
        _log_update("Resetting filters...")
        apply_filters(page, status_callback=_log_update)
        page.wait_for_timeout(2000)
        try:
            page.locator(".el-loading-mask").wait_for(state="hidden", timeout=10000)
        except Exception:
            pass

        for ref_no, item in list(pending_items.items()):
            _log_update(f"Searching for {ref_no}...")
            
            opened = search_and_open_shipment(page, ref_no, item["date_str"], log_fn=_log_update)
            if not opened:
                _log_update(f"Could not find {ref_no} via search.")
                continue
                
            try:
                _run_old_doc_submit_logic(page, item["folder"], item["comment"], _log_update)
                # Clean up only if successful
                del pending_items[ref_no]
            except Exception as e:
                _log_update(f"Error processing {ref_no}: {e}")
            finally:
                # Always return to dashboard even if it fails
                go_back_to_list(page)
                page.wait_for_timeout(2000)
                
    except Exception as e:
        _log_update(f"Error during batch processing: {e}")
        import traceback
        traceback.print_exc()

def _run_old_doc_submit_logic(page: Page, folder: str, comment_text: str, log_fn):
    log_fn("Opening 'Check shipment' drawer...")
    max_retries = 3
    for attempt in range(max_retries):
        try:
            # Wait for any visible loading masks to disappear
            try:
                page.locator(".el-loading-mask:visible").wait_for(state="hidden", timeout=5000)
            except Exception:
                pass

            # Wait for task list to be loaded on the page
            page.wait_for_selector(".mf-tasks-title", timeout=15000)
            
            # Locate the specific "Check shipment" task container (exact match)
            task_title = page.locator(".mf-tasks-title:text-is('Check shipment'), p:text-is('Check shipment')").first
            task_card = page.locator(".content-col").filter(has=task_title).first
            
            if task_card.is_visible(timeout=3000):
                task_card.click()
            else:
                task_title.click()
        except Exception as e:
            log.warning("Primary task card click failed, trying fallback: %s", e)
            try:
                page.locator("p:has-text('Check shipment')").first.click(timeout=5000)
            except Exception as e2:
                log.error("Fallback Check shipment click failed: %s", e2)

        # Wait for the textarea to become visible (confirms drawer is open)
        try:
            page.wait_for_selector("textarea", state="visible", timeout=5000)
            break  # Success! Break out of the retry loop
        except Exception:
            log.warning(f"Drawer did not open (Attempt {attempt + 1}/{max_retries}), forcing click...")
            try:
                page.locator(".mf-tasks-title:text-is('Check shipment'), p:text-is('Check shipment')").first.evaluate("el => el.click()")
                page.wait_for_selector("textarea", state="visible", timeout=5000)
                break  # Success! Break out of the retry loop
            except Exception as fallback_e:
                if attempt < max_retries - 1:
                    log.warning("Forced click failed. Waiting 3 seconds before next retry...")
                    page.wait_for_timeout(3000)
                else:
                    log.error("All retries exhausted for opening drawer: %s", fallback_e)
                    raise fallback_e
            
    page.wait_for_timeout(2000)

    log_fn("Clicking Assignee select dropdown...")
    try:
        # Wait for any overlay to clear
        try:
            page.locator(".el-loading-mask:visible").wait_for(state="hidden", timeout=3000)
        except Exception:
            pass
            
        # Target the select textbox inside the Assignee form item
        assignee_input = page.locator("div.el-form-item:has-text('Assignee')").locator("input.el-input__inner:visible").first
        if assignee_input.is_visible(timeout=3000):
            assignee_input.click()
        else:
            page.locator("input.el-input__inner:visible").first.click(timeout=5000)
    except Exception as e:
        log.error("Select input click failed, trying fallback: %s", e)
        page.locator("input.el-input__inner:visible").first.click(timeout=3000)
        
    page.wait_for_timeout(2000)

    log_fn("Selecting 'Import support' parent menu...")
    try:
        page.get_by_text("Import support").first.click(timeout=5000)
    except Exception as e:
        log.error("Could not click 'Import support': %s", e)
        raise e
    page.wait_for_timeout(2000)

    log_fn("Selecting 'Unassigned' option...")
    try:
        page.get_by_text("Unassigned").first.click(timeout=5000)
    except Exception as e:
        log.error("Could not click 'Unassigned': %s", e)
        raise e
    page.wait_for_timeout(1500)

    log_fn(f"Entering comment: '{comment_text}'...")
    try:
        textarea = page.locator("textarea:visible").first
        textarea.click(timeout=3000)
        textarea.fill(comment_text)
    except Exception as e:
        log.error("Textarea fill failed, trying fallback: %s", e)
        try:
            textarea_el = page.locator("textarea.el-textarea__inner:visible").first
            textarea_el.focus()
            textarea_el.fill(comment_text)
        except Exception as e2:
            log.error("Fallback textarea fill failed: %s", e2)
            raise e2
    page.wait_for_timeout(1500)

    log_fn("Saving assignment and comment...")
    saved = _click_save(page)
    if not saved:
        try:
            save_btn = page.locator("button:has-text('Save'):visible").first
            if save_btn.is_visible(timeout=2000):
                save_btn.click()
                saved = True
        except Exception:
            pass
        if not saved:
            raise Exception("Failed to click save button / confirm save dialog.")
        
    res_path = os.path.join(SHIPMENTS_ROOT, folder, "result.json")
    if os.path.exists(res_path):
        try:
            with open(res_path, "r", encoding="utf-8") as f:
                res_data = json.load(f)
            res_data["status"] = "completed_old_doc_support"
            res_data["submitted_at"] = datetime.now().isoformat()
            with open(res_path, "w", encoding="utf-8") as f:
                json.dump(res_data, f, indent=2, ensure_ascii=False)
        except Exception as json_err:
            log.error("Failed to update result.json status: %s", json_err)



def _update_comparison_json(folder: str, fields: dict, _log_update):
    """Mark updated fields as UPDATED in Comparison_Result.json."""
    try:
        comp_path = os.path.join(SHIPMENTS_ROOT, folder, "Comparison_Result.json")
        if not os.path.exists(comp_path):
            return

        with open(comp_path, "r") as cf:
            comp = json.load(cf)

        updated_fields = set()
        for tab, tab_fields in fields.items():
            for field_key in tab_fields:
                updated_fields.add(field_key)

        changed = False
        for section in ["Parties", "Carrier"]:
            for item in comp.get(section, []):
                if item.get("field") in updated_fields and item.get("status") in ("MISMATCH", "ADDED"):
                    item["status"] = "UPDATED"
                    changed = True

        for section in ["Cargo", "Routing"]:
            for container in comp.get(section, []):
                for field in container.get("fields", []):
                    if field.get("field") in updated_fields and field.get("status") in ("MISMATCH", "ADDED"):
                        field["status"] = "UPDATED"
                        changed = True

        if changed:
            with open(comp_path, "w") as cf:
                json.dump(comp, cf, indent=2, ensure_ascii=False)
            _log_update("Comparison result updated (MISMATCH → UPDATED).")
    except Exception as ue:
        log.warning("Could not update comparison JSON: %s", ue)

def _ensure_update_thread():
    global _update_thread
    if _update_thread and _update_thread.is_alive():
        return
    _update_thread = threading.Thread(target=_run_update_queue, daemon=True)
    _update_thread.start()


def _run_update_queue():
    """Process update queue sequentially using a single browser."""
    page = None
    try:
        page = launch_and_login(headless=False)
        apply_filters(page)
        page.wait_for_timeout(3000)
        try:
            page.locator(".el-loading-mask").wait_for(
                state="hidden", timeout=10000
            )
        except Exception:
            pass
    except Exception as e:
        log.error("Update queue: login failed: %s", e)
        return

    while True:
        with _update_queue_lock:
            if not _update_queue:
                break
            item = _update_queue.pop(0)

        folder = item["folder"]
        fields = item["fields"]
        action = item["action"]

        def _log_update(msg):
            log.info("[UPDATE] %s", msg)
            if not folder.startswith("__"):
                log_oi_event(folder, "update", msg)
            with _update_queue_lock:
                if folder in _update_status:
                    _update_status[folder]["message"] = msg
                    _update_status[folder].setdefault("logs", []).append(
                        f"[{datetime.now().strftime('%H:%M:%S')}] {msg}"
                    )

        parts = folder.split("__", 1)
        if len(parts) != 2:
            _update_status[folder] = {"status": "failed", "message": "Invalid folder"}
            continue

        date_str = parts[0].replace("-", " ")
        ref_no = parts[1]

        _update_status[folder] = {
            "status": "running", "message": "Opening shipment...", "logs": [],
        }

        try:
            opened = search_and_open_shipment(
                page, ref_no, date_str, log_fn=_log_update
            )
            if not opened:
                _update_status[folder]["status"] = "failed"
                _update_status[folder]["message"] = f"Could not find {ref_no}"
                continue

            if action == "complete":
                _log_update("Marking as Completed...")
                complete_task_logic(page, "prepare")
                complete_task_logic(page, "commit")
                _log_update("Completed.")
                _update_status[folder]["status"] = "completed"

            elif action == "update":
                # Fields dict: {tab: {field: value}}
                for tab, tab_fields in fields.items():
                    tab_upper = tab.upper()
                    if tab_upper == "CARRIER":
                        _log_update("Switching to Carrier tab...")
                        page.get_by_role("tab", name="Carrier").click(timeout=5000)
                        page.wait_for_timeout(2000)

                        BL_TYPE_MAP = {
                            "ORIGINAL": "Original",
                            "SEA WAYBILL": "Sea Waybill",
                        }

                        for field_key, new_val in tab_fields.items():
                            if not new_val or _is_empty(new_val):
                                continue
                            _log_update(f"  Updating {field_key} -> {new_val}")

                            if field_key == "Carrier":
                                jordex_code = new_val
                                if Tracking:
                                    jordex_code = Tracking.find_carrier_jordex_code(new_val)
                                _fill_search_select(page, "carrier", jordex_code)
                            elif field_key == "Vessel_Name":
                                _select_vessel_dom(page, new_val)
                            elif field_key == "MBL_Type":
                                mapped = BL_TYPE_MAP.get(
                                    new_val.upper().strip(), new_val
                                )
                                _fill_dropdown_by_label(page, "MB/L Type", mapped)
                            elif field_key == "MBL_Number":
                                _fill_text_by_id(page, "masterBLNumber", new_val)
                            elif field_key == "HBL_Type":
                                _fill_dropdown_by_label(page, "HB/L Type", new_val)
                            elif field_key == "HBL_Number":
                                _fill_text_by_id(page, "houseBLNumber", new_val)

                        _click_save(page)
                        _log_update("Carrier saved.")

                    elif tab_upper == "CARGO":
                        _log_update("Switching to Cargo tab...")
                        try:
                            cargo_tab = page.get_by_role("tab", name="Cargo").first
                            if not cargo_tab.is_visible(timeout=3000):
                                cargo_tab = page.locator(
                                    ".el-tabs__item:has-text('Cargo')"
                                ).first
                            cargo_tab.click(timeout=5000)
                            page.wait_for_timeout(3000)
                        except Exception as ce:
                            _log_update(f"Could not open Cargo tab: {ce}")
                            continue

                        # tab_fields format from new UI:
                        # Container-specific: {"HAMU1436755": {"Package_Type": "Package"}, ...}
                        # Legacy flat: {"Package_Type": "Package"} (falls back to all containers)
                        
                        # Detect format: if first value is a dict, it's container-keyed
                        is_container_keyed = any(isinstance(v, dict) for v in tab_fields.values())
                        
                        if is_container_keyed:
                            # New format: only update specified containers
                            containers_to_update = {k: v for k, v in tab_fields.items() if isinstance(v, dict)}
                        else:
                            # Legacy flat format: read Comparison_Result to find all mismatched containers
                            comp_path = os.path.join(SHIPMENTS_ROOT, folder, "Comparison_Result.json")
                            containers_to_update = {}
                            if os.path.exists(comp_path):
                                try:
                                    with open(comp_path, "r") as cf:
                                        comp_data = json.load(cf)
                                    for c_entry in comp_data.get("Cargo", []):
                                        cno = c_entry.get("container", "")
                                        c_fields = {}
                                        for f in c_entry.get("fields", []):
                                            fname = f.get("field", "")
                                            if fname in tab_fields and f.get("status") in ("MISMATCH", "ADDED"):
                                                c_fields[fname] = tab_fields[fname]
                                        if c_fields:
                                            containers_to_update[cno] = c_fields
                                except Exception:
                                    pass

                        for cno, fields_to_update in containers_to_update.items():
                            if not fields_to_update:
                                continue

                            _log_update(f"  Cargo: updating {cno} — {list(fields_to_update.keys())}")

                            # Ensure we're on Cargo tab
                            try:
                                if not page.locator(".el-tabs__item.is-active:has-text('Cargo')").first.is_visible(timeout=1000):
                                    page.locator(".el-tabs__item:has-text('Cargo')").first.click()
                                    page.wait_for_timeout(2000)
                            except Exception:
                                pass

                            # Find and click the container row
                            row_clicked = False
                            if cno:
                                try:
                                    cargo_rows = page.locator(
                                        ".el-table__body .el-table__row, tr.selectable"
                                    ).all()
                                    for cr in cargo_rows:
                                        if cr.is_visible() and cno in (cr.inner_text(timeout=1000) or ""):
                                            cr.scroll_into_view_if_needed(timeout=2000)
                                            cr.click(timeout=5000)
                                            page.wait_for_timeout(2500)
                                            row_clicked = True
                                            break
                                except Exception as re:
                                    _log_update(f"  Could not click container row {cno}: {re}")

                            if not row_clicked:
                                _log_update(f"  Skipping {cno} — row not found")
                                continue

                            # Try Packages sub-tab
                            try:
                                pkg_tab = page.get_by_role("tab", name="Packages").first
                                if pkg_tab.is_visible(timeout=3000):
                                    pkg_tab.click()
                                    page.wait_for_timeout(1500)
                            except Exception:
                                pass

                            # Fill container-level fields
                            if "Container_Type" in fields_to_update:
                                _fill_dropdown_by_label(page, "Container Type", fields_to_update["Container_Type"])

                            if "Container_No" in fields_to_update:
                                val = fields_to_update["Container_No"]
                                filled = False
                                for sel in ["#containerNumber", "#containerNo"]:
                                    if _fill_text_by_id(page, sel.replace("#", ""), val):
                                        filled = True
                                        break
                                if not filled:
                                    # JS label fallback
                                    page.evaluate("""(val) => {
                                        for (const label of document.querySelectorAll('.el-form-item__label')) {
                                            if (label.textContent.trim().toLowerCase().includes('container n')) {
                                                const item = label.closest('.el-form-item');
                                                if (!item) continue;
                                                const inp = item.querySelector('input.el-input__inner:not([readonly])');
                                                if (inp) {
                                                    const setter = Object.getOwnPropertyDescriptor(
                                                        window.HTMLInputElement.prototype, 'value').set;
                                                    setter.call(inp, val);
                                                    inp.dispatchEvent(new Event('input', {bubbles: true}));
                                                    inp.dispatchEvent(new Event('change', {bubbles: true}));
                                                    return;
                                                }
                                            }
                                        }
                                    }""", val)

                            # Fill package fields — need to click existing package row first
                            pkg_fields = {k: v for k, v in fields_to_update.items()
                                          if k in ("Qty", "Package_Type", "Total_Gross_Weight", "Volume")}
                            if pkg_fields:
                                # Click first package row to open popup
                                # (import_process style: click table row by index)
                                try:
                                    page.evaluate("""() => {
                                        const rows = document.querySelectorAll('table tbody tr');
                                        if (rows.length > 0) rows[0].click();
                                    }""")
                                    page.wait_for_timeout(1500)
                                    try:
                                        page.wait_for_selector(".el-form-item", timeout=5000)
                                    except Exception:
                                        pass

                                    # ── Qty (import_process style) ──
                                    if "Qty" in pkg_fields:
                                        qty_val = pkg_fields["Qty"]
                                        filled = False
                                        for sel in ["#qty", "#quantity"]:
                                            try:
                                                inp = page.locator(sel).first
                                                if inp.is_visible(timeout=1500) and inp.get_attribute("readonly") is None:
                                                    inp.click()
                                                    page.wait_for_timeout(200)
                                                    inp.fill("")
                                                    inp.fill(str(qty_val))
                                                    page.wait_for_timeout(300)
                                                    filled = True
                                                    break
                                            except Exception:
                                                continue
                                        if not filled:
                                            # JS label fallback (qty/quantity only)
                                            page.evaluate("""([val]) => {
                                                for (const label of document.querySelectorAll('.el-form-item__label')) {
                                                    const t = label.textContent.trim().toLowerCase();
                                                    if (t.includes('qty') || t.includes('quantity') || t.includes('number of')) {
                                                        const item = label.closest('.el-form-item');
                                                        if (!item) continue;
                                                        const inp = item.querySelector('input.el-input__inner:not([readonly])');
                                                        if (!inp) continue;
                                                        const setter = Object.getOwnPropertyDescriptor(
                                                            window.HTMLInputElement.prototype, 'value').set;
                                                        setter.call(inp, '');
                                                        inp.dispatchEvent(new Event('input', {bubbles: true}));
                                                        setter.call(inp, val);
                                                        inp.dispatchEvent(new Event('input', {bubbles: true}));
                                                        inp.dispatchEvent(new Event('change', {bubbles: true}));
                                                        return;
                                                    }
                                                }
                                            }""", [str(qty_val)])
                                            page.wait_for_timeout(300)

                                    # ── Package Type (import_process style: selector first, then label) ──
                                    if "Package_Type" in pkg_fields:
                                        pkg_val = _normalize_pkg_type(pkg_fields["Package_Type"])
                                        filled = False
                                        # Strategy 1: ID selectors (same as import_process)
                                        for sel in ["#packageType", "#unit"]:
                                            try:
                                                inp = page.locator(sel).first
                                                if inp.is_visible(timeout=1500):
                                                    if inp.get_attribute("readonly") is not None:
                                                        inp.click()
                                                        page.wait_for_timeout(800)
                                                        _pick_dropdown_option(page, pkg_val)
                                                        filled = True
                                                    else:
                                                        inp.click()
                                                        page.wait_for_timeout(200)
                                                        inp.fill("")
                                                        inp.fill(pkg_val)
                                                        page.wait_for_timeout(300)
                                                        filled = True
                                                    break
                                            except Exception:
                                                continue
                                        # Strategy 2: Label-anchored dropdown (EXACT label match to avoid Container Type)
                                        if not filled:
                                            clicked = page.evaluate("""() => {
                                                for (const label of document.querySelectorAll('.el-form-item__label')) {
                                                    const t = label.textContent.trim().toLowerCase();
                                                    // EXACT: must be "package" or "unit" label, NOT "container type"
                                                    if (t === 'package' || t === 'unit' || t === 'package type' || t === 'unit type') {
                                                        const item = label.closest('.el-form-item');
                                                        if (!item) continue;
                                                        const inp = item.querySelector('input.el-input__inner[readonly]');
                                                        if (inp) { inp.click(); return true; }
                                                    }
                                                }
                                                return false;
                                            }""")
                                            if clicked:
                                                page.wait_for_timeout(800)
                                                _pick_dropdown_option(page, pkg_val)
                                                filled = True
                                        if not filled:
                                            _log_update(f"  Package_Type fill FAILED for '{pkg_val}'")

                                    # ── Weight (import_process style) ──
                                    if "Total_Gross_Weight" in pkg_fields:
                                        wt_val = pkg_fields["Total_Gross_Weight"]
                                        filled = False
                                        for sel in ["#weight", "#grossWeight", "#totalGrossWeight"]:
                                            try:
                                                inp = page.locator(sel).first
                                                if inp.is_visible(timeout=1500) and inp.get_attribute("readonly") is None:
                                                    inp.click()
                                                    page.wait_for_timeout(200)
                                                    inp.fill("")
                                                    inp.fill(str(wt_val))
                                                    page.wait_for_timeout(300)
                                                    filled = True
                                                    break
                                            except Exception:
                                                continue
                                        if not filled:
                                            page.evaluate("""([val]) => {
                                                for (const label of document.querySelectorAll('.el-form-item__label')) {
                                                    const t = label.textContent.trim().toLowerCase();
                                                    if (t.includes('weight') || t.includes('gross')) {
                                                        const item = label.closest('.el-form-item');
                                                        if (!item) continue;
                                                        const inp = item.querySelector('input.el-input__inner:not([readonly])');
                                                        if (!inp) continue;
                                                        const setter = Object.getOwnPropertyDescriptor(
                                                            window.HTMLInputElement.prototype, 'value').set;
                                                        setter.call(inp, '');
                                                        inp.dispatchEvent(new Event('input', {bubbles: true}));
                                                        setter.call(inp, val);
                                                        inp.dispatchEvent(new Event('input', {bubbles: true}));
                                                        inp.dispatchEvent(new Event('change', {bubbles: true}));
                                                        return;
                                                    }
                                                }
                                            }""", [str(wt_val)])
                                            page.wait_for_timeout(300)

                                    # ── Volume (import_process style) ──
                                    if "Volume" in pkg_fields:
                                        vol_val = pkg_fields["Volume"]
                                        filled = False
                                        for sel in ["#volume", "#totalVolume"]:
                                            try:
                                                inp = page.locator(sel).first
                                                if inp.is_visible(timeout=1500) and inp.get_attribute("readonly") is None:
                                                    inp.click()
                                                    page.wait_for_timeout(200)
                                                    inp.fill("")
                                                    inp.fill(str(vol_val))
                                                    page.wait_for_timeout(300)
                                                    filled = True
                                                    break
                                            except Exception:
                                                continue
                                        if not filled:
                                            page.evaluate("""([val]) => {
                                                for (const label of document.querySelectorAll('.el-form-item__label')) {
                                                    const t = label.textContent.trim().toLowerCase();
                                                    if (t.includes('volume') || t.includes('cbm') || t.includes('measurement')) {
                                                        const item = label.closest('.el-form-item');
                                                        if (!item) continue;
                                                        const inp = item.querySelector('input.el-input__inner:not([readonly])');
                                                        if (!inp) continue;
                                                        const setter = Object.getOwnPropertyDescriptor(
                                                            window.HTMLInputElement.prototype, 'value').set;
                                                        setter.call(inp, '');
                                                        inp.dispatchEvent(new Event('input', {bubbles: true}));
                                                        setter.call(inp, val);
                                                        inp.dispatchEvent(new Event('input', {bubbles: true}));
                                                        inp.dispatchEvent(new Event('change', {bubbles: true}));
                                                        return;
                                                    }
                                                }
                                            }""", [str(vol_val)])
                                            page.wait_for_timeout(300)

                                    # Save popup (import_process _click_popup_save style)
                                    try:
                                        popup_save = page.locator(
                                            ".mf-form__footer-buttons .el-button--primary"
                                        ).last
                                        if popup_save.is_visible(timeout=3000):
                                            popup_save.click()
                                            page.wait_for_timeout(2000)
                                            try:
                                                ok = page.locator("button:has-text('OK'):visible").first
                                                if ok.is_visible(timeout=2000):
                                                    ok.click()
                                                    page.wait_for_timeout(1000)
                                            except Exception:
                                                pass
                                    except Exception:
                                        pass

                                except Exception as pe:
                                    _log_update(f"  Package update failed for {cno}: {pe}")

                            # Fill goods description
                            if "Goods_Description" in fields_to_update:
                                try:
                                    goods_tab = page.locator("#tab-goods, [id*='tab-goods']").first
                                    if not goods_tab.is_visible(timeout=2000):
                                        goods_tab = page.get_by_role("tab", name="Goods").first
                                    if goods_tab.is_visible(timeout=2000):
                                        goods_tab.click()
                                        page.wait_for_timeout(1000)
                                        ta = page.locator("textarea:visible").first
                                        if ta.is_visible(timeout=2000) and ta.get_attribute("readonly") is None:
                                            ta.click()
                                            ta.fill("")
                                            ta.fill(fields_to_update["Goods_Description"])
                                except Exception as ge:
                                    _log_update(f"  Goods fill failed: {ge}")

                            # Save the container tab
                            _click_save(page)
                            _log_update(f"  Container {cno} saved.")

                            # Navigate back to cargo list
                            try:
                                page.locator(".el-tabs__item:has-text('Cargo')").first.click()
                                page.wait_for_timeout(2000)
                            except Exception:
                                pass

                        _log_update("Cargo updates complete.")

                    elif tab_upper == "PARTIES":
                        _log_update("Parties update not yet automated.")

                    elif tab_upper == "ROUTING":
                        _log_update("Routing update — navigating...")
                        # TODO: implement routing field update

                _update_status[folder]["status"] = "completed"
                _log_update("Update complete.")

                # ── Update Comparison_Result.json: MISMATCH → UPDATED ──
                try:
                    comp_path = os.path.join(SHIPMENTS_ROOT, folder, "Comparison_Result.json")
                    if os.path.exists(comp_path):
                        with open(comp_path, "r") as cf:
                            comp = json.load(cf)

                        updated_fields = set()
                        for tab, tab_fields in fields.items():
                            for field_key in tab_fields:
                                updated_fields.add(field_key)

                        changed = False
                        for section in ["Parties", "Carrier"]:
                            for item in comp.get(section, []):
                                if item.get("field") in updated_fields and item.get("status") in ("MISMATCH", "ADDED"):
                                    item["status"] = "UPDATED"
                                    changed = True

                        for section in ["Cargo", "Routing"]:
                            for container in comp.get(section, []):
                                for field in container.get("fields", []):
                                    if field.get("field") in updated_fields and field.get("status") in ("MISMATCH", "ADDED"):
                                        field["status"] = "UPDATED"
                                        changed = True

                        if changed:
                            with open(comp_path, "w") as cf:
                                json.dump(comp, cf, indent=2, ensure_ascii=False)
                            _log_update("Comparison result updated (MISMATCH → UPDATED).")
                except Exception as ue:
                    log.warning("Could not update comparison JSON: %s", ue)

        except Exception as e:
            log.error("Update failed for %s: %s", folder, e)
            _update_status[folder]["status"] = "failed"
            _update_status[folder]["message"] = str(e)

        go_back_to_list(page)
        page.wait_for_timeout(2000)

    # Close browser after queue is empty
    try:
        page.context.close()
    except Exception:
        pass


# ═══════════════════════════════════════════════════════════════════════
#  MAIN ENTRY POINT
# ═══════════════════════════════════════════════════════════════════════
def main(status_callback=None, headless=False, cancel_event_ext=None):
    """
    Autonomous processing loop.
    Modes:
      - normal: scan date window, process new shipments
      - no_doc_mode: re-check no_doc.json entries
      - old_file_mode: scan past dates, skip OIs that have docs, push no-doc ones to queue
    """
    global SESSION_ID, cancel_event

    if cancel_event_ext:
        cancel_event = cancel_event_ext

    def log_status(msg):
        log.info(msg)
        if status_callback:
            status_callback(msg)
        if cancel_event and cancel_event.is_set():
            raise KeyboardInterrupt("Cancel requested")

    SESSION_ID = datetime.now().strftime("%Y%m%d_%H%M%S")
    settings = load_settings()

    log_status("Starting Autonomous Sequence...")

    slot = _browser_pool.acquire()
    if not slot:
        log_status("No browser available. Skipping.")
        return

    try:
        if not _browser_pool.ensure_slot_ready(slot):
            log_status("Failed to ready browser. Skipping.")
            return

        page = slot["page"]

        log_status("Applying filters...")
        apply_filters(page, status_callback=status_callback)

        custom_start = settings.get("date_start")
        custom_end = settings.get("date_end")
        if custom_start and custom_end:
            log_status(f"Custom date window: {custom_start} -> {custom_end}")
            apply_date_filter(page, custom_start, custom_end, status_cb=status_callback)

        log_status("Waiting for search results...")
        try:
            page.locator(".el-loading-mask").wait_for(state="hidden", timeout=10000)
        except Exception:
            pass
        page.wait_for_timeout(3000)

        checked = load_checked()
        no_doc = load_no_doc()
        processed_count = 0

        date_window = build_date_window()
        no_doc_mode = settings.get("no_doc_mode", False)
        old_file_mode = settings.get("old_file_mode", False)

        log_status(f"Date window: {date_window}")
        log_status(f"Mode: {'OLD_FILE' if old_file_mode else 'NO_DOC' if no_doc_mode else 'NORMAL'}")

        # ══════════════════════════════════════════════
        #  OLD FILE MODE
        # ══════════════════════════════════════════════
        if old_file_mode:
            log_status("Phase: Old File Mode — scanning ALL rows page by page...")

            go_to_first_page(page)
            page_num = 1
            total_pages_scanned = 0
            max_pages = 20  # safety limit

            while processed_count < MAX_PROCESSED and total_pages_scanned < max_pages:
                if cancel_event and cancel_event.is_set():
                    raise KeyboardInterrupt()

                log_status(f"--- Old File: Page {page_num} ---")
                total_pages_scanned += 1

                try:
                    page.locator(".el-table__body").wait_for(state="visible", timeout=10000)
                except Exception:
                    pass

                # Get ALL visible rows on this page (no date filter)
                all_rows = []
                for sel in [
                    ".el-table__body tr",
                    ".el-table__body .el-table__row",
                    "table tbody tr",
                ]:
                    try:
                        rows = page.locator(sel).all()
                        visible = [r for r in rows if r.is_visible()]
                        if visible:
                            all_rows = visible
                            break
                    except Exception:
                        continue

                if not all_rows:
                    log_status(f"  No rows on page {page_num}, stopping.")
                    break

                log_status(f"  {len(all_rows)} row(s) on page {page_num}")

                for index in range(len(all_rows)):
                    if processed_count >= MAX_PROCESSED or (cancel_event and cancel_event.is_set()):
                        break

                    opened_shipment = False

                    try:
                        # Re-fetch rows (DOM may have changed after go_back_to_list)
                        current_rows = []
                        for sel in [
                            ".el-table__body tr",
                            ".el-table__body .el-table__row",
                            "table tbody tr",
                        ]:
                            try:
                                rows = page.locator(sel).all()
                                visible = [r for r in rows if r.is_visible()]
                                if visible:
                                    current_rows = visible
                                    break
                            except Exception:
                                continue

                        if not current_rows or index >= len(current_rows):
                            log_status(f"  Row index {index} out of range, breaking.")
                            break

                        row = current_rows[index]
                        row.scroll_into_view_if_needed(timeout=10000)
                        ref_no = extract_ref_number(row).strip()

                        if ref_no == "UNKNOWN_REF":
                            continue

                        # Extract date from row text for folder naming
                        row_text = ""
                        try:
                            row_text = row.inner_text(timeout=2000).strip()
                        except Exception:
                            pass

                        # Find which date from our window matches this row
                        row_date_str = ""
                        for dw in date_window:
                            if dw in row_text:
                                row_date_str = dw
                                break

                        # Fallback: try to extract date from DUE DATE column
                        if not row_date_str:
                            try:
                                cells = row.locator("td").all()
                                if cells:
                                    last_cell_text = cells[-1].inner_text(timeout=1000).strip()
                                    for dw in date_window:
                                        if dw in last_cell_text:
                                            row_date_str = dw
                                            break
                            except Exception:
                                pass

                        if not row_date_str:
                            # Use first date in window as fallback
                            row_date_str = date_window[0] if date_window else "unknown"

                        shipment_key = f"{row_date_str.replace(' ', '-')}__{ref_no}"

                        if shipment_key in checked or shipment_key in no_doc:
                            log_status(f"  [{index+1}] {ref_no} ({row_date_str}): Already checked/no-doc — skipping.")
                            continue

                        log_status(f"  [{index+1}] {ref_no} ({row_date_str}): Opening to check docs...")

                        # Open shipment
                        try:
                            row.click(timeout=10000)
                            page.wait_for_load_state("load", timeout=30000)
                            _apply_zoom(page)
                            page.get_by_text("Parties", exact=True).first.wait_for(
                                state="visible", timeout=15000
                            )
                            opened_shipment = True
                        except Exception as e:
                            log.warning("Could not open %s: %s", ref_no, e)

                        if not opened_shipment:
                            go_back_to_list(page)
                            page.wait_for_timeout(2000)
                            continue

                        # Check Documents tab
                        has_docs = False
                        try:
                            doc_tab = page.get_by_text("Documents", exact=True)
                            doc_tab.scroll_into_view_if_needed()
                            doc_tab.click()
                            page.wait_for_timeout(2000)

                            for _ in range(5):
                                rows_check = page.locator(
                                    "table tr:has-text('House'), table tr:has-text('Master')"
                                ).all()
                                if rows_check:
                                    has_docs = True
                                    break
                                page.wait_for_timeout(1000)

                        except Exception as e:
                            log.warning("Doc tab check failed for %s: %s", ref_no, e)

                        if has_docs:
                            log_status(f"  {ref_no}: Has documents — SKIPPING.")
                            go_back_to_list(page)
                            page.wait_for_timeout(2000)
                            continue

                        # No docs → scrape system data and build comment
                        log_status(f"  {ref_no}: No documents. Building Old Doc entry...")
                        save_dir = make_shipment_folder(row_date_str, ref_no)

                        system_data = {
                            "Parties": scrape_parties(page),
                            "Carrier": scrape_carrier(page),
                            "Cargo": scrape_cargo(page, ref_no),
                        }

                        sys_path = os.path.join(save_dir, "System_Data.json")
                        with open(sys_path, "w", encoding="utf-8") as f:
                            json.dump(system_data, f, indent=2, ensure_ascii=False)

                        missing = []
                        cd = system_data.get("Carrier", {})
                        cg = system_data.get("Cargo", [])

                        if not cd.get("HBL_Number", "").strip() and not cd.get("MBL_Number", "").strip():
                            missing.append("HBL, MBL Number")
                        elif not cd.get("HBL_Number", "").strip():
                            missing.append("HBL Number")
                        elif not cd.get("MBL_Number", "").strip():
                            missing.append("MBL Number")

                        # Only check container number for FCL — LCL shipments don't have container numbers
                        is_lcl = any(
                            "LCL" in str(c.get("Load_Type", "")).upper()
                            for c in cg
                        )
                        if not is_lcl:
                            if not any(c.get("Container_No", "").strip() for c in cg):
                                missing.append("Container Number")
                        if not cd.get("Carrier", "").strip():
                            missing.append("Carrier")
                        if not cd.get("Vessel_Name", "").strip():
                            missing.append("Vessel")

                        comment = (", ".join(missing) + " and Documents Not Available") if missing else "No Documents Available"

                        res_data = {
                            "comment": comment,
                            "status": "pending_old_doc_support",
                            "ref_no": ref_no,
                            "date_str": row_date_str,
                            "processed_at": datetime.now().isoformat()
                        }
                        with open(os.path.join(save_dir, "result.json"), "w", encoding="utf-8") as f:
                            json.dump(res_data, f, indent=2, ensure_ascii=False)

                        add_no_doc_entry(no_doc, shipment_key, ref_no, row_date_str, old_doc_support=True)
                        save_no_doc(no_doc)

                        processed_count += 1
                        log_status(f"  [{processed_count}/{MAX_PROCESSED}] {ref_no}: Queued as Old Doc.")

                        go_back_to_list(page)
                        page.wait_for_timeout(2000)

                    except Exception as e:
                        log.warning("Old file row %d error: %s", index + 1, e)
                        if opened_shipment:
                            go_back_to_list(page)
                            page.wait_for_timeout(2000)

                if processed_count >= MAX_PROCESSED:
                    break

                # Move to next page
                if try_next_page(page):
                    page_num += 1
                else:
                    log_status(f"  No more pages after page {page_num}.")
                    break

            log_status(f"Old File mode complete. Queued {processed_count} entries.")
            return

        # ══════════════════════════════════════════════
        #  NO_DOC MODE
        # ══════════════════════════════════════════════
        if no_doc_mode:
            if not no_doc:
                log_status("No-doc mode: no entries to re-check.")
            else:
                log_status(f"No-doc mode: re-checking {len(no_doc)} entries...")
                recheck_keys = list(no_doc.keys())
                for shipment_key in recheck_keys:
                    if processed_count >= MAX_PROCESSED:
                        break
                    if cancel_event and cancel_event.is_set():
                        raise KeyboardInterrupt()

                    entry = no_doc[shipment_key]
                    ref_no = entry["ref"]
                    date_str = entry["date"]

                    log_status(f"Re-checking {ref_no} (date: {date_str})...")

                    try:
                        apply_filters(page, status_callback=status_callback)
                        page.wait_for_timeout(2000)
                        try:
                            page.locator(".el-loading-mask").wait_for(state="hidden", timeout=10000)
                        except Exception:
                            pass
                    except Exception:
                        pass

                    opened = search_and_open_shipment(page, ref_no, date_str, log_fn=log_status)
                    if not opened:
                        log_status(f"  {ref_no}: Not found — keeping in no-doc.")
                        add_no_doc_entry(no_doc, shipment_key, ref_no, date_str)
                        save_no_doc(no_doc)
                        continue

                    try:
                        docs_found, original_paths, save_dir = process_shipment_documents(
                            page, date_str, ref_no
                        )

                        if not docs_found:
                            log_status(f"  {ref_no}: Still no documents.")
                            add_no_doc_entry(no_doc, shipment_key, ref_no, date_str)
                            save_no_doc(no_doc)
                            go_back_to_list(page)
                            page.wait_for_timeout(2000)
                            continue

                        write_processing_log(save_dir, ref_no, date_str, "docs_downloaded",
                                             f"{len(original_paths)} original files")

                        extraction = extract_documents(original_paths, save_dir, status_cb=log_status)
                        hbl_list = extraction.get("hbl", [])
                        mbl_data = extraction.get("mbl") or {}

                        if hbl_list:
                            hbl_data, hbl_numbers, merge_type = merge_hbl_data(hbl_list)
                        else:
                            hbl_data = {}

                        system_data = {
                            "Parties": scrape_parties(page),
                            "Carrier": scrape_carrier(page),
                            "Cargo": scrape_cargo(page, ref_no),
                        }
                        with open(os.path.join(save_dir, "System_Data.json"), "w", encoding="utf-8") as f:
                            json.dump(system_data, f, indent=2, ensure_ascii=False)

                        all_container_nos = [
                            c.get("Container_No", "").strip()
                            for c in system_data.get("Cargo", [])
                            if c.get("Container_No", "").strip()
                        ]
                        primary_container = all_container_nos[0] if all_container_nos else None

                        carrier_str = system_data.get("Carrier", {}).get("Carrier", "")
                        matched_carrier = Tracking.find_carrier_code(carrier_str) if Tracking else None
                        if not matched_carrier and mbl_data and Tracking:
                            matched_carrier = Tracking.find_carrier_code(mbl_data.get("carrier_name", ""))

                        # Track only primary container
                        tracking_results = {}
                        if (matched_carrier and primary_container and Tracking
                                and matched_carrier.upper() in SUPPORTED_CARRIERS
                                and _normalize_carrier_name(matched_carrier) != "OOCL"):
                            log_status(f"  {ref_no}: Tracking {primary_container}...")
                            try:
                                tracking_results[primary_container] = Tracking.track_shipment_thread_safe(
                                    matched_carrier, primary_container, save_dir, log_status
                                )
                            except Exception as te:
                                log.error("Tracking failed: %s", te)

                        routing_data = {}
                        for attempt in range(3):
                            try:
                                routing_data = scrape_routing(page, save_dir, all_container_nos)
                                if routing_data:
                                    break
                            except Exception as e:
                                log.warning(f"Routing scrape attempt {attempt + 1} failed: {e}")
                                time.sleep(2)
                        write_processing_log(save_dir, ref_no, date_str, "routing_scraped")

                        checked.add(shipment_key)
                        save_checked(checked)
                        remove_no_doc_entry(no_doc, shipment_key)
                        save_no_doc(no_doc)

                        processed_count += 1
                        log_status(f"  [{processed_count}/{MAX_PROCESSED}] {ref_no} done.")

                    except Exception as e:
                        log.error("No-doc retry failed for %s: %s", ref_no, e)
                        import traceback; traceback.print_exc()
                    finally:
                        go_back_to_list(page)
                        page.wait_for_timeout(2000)

            log_status(f"No-doc mode complete. Processed {processed_count}.")
            return

        # ══════════════════════════════════════════════
        #  NORMAL MODE
        # ══════════════════════════════════════════════
        log_status("Phase: Scanning date window for new shipments...")

        go_to_first_page(page)
        date_idx = 0
        page_num = 1

        while date_idx < len(date_window) and processed_count < MAX_PROCESSED:
            if cancel_event and cancel_event.is_set():
                raise KeyboardInterrupt()

            date_str = date_window[date_idx]
            log_status(f"--- Date: {date_str} ({date_idx + 1}/{len(date_window)}) | Page {page_num} ---")

            try:
                page.locator(".el-table__body").wait_for(state="visible", timeout=10000)
            except Exception:
                pass

            all_rows = get_all_rows_for_date(page, date_str)

            if not all_rows:
                date_idx += 1
                if date_idx < len(date_window):
                    continue
                else:
                    break

            rows_count = len(all_rows)
            log_status(f"  {rows_count} row(s) for {date_str} on page {page_num}.")

            for index in range(rows_count):
                if processed_count >= MAX_PROCESSED:
                    break
                if cancel_event and cancel_event.is_set():
                    raise KeyboardInterrupt()

                try:
                    current_rows = get_all_rows_for_date(page, date_str)
                    if not current_rows or index >= len(current_rows):
                        break

                    row = current_rows[index]
                    row.scroll_into_view_if_needed(timeout=10000)

                    ref_no = extract_ref_number(row).strip()
                    shipment_key = f"{date_str.replace(' ', '-')}__{ref_no}"

                    log_status(f"  Row [{index + 1}/{rows_count}]: {ref_no}")

                except Exception as e:
                    log.warning("Row %d detached: %s", index + 1, e)
                    page.evaluate("window.scrollTo(0, 0)")
                    page.wait_for_timeout(2000)
                    continue

                # Skip already-checked; stale no_doc handled inside process_single_shipment
                if shipment_key in checked:
                    continue

                result = process_single_shipment(
                    page, row, ref_no, date_str,
                    shipment_key, checked, no_doc, log_status
                )

                if result == "processed":
                    processed_count += 1
                    log_status(f"  [{processed_count}/{MAX_PROCESSED}] {ref_no} done.")

            if processed_count >= MAX_PROCESSED:
                break

            if try_next_page(page):
                page_num += 1
                continue
            else:
                date_idx += 1
                page_num = 1
                go_to_first_page(page)

    except KeyboardInterrupt:
        log.info("Shutdown requested.")
    except Exception as e:
        log.error("Unhandled error: %s", e)
        import traceback
        traceback.print_exc()
        try:
            page.screenshot(path="shipment_process_error.png")
        except Exception:
            pass
    finally:
        log_status(f"Complete. Processed {processed_count} shipment(s).")
        log_status(f"No-doc: {len(no_doc)}, Checked: {len(checked)}")

        try:
            go_back_to_list(page)
            page.wait_for_timeout(2000)
        except Exception:
            pass

        try:
            slot["page"].context.close()
        except Exception:
            pass
        slot["page"] = None

        _browser_pool.release(slot)

def recompare_shipment_with_ai(shipment_key: str) -> dict:
    """
    On-demand AI comparison. Loads existing local JSON files and re-runs the LLM prompt.
    Returns the new comparison result dict.
    """
    save_dir = os.path.join(SHIPMENTS_ROOT, shipment_key)
    if not os.path.exists(save_dir):
        raise FileNotFoundError(f"Shipment folder not found: {save_dir}")

    def _load_json(filename):
        path = os.path.join(save_dir, filename)
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception as e:
                log.error(f"Failed to load {filename}: {e}")
        return {}

    system_data = _load_json("System_Data.json")
    routing_data = _load_json("view_routing.json")
    
    tracking_results = _load_json("Tracking_Results.json")
    if not tracking_results and routing_data:
        # Fallback: Load individual {cno}_result.json files if they exist
        for cno in routing_data.keys():
            cno_result = _load_json(f"{cno}_result.json")
            if cno_result:
                tracking_results[cno] = cno_result

    extraction_data = _load_json("Extraction_Data.json")
    if extraction_data:
        hbl_data = extraction_data.get("hbl", {})
        mbl_data = extraction_data.get("mbl", {})
    else:
        # Fallback for old shipments processed before Extraction_Data.json was added
        hbl_data = {}
        mbl_data = {}
        if os.path.exists(save_dir):
            for fname in os.listdir(save_dir):
                if fname.endswith(".json"):
                    if fname.startswith("House_BL"):
                        hbl_data = _load_json(fname)
                    elif fname.startswith("Master_BL"):
                        mbl_data = _load_json(fname)

    log.info(f"Re-running AI Comparison for {shipment_key}...")

    is_direct_file = not bool(hbl_data)

    comparison = compare_data(
        system_data, hbl_data, mbl_data,
        tracking_results, routing_data, is_direct_file
    )

    log.info(f"Re-running AI Routing Comparison for {shipment_key}...")
    routing_result = _compare_routing_with_ai(routing_data, tracking_results)
    comparison["Routing"] = routing_result

    comparison_path = os.path.join(save_dir, "Comparison_Result.json")
    try:
        with open(comparison_path, "w", encoding="utf-8") as f:
            json.dump(comparison, f, indent=2, ensure_ascii=False)
        log.info(f"Updated {comparison_path}")
    except Exception as ce:
        log.error("Failed to write Comparison_Result.json: %s", ce)

    return comparison

def re_extract_shipment_documents(shipment_key: str) -> dict:
    """
    Finds existing PDFs in the shipment folder and re-runs the LLM extraction.
    Updates House_BL.json, Master_BL.json, and Extraction_Data.json.
    """
    save_dir = os.path.join(SHIPMENTS_ROOT, shipment_key)
    if not os.path.exists(save_dir):
        raise FileNotFoundError(f"Shipment folder not found: {save_dir}")

    # Find all PDFs in the folder (excluding comparison or irrelevant PDFs if any)
    original_paths = []
    for f in os.listdir(save_dir):
        if f.endswith(".pdf"):
            original_paths.append(os.path.join(save_dir, f))

    if not original_paths:
        raise ValueError("No PDF files found to extract in this shipment.")

    log.info(f"Re-extracting {len(original_paths)} documents for {shipment_key}...")
    
    # We can pass log_status as a lambda or ignore
    extraction = extract_documents(original_paths, save_dir, status_cb=None)
    
    hbl_data = extraction["hbl"][0] if extraction["hbl"] else {}
    mbl_data = extraction["mbl"] or {}
    
    # Update Extraction_Data.json
    extract_path = os.path.join(save_dir, "Extraction_Data.json")
    try:
        with open(extract_path, "w", encoding="utf-8") as f:
            json.dump({"hbl": hbl_data, "mbl": mbl_data}, f, indent=2, ensure_ascii=False)
    except Exception as e:
        log.error("Failed to write Extraction_Data.json: %s", e)

    # Log it
    try:
        parts = shipment_key.split("__", 1)
        ref_no = parts[1] if len(parts) > 1 else shipment_key
        log_oi_event(ref_no, "info", "Re-extracted documents via AI")
    except Exception:
        pass

    return {"hbl": hbl_data, "mbl": mbl_data}
if __name__ == "__main__":
    main()
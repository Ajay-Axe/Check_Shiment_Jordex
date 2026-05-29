"""
Tracking.py — Jordex Carrier Tracking Engine
=============================================
Combines:
  - CARRIER_MAP: short-code → aliases (for resolving carrier names from BL extraction)
  - find_carrier_code(): maps extracted carrier_name to a short code
  - track_shipment_thread_safe(): thread-safe Playwright tracking entry point
  - extract_with_vision(): Gemini Vision → structured JSON
  - All carrier-specific navigators (MSC, Maersk, ONE, Yang Ming, Evergreen, OOCL, HMM, COSCO, Hapag-Lloyd)
  - Stealth bypasses for HMM and Hapag-Lloyd

Flow:
  worker.py calls → run_tracking_for_folder(folder_name, mbl_data, hbl_data)
    → resolves carrier from MBL carrier_name
    → for each container in HBL, calls track_shipment_thread_safe()
    → saves {container}_tracking.png + {container}_result.json into Shipments/<OI>/tracking/
"""

import os
import json
import logging
import re
import time
import threading

from playwright.sync_api import sync_playwright, Page
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

try:
    from json_repair import repair_json
except ImportError:
    def repair_json(s): return s  # fallback: no repair

try:
    from playwright_stealth import stealth_sync
except ImportError:
    pass

try:
    from scrapling.fetchers import StealthyFetcher
except ImportError:
    pass

try:
    from google import genai
    from google.genai import types
except ImportError:
    pass

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

GEMINI_API_KEY     = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL       = os.getenv("GEMINI_MODEL", "gemini-2.5-flash-lite")
GEMINI_MODEL_SMART = os.getenv("GEMINI_MODEL_SMART", "gemini-2.5-flash")

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)-7s | %(message)s")
log = logging.getLogger("Tracking")

# ──────────────────────────────────────────────────────────────────────────────
# CARRIER MAPPING
# short_code → list of name aliases (used to resolve carrier_name from BL JSON)
# ──────────────────────────────────────────────────────────────────────────────

CARRIER_MAP = {
    "MSC":        ["MEDITERRANEAN SHIPPING COMPANY", "MSC", "MEDITERRANEAN SHIPPING CO",
                   "MEDITERRANEAN SHIPPIN"],
    "MAERSK":     ["MAERSK", "A.P. MOLLER - MAERSK", "MAERSK LINE", "AP MOLLER MAERSK"],
    "ONE":        ["OCEAN NETWORK EXPRESS", "ONE LINE", "ONE"],
    "EVERGREEN":  ["EVERGREEN", "EVERGREEN LINE", "EMC", "EVERGREEN MARINE"],
    "YANG MING":  ["YANG MING", "YANG MING LINE", "YML", "YANG MING MARINE"],
    "HAPAG":      ["HAPAG-LLOYD", "HAPAG LLOYD", "HAPAG", "HAPAG-LLOYD AKTIENGESELLSCHAFT"],
    "COSCO":      ["COSCO", "COSCO SHIPPING", "COSCO LINE", "COSCO SHIPPING LINES"],
    "HMM":        ["HMM", "HYUNDAI MERCHANT MARINE", "HMM CO"],
    "ZIM":        ["ZIM", "ZIM INTEGRATED SHIPPING", "ZIM LINE", "ZIM INTEGRATED SHIPPING SERVICES LTD"],
    "PIL":        ["PIL", "PACIFIC INTERNATIONAL LINES"],
    "OOCL":       ["OOCL", "ORIENT OVERSEAS CONTAINER LINE", "ORIENT OVERSEAS"],
    "WAN HAI":    ["WAN HAI", "WAN HAI LINES", "WHL"],
}

# Maps short codes to Carrier_Logic carrier keys (for navigator registry)
CODE_TO_CARRIER_KEY = {
    "MSC":       "MSC",
    "MAERSK":    "Maersk",
    "ONE":       "ONE",
    "EVERGREEN": "Evergreen",
    "YANG MING": "Yang Ming",
    "HAPAG":     "Hapag-Lloyd",
    "COSCO":     "COSCO",
    "HMM":       "HMM",
    "OOCL":      "OOCL",
    "ZIM":       "ZIM",
}
# ──────────────────────────────────────────────────────────────────────────────
# ADD THIS BLOCK after CARRIER_MAP and CODE_TO_CARRIER_KEY in Tracking.py
# This maps carrier names to Jordex dropdown search codes (from extractor.py)
# ──────────────────────────────────────────────────────────────────────────────

# Jordex dropdown search codes — used when filling the Carrier field in Jordex UI
CARRIER_NAME_TO_CODE = {
    "HAPAG-LLOYD": "HAPAG LLOYD",
    "HAPAG LLOYD": "HAPAG LLOYD",
    "HAPAG-LLOYD AKTIENGESELLSCHAFT": "HAPAG LLOYD",
    "OOCL": "OOLU",
    "ORIENT OVERSEAS CONTAINER LINE": "OOLU",
    "YANG MING": "YMJA",
    "YANG MING LINE": "YMJA",
    "YANG MING MARINE": "YMJA",
    "MSC": "MSCU",
    "MEDITERRANEAN SHIPPING COMPANY": "MSCU",
    "MEDITERRANEAN SHIPPING CO": "MSCU",
    "MAERSK": "MAEU",
    "MAERSK LINE": "MAEU",
    "A.P. MOLLER - MAERSK": "MAEU",
    "AP MOLLER MAERSK": "MAEU",
    "OCEAN NETWORK EXPRESS": "ONEY",
    "ONE LINE": "ONEY",
    "ONE": "ONEY",
    "EVERGREEN": "EGLV",
    "EVERGREEN LINE": "EGLV",
    "EVERGREEN MARINE": "EGLV",
    "EMC": "EGLV",
    "COSCO": "COEU",
    "COSCO SHIPPING": "COEU",
    "COSCO SHIPPING LINES": "COEU",
    "HMM": "HDMU",
    "HYUNDAI MERCHANT MARINE": "HDMU",
    "HMM CO": "HDMU",
    "ZIM": "ZIMU",
    "ZIM INTEGRATED SHIPPING": "ZIMU",
    "ZIM LINE": "ZIMU",
    "ZIM INTEGRATED SHIPPING SERVICES LTD": "ZIMU",
    "PIL": "PCIU",
    "PACIFIC INTERNATIONAL LINES": "PCIU",
    "WAN HAI": "WHLC",
    "WAN HAI LINES": "WHLC",
    "WHL": "WHLC",
}


def find_carrier_jordex_code(carrier_name: str) -> str:
    """
    Maps a carrier name to the Jordex dropdown search code.
    Used when filling the Carrier field in the Jordex UI.

    Examples:
      "HAPAG-LLOYD AKTIENGESELLSCHAFT, HAMBURG" -> "HAPAG LLOYD"
      "MEDITERRANEAN SHIPPING COMPANY S.A."     -> "MSCU"
      "OCEAN NETWORK EXPRESS PTE. LTD."         -> "ONEY"

    Falls back to the raw name if no mapping found.
    """
    if not carrier_name:
        return ""
    name_upper = carrier_name.upper().strip()

    # Direct match
    if name_upper in CARRIER_NAME_TO_CODE:
        return CARRIER_NAME_TO_CODE[name_upper]

    # Substring match: check if any key is contained in the name
    for key, code in CARRIER_NAME_TO_CODE.items():
        if key in name_upper:
            return code

    # Reverse: check if name is contained in any key
    for key, code in CARRIER_NAME_TO_CODE.items():
        if name_upper in key:
            return code

    return carrier_name  # fallback to raw name
# Carrier portal URLs
CARRIERS = {
    "MSC":         "https://www.msc.com/en/track-a-shipment",
    "Maersk":      "https://www.maersk.com/tracking/",
    "ONE":         "https://ecomm.one-line.com/one-ecom/manage-shipment/cargo-tracking",
    "OOCL":        "https://www.oocl.com/eng/ourservices/eservices/cargotracking/pages/cargotracking.aspx",
    "HMM":         "https://www.hmm21.com/e-service/general/trackNTrace/TrackNTrace.do",
    "Yang Ming":   "https://www.yangming.com/en/esolution/cargo_tracking",
    "COSCO":       "https://elines.coscoshipping.com/ebusiness/cargotracking",
    "Hapag-Lloyd": "https://www.hapag-lloyd.com/en/online-business/track/track-by-container-solution.html",
    "Evergreen":   "https://ct.shipmentlink.com/servlet/TDB1_CargoTracking.do",
    "ZIM":         "https://www.zim.com/tools/track-a-shipment",
}

SCREENSHOTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "Screenshots")
os.makedirs(SCREENSHOTS_DIR, exist_ok=True)


# ──────────────────────────────────────────────────────────────────────────────
# CARRIER RESOLUTION
# ──────────────────────────────────────────────────────────────────────────────

def find_carrier_code(name: str) -> str | None:
    """
    Maps a carrier name string (from BL extraction) to a short code.
    Returns e.g. "HAPAG", "MSC", "MAERSK", or None if unrecognised.

    Examples:
      "HAPAG-LLOYD AKTIENGESELLSCHAFT, HAMBURG" → "HAPAG"
      "MEDITERRANEAN SHIPPING COMPANY S.A."     → "MSC"
      "OCEAN NETWORK EXPRESS PTE. LTD."         → "ONE"
    """
    if not name:
        return None
    name_upper = name.upper().strip()

    # 1. Exact match on short code itself
    for code in CARRIER_MAP:
        if name_upper == code:
            return code

    # 2. Alias match: alias contained in name OR name contained in alias
    for code, aliases in CARRIER_MAP.items():
        for alias in aliases:
            if alias in name_upper or name_upper in alias:
                return code

    return None


# ──────────────────────────────────────────────────────────────────────────────
# FILE HELPERS
# ──────────────────────────────────────────────────────────────────────────────

def save_screenshot(screenshot_bytes: bytes, number: str, suffix: str,
                    save_dir: str = None) -> str:
    target_dir = save_dir or SCREENSHOTS_DIR
    os.makedirs(target_dir, exist_ok=True)
    filename = f"{number}_{suffix}.png"
    filepath = os.path.join(target_dir, filename)
    with open(filepath, "wb") as f:
        f.write(screenshot_bytes)
    log.info("Screenshot saved: %s", filepath)
    return filepath


def save_result_json(number: str, data: dict, save_dir: str = None) -> str:
    target_dir = save_dir or SCREENSHOTS_DIR
    os.makedirs(target_dir, exist_ok=True)
    filename = f"{number}_result.json"
    filepath = os.path.join(target_dir, filename)
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    log.info("Result JSON saved: %s", filepath)
    return filepath


# ──────────────────────────────────────────────────────────────────────────────
# DETECT INPUT TYPE
# ──────────────────────────────────────────────────────────────────────────────

def detect_number_type(number: str) -> str:
    number = (number or "").strip().upper()
    if re.match(r'^[A-Z]{4}\d{7}$', number):
        return "container"
    if re.match(r'^[A-Z]{4,}[A-Z0-9]{6,}$', number) and len(number) > 11:
        return "bl"
    return "container" if len(number) <= 11 else "bl"


# ──────────────────────────────────────────────────────────────────────────────
# STEALTH BROWSER SCRIPT
# ──────────────────────────────────────────────────────────────────────────────

STEALTH_SCRIPT = """
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
window.chrome = { runtime: {}, loadTimes: function(){}, csi: function(){}, app: {} };
Object.defineProperty(navigator, 'permissions', {
    get: () => ({ query: (p) => Promise.resolve({ state: p.name === 'notifications' ? 'denied' : 'granted' }) })
});
Object.defineProperty(navigator, 'hardwareConcurrency', { get: () => 8 });
Object.defineProperty(navigator, 'deviceMemory', { get: () => 8 });
Object.defineProperty(screen, 'colorDepth', { get: () => 24 });
"""


# ──────────────────────────────────────────────────────────────────────────────
# CARRIER-SPECIFIC NAVIGATORS
# ──────────────────────────────────────────────────────────────────────────────

def msc_navigate(page: Page, number: str):
    log.info("MSC: Navigating...")
    page.goto(CARRIERS["MSC"], wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(3000)
    try:
        page.get_by_role('button', name='Accept All').click(timeout=5000)
        page.wait_for_timeout(1500)
    except: pass
    page.wait_for_timeout(2000)
    try:
        input_box = page.get_by_role('textbox', name='Enter a Container/Bill of')
        input_box.wait_for(state="visible", timeout=10000)
        input_box.click()
        input_box.fill(number)
    except:
        page.locator('input[type="text"]:visible').first.fill(number)
    try:
        page.locator('button.msc-search-autocomplete__search').wait_for(state="visible", timeout=5000)
        page.locator('button.msc-search-autocomplete__search').click()
    except:
        page.keyboard.press("Enter")
    page.wait_for_timeout(8000)
    try:
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        page.wait_for_timeout(2000)
    except: pass


def maersk_navigate(page: Page, number: str):
    log.info("Maersk: Navigating...")
    page.add_init_script(STEALTH_SCRIPT)
    try:
        page.goto(CARRIERS["Maersk"], wait_until="domcontentloaded", timeout=60000)
    except: log.warning("Maersk: Navigation timeout")
    try:
        page.locator('[data-test="coi-allow-all-button"]').click(timeout=5000)
        page.wait_for_timeout(1000)
    except: pass
    try:
        page.locator('[data-test="finishButton"]').click(timeout=3000)
        page.wait_for_timeout(500)
    except: pass
    page.wait_for_timeout(2000)
    search_box = page.get_by_role('textbox')
    search_box.click()
    page.wait_for_timeout(500)
    search_box.type(number, delay=120)
    page.wait_for_timeout(1000)
    page.locator('[data-test="track-button"]').get_by_role('button', name='Track').click()
    try:
        page.wait_for_selector(
            '[class*="tracking-result"], [class*="shipment-details"], [data-test="tracking-result"]',
            timeout=25000)
    except: log.warning("Maersk: Timeout waiting for results")
    page.wait_for_timeout(3000)


def one_navigate(page: Page, number: str):
    log.info("ONE: Navigating...")
    page.goto(CARRIERS["ONE"], wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(3000)
    try:
        overlay = page.get_by_test_id('tnt-promotion-overlay')
        if overlay.is_visible(timeout=3000):
            overlay.click(); page.wait_for_timeout(1000)
    except: pass
    try:
        skip_btn = page.get_by_role('button', name='Skip')
        if skip_btn.is_visible(timeout=3000):
            skip_btn.click(); page.wait_for_timeout(1000)
    except: pass
    num_type = detect_number_type(number)
    try:
        page.get_by_test_id('tnt-search-dropdown-btn').click()
        page.wait_for_timeout(500)
        if num_type == "container":
            page.get_by_role('option', name='Container No.').click()
        else:
            page.get_by_role('option', name='BL No. or Booking No.').click()
        page.wait_for_timeout(500)
    except Exception as e:
        log.warning(f"ONE: Dropdown failed: {e}")
    try:
        inp = page.get_by_test_id('tnt-search-multiple-input')
        inp.click(); inp.fill(number)
        page.get_by_test_id('tnt-search-multiple-button').click()
    except Exception as e:
        log.error(f"ONE: Search input failed: {e}")
    page.wait_for_timeout(8000)
    try: page.evaluate("window.scrollTo(0, 300)")
    except: pass
    try:
        page.wait_for_selector('.tnt-result-list, .tnt-no-data, .cargo-tracking-result', timeout=20000)
    except: log.warning("ONE: Timeout waiting for results")
    page.wait_for_timeout(3000)


def yangming_navigate(page: Page, number: str):
    log.info("Yang Ming: Navigating...")
    page.goto(CARRIERS["Yang Ming"], wait_until="networkidle")
    try:
        page.get_by_role('button', name='Agree').click(timeout=5000)
    except: pass
    page.wait_for_timeout(2000)
    first_input = page.get_by_role('textbox').first
    first_input.click(); first_input.fill(number)
    page.get_by_role('button', name='Search').click()
    page.wait_for_timeout(5000)


def evergreen_navigate(page: Page, number: str):
    log.info("Evergreen: Navigating...")
    page.goto(CARRIERS["Evergreen"], wait_until="networkidle")
    try:
        page.get_by_text('Accept All', exact=True).click(timeout=5000)
    except: pass
    page.wait_for_timeout(2000)
    num_type = detect_number_type(number)
    if num_type == "container":
        page.get_by_role('radio', name='Container No.').check()
    elif num_type == "bl":
        page.locator('#nav-quick').get_by_text('Bill of Lading No.').click()
    page.wait_for_timeout(500)
    page.locator('#NO').click()
    page.locator('#NO').fill(number)
    page.once('dialog', lambda dialog: dialog.dismiss())
    page.get_by_role('button', name='Submit').click()
    page.wait_for_timeout(5000)


def oocl_navigate(page: Page, number: str):
    log.info("OOCL: Navigating...")
    page.goto(CARRIERS["OOCL"], wait_until="networkidle")
    try:
        page.locator('button:has-text("Accept"), button:has-text("agree")').first.click(timeout=5000)
    except: pass
    page.wait_for_timeout(2000)
    try:
        search_input = page.locator('input[type="text"]:visible, textarea:visible').first
        search_input.fill(number)
        page.locator('button:has-text("Search"), button:has-text("Track"), input[type="submit"]').first.click()
    except Exception as e:
        log.warning(f"OOCL: Navigation failed: {e}")
    page.wait_for_timeout(5000)


def hmm_navigate(page: Page, number: str):
    log.info("HMM: Navigating...")
    page.goto(CARRIERS["HMM"], wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(5000)
    try:
        page.locator('button:has-text("Accept"), button:has-text("agree"), button:has-text("OK")').first.click(timeout=3000)
    except: pass
    page.wait_for_timeout(2000)
    num_type = detect_number_type(number)
    try:
        if num_type == "container":
            page.locator('tr:has-text("Container No."), div:has-text("Container No.")').first.locator('input').first.fill(number)
        else:
            page.locator('tr:has-text("B/L No."), div:has-text("B/L No.")').first.locator('input').first.fill(number)
    except:
        try:
            page.locator('input[placeholder*="B/L"], input[placeholder*="CNTR"]').first.fill(number)
            page.keyboard.press("Enter")
            page.wait_for_timeout(5000)
            return
        except: pass
    try:
        page.get_by_role('button', name='Retrieve').click()
    except:
        page.locator('button:has-text("Retrieve"), input[value="Retrieve"]').first.click()
    page.wait_for_timeout(5000)


def cosco_navigate(page: Page, number: str):
    log.info("COSCO: Navigating...")
    try:
        page.goto(CARRIERS["COSCO"], wait_until="networkidle", timeout=60000)
    except Exception as e:
        log.warning(f"COSCO: Navigation issue: {e}")
    try:
        page.get_by_role("button", name="Allow All").click(timeout=8000)
    except: pass
    tracking_frame = page.frame_locator("#scctCargoTracking")
    try:
        dropdown_trigger = tracking_frame.get_by_text("Booking No.")
        dropdown_trigger.wait_for(state="visible", timeout=15000)
        dropdown_trigger.click()
        tracking_frame.get_by_text("Container No.").wait_for(state="visible", timeout=5000)
        tracking_frame.get_by_text("Container No.").click()
        search_input = tracking_frame.get_by_role("textbox")
        search_input.wait_for(state="visible")
        search_input.fill(number)
        tracking_frame.get_by_role("button", name="Search").click()
    except Exception as e:
        log.error(f"COSCO: Frame interaction failed: {e}")
    page.wait_for_timeout(5000)


def hapaglloyd_navigate(page: Page, number: str):
    log.info("Hapag-Lloyd: Navigating...")
    try:
        page.goto(CARRIERS["Hapag-Lloyd"], wait_until="domcontentloaded", timeout=60000)
    except: log.warning("Hapag-Lloyd: Navigation timeout")
    page.wait_for_timeout(3000)
    try:
        page.locator('button:has-text("Confirm my choices"), button:has-text("Accept all"), button:has-text("I agree")').first.click(timeout=5000)
        page.wait_for_timeout(1000)
    except: pass
    try:
        tab = page.locator('span:has-text("by Container"), a:has-text("by Container")').first
        if tab.is_visible(timeout=3000):
            tab.click(); page.wait_for_timeout(1500)
    except: pass
    try:
        input_field = page.locator('input[maxlength="13"], input[placeholder*="container"], input[placeholder*="Container"]').first
        if input_field.is_visible(timeout=5000):
            input_field.click(); input_field.fill(number)
            page.wait_for_timeout(500)
            page.locator('button:has-text("Find"), button:has-text("Search"), button:has-text("Track")').first.click()
            page.wait_for_timeout(8000)
        else:
            page.locator('input[type="text"]:visible').first.fill(number)
            page.keyboard.press("Enter")
            page.wait_for_timeout(8000)
    except Exception as e:
        log.warning(f"Hapag-Lloyd: Could not fill search: {e}")
    page.wait_for_timeout(3000)


def zim_navigate(page: Page, number: str):
    log.info("ZIM: Navigating...")
    page.goto(CARRIERS["ZIM"], wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(3000)
    try:
        page.get_by_role('button', name='I Agree').click(timeout=5000)
    except: pass
    try:
        search_box = page.get_by_role('textbox', name='shipping tracking')
        search_box.click(timeout=5000)
        search_box.fill("")
        search_box.fill(number)
        page.get_by_role('button', name='Search', exact=True).click()
    except Exception as e:
        log.warning(f"ZIM: Could not fill search: {e}")
    page.wait_for_timeout(8000)

NAV_REGISTRY = {
    "MSC":         msc_navigate,
    "Maersk":      maersk_navigate,
    "ONE":         one_navigate,
    "Yang Ming":   yangming_navigate,
    "Evergreen":   evergreen_navigate,
    "OOCL":        oocl_navigate,
    "HMM":         hmm_navigate,
    "COSCO":       cosco_navigate,
    "Hapag-Lloyd": hapaglloyd_navigate,
    "ZIM":         zim_navigate,
}


# ──────────────────────────────────────────────────────────────────────────────
# STEALTH BYPASSES
# ──────────────────────────────────────────────────────────────────────────────

def hmm_stealth_search(tracking_number: str, save_dir: str = None) -> dict:
    log.info("HMM: Starting Stealth Bypass for %s...", tracking_number)
    final_result = {"error": "HMM stealth bypass failed"}

    def stealth_action(page):
        nonlocal final_result
        try:
            page.set_viewport_size({"width": 1280, "height": 720})
            time.sleep(3)
            try:
                if page.locator('text="Your access is blocked by our firewall"').is_visible(timeout=5000):
                    page.locator('button:has-text("OK")').first.click(); time.sleep(2)
            except: pass
            cntr_input = page.locator('input[name="srchCntrNo1"]')
            cntr_input.wait_for(state="visible", timeout=10000)
            cntr_input.dblclick()
            page.keyboard.press("Control+A"); page.keyboard.press("Delete")
            cntr_input.fill(tracking_number); time.sleep(0.5)
            page.get_by_role('button', name='Retrieve').wait_for(state="visible", timeout=5000)
            page.get_by_role('button', name='Retrieve').click()
            time.sleep(10)
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)"); time.sleep(1)
            final_result = extract_with_vision(page, "HMM", tracking_number, save_dir=save_dir)
        except Exception as e:
            log.error("HMM Stealth Action Error: %s", e)
            final_result = {"error": str(e)}

    def _run():
        nonlocal final_result
        try:
            StealthyFetcher.fetch(
                CARRIERS["HMM"], headless=False, solve_cloudflare=True,
                browser_type="chromium",
                executable_path=r"C:\Program Files\Google\Chrome\Application\chrome.exe",
                block_webrtc=True, hide_canvas=True, network_idle=True,
                google_search=True, wait=5, page_action=stealth_action)
        except Exception as e:
            final_result = {"error": f"Scrapling error: {e}"}

    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=1) as ex:
        try: ex.submit(_run).result(timeout=300)
        except Exception as e: final_result = {"error": f"HMM stealth failed: {e}"}
    return final_result


# def hapaglloyd_stealth_search(tracking_number: str, save_dir: str = None) -> dict:
#     log.info("Hapag-Lloyd: Starting Stealth Bypass...")
#     final_result = {"error": "Search failed or timed out"}

#     def stealth_action(page):
#         nonlocal final_result
#         try:
#             page.set_viewport_size({"width": 1280, "height": 720})
#             time.sleep(2)
#             for sel in ['button:has-text("Confirm my choices")', 'button:has-text("Accept all")',
#                         'button:has-text("Accept All")', 'button:has-text("I agree")']:
#                 btn = page.locator(sel).first
#                 if btn.is_visible():
#                     btn.click(); time.sleep(1.5); break
#             try: page.wait_for_load_state("networkidle", timeout=15000)
#             except: pass
#             time.sleep(3)
#             tab = page.locator('span.bs-sidebar-nav__sublink-label:has-text("by Container")')
#             tab.wait_for(state="visible", timeout=10000); tab.click(); time.sleep(1.5)
#             input_field = page.locator('input.hal-olb-input[maxlength="13"]').first
#             input_field.wait_for(state="visible", timeout=15000)
#             input_field.click(); time.sleep(0.3)
#             page.keyboard.press("Control+A"); page.keyboard.press("Delete")
#             input_field.fill(tracking_number); time.sleep(0.5)
#             page.locator('button.hal-button--primary:has-text("Find")').first.wait_for(state="visible", timeout=5000)
#             page.locator('button.hal-button--primary:has-text("Find")').first.click()
#             time.sleep(8)
#             final_result = extract_with_vision(page, "Hapag-Lloyd", tracking_number, save_dir=save_dir)
#             if "error" not in final_result:
#                 arrival_place = final_result.get("pod")
#                 vessel_name = final_result.get("arrival_vessel")
#                 if arrival_place and vessel_name:
#                     try:
#                         log.info("Hapag-Lloyd: Opening Vessel Tracker for %s → %s",
#                                  vessel_name, arrival_place)

#                         # ── Navigate to Vessel Tracker tab ───────────────────────────────
#                         vessel_tab = page.locator(
#                             'span.bs-sidebar-nav__sublink-label:has-text("Vessel Tracker")'
#                         ).first
#                         vessel_tab.wait_for(state="visible", timeout=8000)
#                         vessel_tab.click()
#                         time.sleep(4)

#                         # ── Type vessel name into ExtJS combo ────────────────────────────
#                         vessel_input = None
#                         for sel in [
#                             'input.inputCombo.sizeGiganticCombo',
#                             'input.inputCombo',
#                             'div.x-form-field-wrap input[type="text"]',
#                             'input[id^="ext-gen"]',
#                         ]:
#                             try:
#                                 loc = page.locator(sel).first
#                                 loc.wait_for(state="visible", timeout=5000)
#                                 vessel_input = loc
#                                 break
#                             except Exception:
#                                 continue

#                         if not vessel_input:
#                             log.warning("Hapag-Lloyd: Vessel input not found")
#                             raise RuntimeError("Vessel input not found")

#                         vessel_input.click()
#                         time.sleep(0.3)
#                         page.keyboard.press("Control+A")
#                         page.keyboard.press("Delete")
#                         vessel_input.type(vessel_name, delay=80)
#                         time.sleep(2)

#                         # ── Select vessel from autocomplete dropdown ──────────────────────
#                         try:
#                             dropdown_row = page.locator(
#                                 f'.x-combo-list-item:has-text("{vessel_name}")'
#                             ).first
#                             dropdown_row.wait_for(state="visible", timeout=8000)
#                             dropdown_row.click()
#                             time.sleep(1)
#                         except Exception:
#                             try:
#                                 page.get_by_text(vessel_name, exact=False).first.click()
#                                 time.sleep(1)
#                             except Exception:
#                                 vessel_input.press("Enter")
#                                 time.sleep(1)

#                         # ── Click Find ────────────────────────────────────────────────────
#                         try:
#                             find_btn = page.locator(
#                                 'button.hal-button--primary:has-text("Find")'
#                             ).first
#                             find_btn.wait_for(state="visible", timeout=5000)
#                             find_btn.click()
#                         except Exception:
#                             page.locator(
#                                 'button:has-text("Find"), input[value="Find"]'
#                             ).first.click()

#                         time.sleep(8)   # wait for schedule table to render

#                         # ── Find the correct ARRIVAL PLACE row ───────────────────────────
#                         arrival_place_upper = arrival_place.strip().upper()
#                         log.info("Hapag-Lloyd: Looking for arrival place row: %s", arrival_place_upper)

#                         row_xpath = (
#                             f'xpath=//tr['
#                             f'  td[normalize-space(.)="{arrival_place_upper}"]'
#                             f'  and ('
#                             f'    td[contains(., "2026")] or td[contains(., "2025")]'
#                             f'    or td[contains(., "Monday")] or td[contains(., "Tuesday")]'
#                             f'    or td[contains(., "Wednesday")] or td[contains(., "Thursday")]'
#                             f'    or td[contains(., "Friday")] or td[contains(., "Saturday")]'
#                             f'    or td[contains(., "Sunday")]'
#                             f'  )'
#                             f']'
#                         )
#                         rows = page.locator(row_xpath)
#                         row_count = rows.count()
#                         log.info("Hapag-Lloyd: Schedule rows matched for '%s': %d",
#                                  arrival_place_upper, row_count)

#                         if row_count == 0:
#                             port_cells = page.locator(
#                                 f'xpath=//td[normalize-space(.)="{arrival_place_upper}"]'
#                             )
#                             cell_count = port_cells.count()
#                             log.info("Hapag-Lloyd: Fallback td matches: %d", cell_count)

#                             if cell_count == 0:
#                                 port_cells = page.locator(f'td:has-text("{arrival_place_upper}")')
#                                 cell_count = port_cells.count()
#                                 log.info("Hapag-Lloyd: CSS partial td matches: %d", cell_count)

#                             if cell_count == 0:
#                                 raise RuntimeError(
#                                     f"No row found for arrival place '{arrival_place_upper}'"
#                                 )

#                             best_idx = 0
#                             for i in range(cell_count):
#                                 try:
#                                     cell_text = port_cells.nth(i).inner_text().strip().upper()
#                                     if cell_text == arrival_place_upper:
#                                         best_idx = i
#                                         break
#                                 except Exception:
#                                     pass

#                             target_cell = port_cells.nth(best_idx)
#                             target_cell.scroll_into_view_if_needed()
#                             target_cell.click()
#                             log.info("Hapag-Lloyd: Clicked port cell (fallback) index %d", best_idx)
#                         else:
#                             port_cell_in_row = rows.first.locator(
#                                 f'xpath=.//td[normalize-space(.)="{arrival_place_upper}"]'
#                             ).first
#                             port_cell_in_row.scroll_into_view_if_needed()
#                             port_cell_in_row.click()
#                             log.info("Hapag-Lloyd: Clicked port cell inside data row")

#                         time.sleep(1.5)

#                         # ── Click Terminal button ─────────────────────────────────────────
#                         terminal_clicked = False
#                         for sel in [
#                             ('role',   'button', 'Terminal'),
#                             ('css',    'button:has-text("Terminal")',    None),
#                             ('css',    'a:has-text("Terminal")',         None),
#                             ('css',    'span.x-btn-text:has-text("Terminal")', None),
#                             ('css',    'td.x-btn-mc:has-text("Terminal")', None),
#                             ('css',    'div.x-btn:has-text("Terminal")', None),
#                             ('css',    '[class*="btn"]:has-text("Terminal")', None),
#                         ]:
#                             try:
#                                 if sel[0] == 'role':
#                                     btn = page.get_by_role(sel[1], name=sel[2]).first
#                                 else:
#                                     btn = page.locator(sel[1]).first
#                                 btn.wait_for(state="visible", timeout=4000)
#                                 btn.scroll_into_view_if_needed()
#                                 btn.click()
#                                 log.info("Hapag-Lloyd: Terminal clicked via %s", sel[1] if sel[0]=='css' else 'role=button')
#                                 terminal_clicked = True
#                                 time.sleep(4)
#                                 break
#                             except Exception:
#                                 continue

#                         if not terminal_clicked:
#                             log.warning("Hapag-Lloyd: Could not click Terminal button")
#                             raise RuntimeError("Terminal button not found")

#                         time.sleep(3)

#                         # ── Read terminal Name + Departure ────────────────────────────
#                         # The terminal table may be inside an iframe (Usabilla) or on
#                         # the main page depending on Hapag's current rendering.
#                         import re as _re
#                         terminal_name = None
#                         terminal_date = None

#                         # Step A: Detect iframe — terminal table lives here
#                         search_ctx = page  # default: main page
#                         for iframe_sel in [
#                             'iframe[title="Usabilla Feedback Button"]',
#                             'iframe[title*="Usabilla"]',
#                             'iframe[title*="usabilla"]',
#                         ]:
#                             try:
#                                 iframe_loc = page.locator(iframe_sel).first
#                                 if iframe_loc.is_visible(timeout=3000):
#                                     search_ctx = iframe_loc.content_frame
#                                     log.info("Hapag-Lloyd: Terminal table found in iframe '%s'", iframe_sel)
#                                     break
#                             except Exception:
#                                 continue

#                         # Step B: Read terminal name + date from the terminal table
#                         # CRITICAL: The page has multiple tables matching
#                         # table[id*='schedules_vessel_tracing']. The vessel schedule
#                         # table comes first (contains vessel names like OAKLAND EXPRESS
#                         # and voyage codes like 620W). The terminal table comes LAST
#                         # and contains rows like: [radio] [ECT DELTA TERMINAL BV / DDE] [2026-09-18]
#                         #
#                         # Strategy: find the LAST table with id containing
#                         # 'schedules_vessel_tracing', then read its tbody tr cells.

#                         for ctx_label, ctx in [("iframe" if search_ctx != page else "main", search_ctx), ("main", page)]:
#                             if terminal_name:
#                                 break

#                             # Approach 1: Get the LAST table by id prefix — that is the terminal table
#                             try:
#                                 all_tables = ctx.locator("table[id*='schedules_vessel_tracing']")
#                                 table_count = all_tables.count()
#                                 log.info("Hapag-Lloyd: [%s] tables with id 'schedules_vessel_tracing': %d",
#                                          ctx_label, table_count)

#                                 if table_count > 0:
#                                     # Use the LAST table — terminal table renders below vessel table
#                                     terminal_tbl = all_tables.nth(table_count - 1)
#                                     # Get spans ONLY from tbody > tr > td (skip thead entirely)
#                                     row_spans = terminal_tbl.locator("tbody > tr > td > span.nonEditableContent")
#                                     span_count = row_spans.count()
#                                     log.info("Hapag-Lloyd: [%s] last table tbody td spans: %d",
#                                              ctx_label, span_count)

#                                     if span_count >= 2:
#                                         terminal_name = row_spans.nth(0).inner_text().strip()
#                                         terminal_date = row_spans.nth(1).inner_text().strip()
#                                         log.info("Hapag-Lloyd: [%s] Raw extracted → name='%s', date='%s'",
#                                                  ctx_label, terminal_name, terminal_date)
#                                     elif span_count == 1:
#                                         terminal_name = row_spans.nth(0).inner_text().strip()
#                             except Exception as ex:
#                                 log.warning("Hapag-Lloyd: [%s] last-table approach failed: %s", ctx_label, ex)

#                             # Approach 2: Find table with Name/Departure headers, use .last
#                             if not terminal_name:
#                                 try:
#                                     tbl = ctx.locator(
#                                         'table:has(th:has-text("Name")):has(th:has-text("Departure"))'
#                                     ).last
#                                     row_spans = tbl.locator("tbody > tr > td > span.nonEditableContent")
#                                     span_count = row_spans.count()
#                                     log.info("Hapag-Lloyd: [%s] Name/Departure table tbody spans: %d",
#                                              ctx_label, span_count)
#                                     if span_count >= 2:
#                                         terminal_name = row_spans.nth(0).inner_text().strip()
#                                         terminal_date = row_spans.nth(1).inner_text().strip()
#                                     elif span_count == 1:
#                                         terminal_name = row_spans.nth(0).inner_text().strip()
#                                 except Exception as ex:
#                                     log.warning("Hapag-Lloyd: [%s] Name/Departure fallback failed: %s",
#                                                 ctx_label, ex)

#                             if ctx == page and search_ctx == page:
#                                 break

#                         # Step D: Write back to final_result
#                         if terminal_name:
#                             final_result["pod_terminal"] = terminal_name
#                             log.info("Hapag-Lloyd: Terminal name → '%s'", terminal_name)
#                         else:
#                             log.warning("Hapag-Lloyd: Could not extract terminal name")

#                         if terminal_date:
#                             m = _re.match(r'(\d{4})-(\d{2})-(\d{2})', terminal_date)
#                             if m:
#                                 terminal_date = f"{m.group(3)}-{m.group(2)}-{m.group(1)}"
#                             if _re.match(r'\d{2}-\d{2}-\d{4}', terminal_date):
#                                 final_result["eta"] = terminal_date
#                                 log.info("Hapag-Lloyd: ETA overwritten → '%s'", terminal_date)
#                             else:
#                                 log.warning("Hapag-Lloyd: Skipping non-date value: '%s'", terminal_date)
#                         else:
#                             log.warning("Hapag-Lloyd: No terminal date found, ETA unchanged")

#                         screenshot_bytes = page.screenshot(full_page=True)
#                         save_screenshot(screenshot_bytes,
#                                         tracking_number + "_terminal",
#                                         "vessel_terminal",
#                                         save_dir=save_dir)

#                     except Exception as e:
#                         log.warning("Hapag-Lloyd: Vessel/terminal tracking failed: %s", e)
#         except Exception as e:
#             log.error("Hapag Stealth Action Error: %s", e)
#             final_result = {"error": str(e)}

#     def _run():
#         nonlocal final_result
#         try:
#             StealthyFetcher.fetch(
#                 CARRIERS["Hapag-Lloyd"], headless=False, solve_cloudflare=True,
#                 browser_type="chromium",
#                 executable_path=r"C:\Program Files\Google\Chrome\Application\chrome.exe",
#                 block_webrtc=True, hide_canvas=True, network_idle=True,
#                 google_search=True, wait=3, page_action=stealth_action)
#         except Exception as e:
#             final_result = {"error": f"Scrapling error: {e}"}

#     from concurrent.futures import ThreadPoolExecutor
#     with ThreadPoolExecutor(max_workers=1) as ex:
#         try: ex.submit(_run).result(timeout=300)
#         except Exception as e: final_result = {"error": f"Hapag stealth failed: {e}"}
#     return final_result


def hapaglloyd_stealth_search(tracking_number: str, save_dir: str = None) -> dict:
    log.info("Hapag-Lloyd: Starting Stealth Bypass...")
    final_result = {"error": "Search failed or timed out"}

    def stealth_action(page):
        nonlocal final_result
        try:
            page.set_viewport_size({"width": 1280, "height": 720})
            time.sleep(2)
            for sel in ['button:has-text("Confirm my choices")', 'button:has-text("Accept all")',
                        'button:has-text("Accept All")', 'button:has-text("I agree")']:
                btn = page.locator(sel).first
                if btn.is_visible():
                    btn.click(); time.sleep(1.5); break
            try: page.wait_for_load_state("networkidle", timeout=15000)
            except: pass
            time.sleep(3)
            tab = page.locator('span.bs-sidebar-nav__sublink-label:has-text("by Container")')
            tab.wait_for(state="visible", timeout=10000); tab.click(); time.sleep(1.5)
            input_field = page.locator('input.hal-olb-input[maxlength="13"]').first
            input_field.wait_for(state="visible", timeout=15000)
            input_field.click(); time.sleep(0.3)
            page.keyboard.press("Control+A"); page.keyboard.press("Delete")
            input_field.fill(tracking_number); time.sleep(0.5)
            page.locator('button.hal-button--primary:has-text("Find")').first.wait_for(state="visible", timeout=5000)
            page.locator('button.hal-button--primary:has-text("Find")').first.click()
            time.sleep(8)
            final_result = extract_with_vision(page, "Hapag-Lloyd", tracking_number, save_dir=save_dir)
            if "error" not in final_result:
                arrival_place = final_result.get("pod")
                vessel_name = final_result.get("arrival_vessel")
                if arrival_place and vessel_name:
                    try:
                        log.info("Hapag-Lloyd: Opening Vessel Tracker for %s → %s",
                                 vessel_name, arrival_place)

                        # ── Navigate to Vessel Tracker tab ───────────────────────────────
                        vessel_tab = page.locator(
                            'span.bs-sidebar-nav__sublink-label:has-text("Vessel Tracker")'
                        ).first
                        vessel_tab.wait_for(state="visible", timeout=8000)
                        vessel_tab.click()
                        time.sleep(4)

                        # ── Type vessel name into ExtJS combo ────────────────────────────
                        vessel_input = None
                        for sel in [
                            'input.inputCombo.sizeGiganticCombo',
                            'input.inputCombo',
                            'div.x-form-field-wrap input[type="text"]',
                            'input[id^="ext-gen"]',
                        ]:
                            try:
                                loc = page.locator(sel).first
                                loc.wait_for(state="visible", timeout=5000)
                                vessel_input = loc
                                break
                            except Exception:
                                continue

                        if not vessel_input:
                            log.warning("Hapag-Lloyd: Vessel input not found")
                            raise RuntimeError("Vessel input not found")

                        vessel_input.click()
                        time.sleep(0.3)
                        page.keyboard.press("Control+A")
                        page.keyboard.press("Delete")
                        vessel_input.type(vessel_name, delay=80)
                        time.sleep(2)

                        # ── Select vessel from autocomplete dropdown ──────────────────────
                        try:
                            dropdown_row = page.locator(
                                f'.x-combo-list-item:has-text("{vessel_name}")'
                            ).first
                            dropdown_row.wait_for(state="visible", timeout=8000)
                            dropdown_row.click()
                            time.sleep(1)
                        except Exception:
                            try:
                                page.get_by_text(vessel_name, exact=False).first.click()
                                time.sleep(1)
                            except Exception:
                                vessel_input.press("Enter")
                                time.sleep(1)

                        # ── Click Find ────────────────────────────────────────────────────
                        try:
                            find_btn = page.locator(
                                'button.hal-button--primary:has-text("Find")'
                            ).first
                            find_btn.wait_for(state="visible", timeout=5000)
                            find_btn.click()
                        except Exception:
                            page.locator(
                                'button:has-text("Find"), input[value="Find"]'
                            ).first.click()

                        time.sleep(8)   # wait for schedule table to render

                        # ── Find the correct ARRIVAL PLACE row ───────────────────────────
                        arrival_place_upper = arrival_place.strip().upper()
                        log.info("Hapag-Lloyd: Looking for arrival place row: %s", arrival_place_upper)

                        row_xpath = (
                            f'xpath=//tr['
                            f'  td[normalize-space(.)="{arrival_place_upper}"]'
                            f'  and ('
                            f'    td[contains(., "2026")] or td[contains(., "2025")]'
                            f'    or td[contains(., "Monday")] or td[contains(., "Tuesday")]'
                            f'    or td[contains(., "Wednesday")] or td[contains(., "Thursday")]'
                            f'    or td[contains(., "Friday")] or td[contains(., "Saturday")]'
                            f'    or td[contains(., "Sunday")]'
                            f'  )'
                            f']'
                        )
                        rows = page.locator(row_xpath)
                        row_count = rows.count()
                        log.info("Hapag-Lloyd: Schedule rows matched for '%s': %d",
                                 arrival_place_upper, row_count)

                        if row_count == 0:
                            port_cells = page.locator(
                                f'xpath=//td[normalize-space(.)="{arrival_place_upper}"]'
                            )
                            cell_count = port_cells.count()
                            log.info("Hapag-Lloyd: Fallback td matches: %d", cell_count)

                            if cell_count == 0:
                                port_cells = page.locator(f'td:has-text("{arrival_place_upper}")')
                                cell_count = port_cells.count()
                                log.info("Hapag-Lloyd: CSS partial td matches: %d", cell_count)

                            if cell_count == 0:
                                raise RuntimeError(
                                    f"No row found for arrival place '{arrival_place_upper}'"
                                )

                            best_idx = 0
                            for i in range(cell_count):
                                try:
                                    cell_text = port_cells.nth(i).inner_text().strip().upper()
                                    if cell_text == arrival_place_upper:
                                        best_idx = i
                                        break
                                except Exception:
                                    pass

                            target_cell = port_cells.nth(best_idx)
                            target_cell.scroll_into_view_if_needed()
                            target_cell.click()
                            log.info("Hapag-Lloyd: Clicked port cell (fallback) index %d", best_idx)
                        else:
                            port_cell_in_row = rows.first.locator(
                                f'xpath=.//td[normalize-space(.)="{arrival_place_upper}"]'
                            ).first
                            port_cell_in_row.scroll_into_view_if_needed()
                            port_cell_in_row.click()
                            log.info("Hapag-Lloyd: Clicked port cell inside data row")

                        time.sleep(1.5)

                        # ── Click Terminal button ─────────────────────────────────────────
                        terminal_clicked = False
                        for sel in [
                            ('role',   'button', 'Terminal'),
                            ('css',    'button:has-text("Terminal")',    None),
                            ('css',    'a:has-text("Terminal")',         None),
                            ('css',    'span.x-btn-text:has-text("Terminal")', None),
                            ('css',    'td.x-btn-mc:has-text("Terminal")', None),
                            ('css',    'div.x-btn:has-text("Terminal")', None),
                            ('css',    '[class*="btn"]:has-text("Terminal")', None),
                        ]:
                            try:
                                if sel[0] == 'role':
                                    btn = page.get_by_role(sel[1], name=sel[2]).first
                                else:
                                    btn = page.locator(sel[1]).first
                                btn.wait_for(state="visible", timeout=4000)
                                btn.scroll_into_view_if_needed()
                                btn.click()
                                log.info("Hapag-Lloyd: Terminal clicked via %s", sel[1] if sel[0]=='css' else 'role=button')
                                terminal_clicked = True
                                time.sleep(4)
                                break
                            except Exception:
                                continue

                        if not terminal_clicked:
                            log.warning("Hapag-Lloyd: Could not click Terminal button")
                            raise RuntimeError("Terminal button not found")

                        time.sleep(3)

                        # ── Read terminal Name + Departure ────────────────────────────
                        # The terminal table may be inside an iframe (Usabilla) or on
                        # the main page depending on Hapag's current rendering.
                        import re as _re
                        terminal_name = None
                        terminal_date = None

                        # Step A: Detect iframe — terminal table lives here
                        search_ctx = page  # default: main page
                        for iframe_sel in [
                            'iframe[title="Usabilla Feedback Button"]',
                            'iframe[title*="Usabilla"]',
                            'iframe[title*="usabilla"]',
                        ]:
                            try:
                                iframe_loc = page.locator(iframe_sel).first
                                if iframe_loc.is_visible(timeout=3000):
                                    search_ctx = iframe_loc.content_frame
                                    log.info("Hapag-Lloyd: Terminal table found in iframe '%s'", iframe_sel)
                                    break
                            except Exception:
                                continue

                        # Step B: Read terminal name + date from the terminal table
                        # CRITICAL: The page has multiple tables matching
                        # table[id*='schedules_vessel_tracing']. The vessel schedule
                        # table comes first (contains vessel names like OAKLAND EXPRESS
                        # and voyage codes like 620W). The terminal table comes LAST
                        # and contains rows like: [radio] [ECT DELTA TERMINAL BV / DDE] [2026-09-18]
                        #
                        # Strategy: find the LAST table with id containing
                        # 'schedules_vessel_tracing', then read its tbody tr cells.

                        for ctx_label, ctx in [("iframe" if search_ctx != page else "main", search_ctx), ("main", page)]:
                            if terminal_name:
                                break

                            # Approach 1: Get the LAST table by id prefix — that is the terminal table
                            try:
                                all_tables = ctx.locator("table[id*='schedules_vessel_tracing']")
                                table_count = all_tables.count()
                                log.info("Hapag-Lloyd: [%s] tables with id 'schedules_vessel_tracing': %d",
                                         ctx_label, table_count)

                                if table_count > 0:
                                    # Use the LAST table — terminal table renders below vessel table
                                    terminal_tbl = all_tables.nth(table_count - 1)
                                    # Get spans ONLY from tbody > tr > td (skip thead entirely)
                                    row_spans = terminal_tbl.locator("tbody > tr > td > span.nonEditableContent")
                                    span_count = row_spans.count()
                                    log.info("Hapag-Lloyd: [%s] last table tbody td spans: %d",
                                             ctx_label, span_count)

                                    if span_count >= 2:
                                        terminal_name = row_spans.nth(0).inner_text().strip()
                                        terminal_date = row_spans.nth(1).inner_text().strip()
                                        log.info("Hapag-Lloyd: [%s] Raw extracted → name='%s', date='%s'",
                                                 ctx_label, terminal_name, terminal_date)
                                    elif span_count == 1:
                                        terminal_name = row_spans.nth(0).inner_text().strip()
                            except Exception as ex:
                                log.warning("Hapag-Lloyd: [%s] last-table approach failed: %s", ctx_label, ex)

                            # Approach 2: Find table with Name/Departure headers, use .last
                            if not terminal_name:
                                try:
                                    tbl = ctx.locator(
                                        'table:has(th:has-text("Name")):has(th:has-text("Departure"))'
                                    ).last
                                    row_spans = tbl.locator("tbody > tr > td > span.nonEditableContent")
                                    span_count = row_spans.count()
                                    log.info("Hapag-Lloyd: [%s] Name/Departure table tbody spans: %d",
                                             ctx_label, span_count)
                                    if span_count >= 2:
                                        terminal_name = row_spans.nth(0).inner_text().strip()
                                        terminal_date = row_spans.nth(1).inner_text().strip()
                                    elif span_count == 1:
                                        terminal_name = row_spans.nth(0).inner_text().strip()
                                except Exception as ex:
                                    log.warning("Hapag-Lloyd: [%s] Name/Departure fallback failed: %s",
                                                ctx_label, ex)

                            if ctx == page and search_ctx == page:
                                break

                        # Step D: Write back to final_result
                        if terminal_name:
                            final_result["pod_terminal"] = terminal_name
                            log.info("Hapag-Lloyd: Terminal name → '%s'", terminal_name)
                        else:
                            log.warning("Hapag-Lloyd: Could not extract terminal name")

                        if terminal_date:
                            m = _re.match(r'(\d{4})-(\d{2})-(\d{2})', terminal_date)
                            if m:
                                terminal_date = f"{m.group(3)}-{m.group(2)}-{m.group(1)}"
                            if _re.match(r'\d{2}-\d{2}-\d{4}', terminal_date):
                                final_result["eta"] = terminal_date
                                log.info("Hapag-Lloyd: ETA overwritten → '%s'", terminal_date)
                            else:
                                log.warning("Hapag-Lloyd: Skipping non-date value: '%s'", terminal_date)
                        else:
                            log.warning("Hapag-Lloyd: No terminal date found, ETA unchanged")

                        screenshot_bytes = page.screenshot(full_page=True)
                        save_screenshot(screenshot_bytes,
                                        tracking_number + "_terminal",
                                        "vessel_terminal",
                                        save_dir=save_dir)

                    except Exception as e:
                        log.warning("Hapag-Lloyd: Vessel/terminal tracking failed: %s", e)
        except Exception as e:
            log.error("Hapag Stealth Action Error: %s", e)
            final_result = {"error": str(e)}

    def _run():
        nonlocal final_result
        try:
            StealthyFetcher.fetch(
                CARRIERS["Hapag-Lloyd"], headless=False, solve_cloudflare=True,
                browser_type="chromium",
                executable_path=r"C:\Program Files\Google\Chrome\Application\chrome.exe",
                block_webrtc=True, hide_canvas=True, network_idle=True,
                google_search=True, wait=3, page_action=stealth_action)
        except Exception as e:
            final_result = {"error": f"Scrapling error: {e}"}

    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=1) as ex:
        try: ex.submit(_run).result(timeout=300)
        except Exception as e: final_result = {"error": f"Hapag stealth failed: {e}"}
    return final_result

# ──────────────────────────────────────────────────────────────────────────────
# VISION EXTRACTION
# ──────────────────────────────────────────────────────────────────────────────

VISION_PROMPT = """You are a logistics data extraction AI. Analyze this container tracking screenshot and extract structured shipment data.

IMPORTANT RULES:

1. PORT OF LOADING (POL): Find where the container was FIRST loaded onto a vessel.
   - Look for events like "Export Loaded on Vessel", "Loaded", "Shipped Onboard", "Departure"
   - The PORT on that row = POL
   - The DATE on that row = ETD
   - The VESSEL on that row = loaded_vessel. Separate vessel name and voyage number.
   - The VOYAGE NUMBER on that row = loaded_voyage

2. PORT OF DISCHARGE (POD): Find the FINAL OCEAN destination port.
   - Ignore inland delivery legs that use "Waterway", "Barge", "Truck", or "Rail".
   - Look for the last "Estimated Time of Arrival", "Arrival", or "Discharged" event at an OCEAN PORT.
   - The PORT on that row = POD
   - The DATE on that row = ETA
   - The VESSEL on that row = arrival_vessel. Separate vessel and voyage. Ensure the vessel name is the OCEAN vessel (e.g. "MSC MUGE", not "Waterway").
   - The VOYAGE NUMBER on that row = arrival_voyage

3. CARRIER ON-CARRIAGE (INLAND TRACKING):
   - If there are inland delivery legs ("Waterway", "Truck", "Rail") occurring AFTER the final ocean POD arrival, capture them in the carrier_on_carriage array.
   - Extract the arrival_place (e.g. KAMPEN) and arrival_date.

3. TRANSSHIPMENT DETECTION (CRITICAL):
   Compare loaded_vessel and arrival_vessel.
   - If loaded_vessel == arrival_vessel → direct = true, transshipments = []
   - If loaded_vessel != arrival_vessel → direct = false, AND you MUST populate transshipments.

   HOW TO FIND TRANSSHIPMENT DATA:
   Scan all_events between the POL loading event and the POD arrival event.
   Any port that is NOT the POL and NOT the POD is a transshipment port.

   For EACH transshipment port, look for TWO types of events at that port:
     a) ARRIVAL event ("Discharged", "Arrival", "Unloaded") → this gives the transshipment ETA
     b) DEPARTURE event ("Loaded", "Departure", "Shipped") → this gives the transshipment ETD

   Extract for each transshipment stop:
     - port: Full port name (e.g. "Antwerp, Belgium")
     - terminal: Terminal name if visible, otherwise null
     - eta: Date the container ARRIVED at this transshipment port (from discharge/arrival event)
     - etd: Date the container DEPARTED from this transshipment port (from loaded/departure event)
     - vessel_in: The vessel that BROUGHT the container TO this port (= the previous leg's vessel)
     - vessel_out: The vessel that TAKES the container FROM this port (= the next leg's vessel)
     - voyage_in: Voyage number of vessel_in, or null
     - voyage_out: Voyage number of vessel_out, or null

   RULE: If loaded_vessel != arrival_vessel but transshipments array is empty,
   you have MISSED the transshipment port. Go back and scan all_events again.
   There MUST be at least one intermediate port where the vessel changed.

   RULE: If you can see a date for discharge/arrival at the transshipment port,
   you MUST include it as eta. If you can see a date for loading/departure,
   you MUST include it as etd. Do NOT return null for dates that are visible
   in the tracking events.

   RULE: If only one date is visible for a transshipment port, use it for
   whichever event it corresponds to (eta or etd) and set the other to null.

5. STATUS: "Delivered", "Discharged", "In Transit", "Loaded", or "Booked"

6. ALL EVENTS: Extract EVERY row from the tracking table as all_events, oldest first.
   Each event must have: date, location, event, vessel, voyage, terminal.
   These events are the SOURCE for all other fields. Extract them FIRST,
   then derive POL, POD, transshipments, and carrier_on_carriage from them.

CRITICAL DATE FORMAT: ALL DATES must be returned strictly in DD-MM-YYYY format (e.g., 01-06-2026).

Return ONLY valid JSON (no markdown, no code fences):
{
  "carrier": "Carrier Name",
  "tracking_number": "Container/BL Number",
  "status": "In Transit",
  "container_type": "20' DRY VAN or null",
  "pol": "Port of Loading name",
  "pol_terminal": "Terminal at POL or null",
  "etd": "DD-MM-YYYY",
  "loaded_vessel": "Vessel name at loading",
  "loaded_voyage": "Voyage number at loading or null",
  "pod": "Port of Discharge name",
  "pod_terminal": "Terminal at POD or null",
  "eta": "DD-MM-YYYY",
  "arrival_vessel": "Vessel name for arrival",
  "arrival_voyage": "Voyage number for arrival or null",
  "direct": true or false,
  "transshipments": [
    {
      "port": "Transshipment port name",
      "terminal": "Terminal name or null",
      "eta": "DD-MM-YYYY or null",
      "etd": "DD-MM-YYYY or null",
      "vessel_in": "Vessel that arrived here",
      "vessel_out": "Vessel that departed here",
      "voyage_in": "Voyage of vessel_in or null",
      "voyage_out": "Voyage of vessel_out or null"
    }
  ],
  "carrier_on_carriage": [
    {
      "arrival_place": "Inland arrival place",
      "arrival_date": "DD-MM-YYYY"
    }
  ],
  "all_events": [
    {"date": "DD-MM-YYYY", "location": "Port name", "event": "Event description", "vessel": "Vessel name", "voyage": "Voyage number", "terminal": "Terminal name"}
  ]
}

VALIDATION BEFORE RETURNING:
- If loaded_vessel != arrival_vessel AND transshipments is empty → ERROR. Find the transshipment port.
- If transshipments has entries but all eta/etd are null → scan all_events for dates at those ports.
- Dates should be strictly DD-MM-YYYY format.

If the page shows an error or no results, return: {"error": "No tracking results found"}"""


_gemini_client = None

def get_gemini_client():
    global _gemini_client
    if _gemini_client:
        return _gemini_client
    if not GEMINI_API_KEY:
        log.error("GEMINI_API_KEY not found.")
        return None
    try:
        _gemini_client = genai.Client(api_key=GEMINI_API_KEY)
        return _gemini_client
    except Exception as e:
        log.error("Failed to init Gemini: %s", e)
        return None


def extract_with_vision(page: Page, carrier: str, number: str,
                        save_dir: str = None) -> dict:
    """
    Screenshot → Gemini Vision → structured tracking JSON.
    Saves {number}_tracking.png in save_dir (or SCREENSHOTS_DIR).
    """
    client = get_gemini_client()
    if not client:
        return {"error": "AI client not ready — check GEMINI_API_KEY"}

    screenshot_bytes = page.screenshot(full_page=True)
    target_dir = save_dir or SCREENSHOTS_DIR
    save_screenshot(screenshot_bytes, number, "tracking", target_dir)

    log.info("Extracting tracking data via Vision AI...")
    try:
        contents = [
            types.Part.from_bytes(data=screenshot_bytes, mime_type="image/png"),
            VISION_PROMPT,
        ]
        response = client.models.generate_content(
            model=GEMINI_MODEL_SMART,
            contents=contents,
            config=types.GenerateContentConfig(temperature=0.1, max_output_tokens=4096),
        )
        raw = response.text.strip()
        clean = raw.replace("```json", "").replace("```", "").strip()

        try:
            data = json.loads(clean)
        except json.JSONDecodeError:
            log.warning("JSON malformed, attempting auto-repair...")
            data = json.loads(repair_json(clean))

        # Ensure required fields
        data.setdefault("carrier", carrier)
        data.setdefault("tracking_number", number)
        if not data.get("carrier") or data["carrier"] in ("Carrier Name", ""):
            data["carrier"] = carrier
        if not data.get("tracking_number") or data["tracking_number"] in ("Container/BL Number", ""):
            data["tracking_number"] = number

        for field in ["status", "pol", "pod", "eta", "etd", "loaded_vessel",
                      "arrival_vessel", "container_type", "pol_terminal", "pod_terminal",
                      "loaded_voyage", "arrival_voyage"]:
            data.setdefault(field, None)
        for field in ["transshipments", "all_events"]:
            data.setdefault(field, [])
        if data.get("direct") is None:
            data["direct"] = len(data.get("transshipments", [])) == 0

        return data

    except Exception as e:
        log.error("Vision extraction failed: %s", e)
        import traceback; traceback.print_exc()
        return {"error": f"Vision extraction failed: {str(e)}"}


# ──────────────────────────────────────────────────────────────────────────────
# CORE TRACKING FUNCTION
# ──────────────────────────────────────────────────────────────────────────────

def track_on_page(page: Page, carrier_name: str, tracking_number: str,
                  save_dir: str = None) -> dict:
    """
    Perform tracking on an EXISTING Playwright page.
    carrier_name: short code (e.g. "HAPAG") or full name (auto-resolved).
    """
    # Resolve to short code if needed, then to carrier key
    code = find_carrier_code(carrier_name) or carrier_name.upper()
    carrier_key = CODE_TO_CARRIER_KEY.get(code)
    if not carrier_key:
        # Try direct match against CARRIERS keys
        carrier_key = next((k for k in CARRIERS if k.upper() == carrier_name.upper()), None)
    if not carrier_key:
        return {"error": f"Carrier '{carrier_name}' not supported. Known codes: {list(CODE_TO_CARRIER_KEY)}"}

    url = CARRIERS.get(carrier_key)
    nav_func = NAV_REGISTRY.get(carrier_key)
    if not nav_func:
        return {"error": f"Navigation not implemented for '{carrier_key}'"}

    tracking_number = (tracking_number or "").strip().upper()
    max_attempts = 3
    last_error = None

    for attempt in range(1, max_attempts + 1):
        try:
            log.info("Tracking attempt %d/%d — %s / %s", attempt, max_attempts,
                     carrier_key, tracking_number)

            # Special paths that always use stealth
            if carrier_key == "Hapag-Lloyd":
                result = hapaglloyd_stealth_search(tracking_number, save_dir=save_dir)
                if result and "error" not in result:
                    save_result_json(tracking_number, result, save_dir)
                    return result
                last_error = result.get("error") if result else "Stealth failed"
                continue

            if carrier_key == "HMM":
                result = hmm_stealth_search(tracking_number, save_dir=save_dir)
                if result and "error" not in result:
                    save_result_json(tracking_number, result, save_dir)
                    return result
                last_error = result.get("error") if result else "Stealth failed"
                continue

            # Standard flow
            nav_func(page, tracking_number)
            page.wait_for_timeout(3000)
            if page.is_closed():
                return {"error": "Browser page closed during tracking"}

            result = extract_with_vision(page, carrier_key, tracking_number, save_dir=save_dir)
            if "error" not in result:
                save_result_json(tracking_number, result, save_dir)
                return result

            last_error = result.get("error")
            log.warning("Attempt %d failed: %s", attempt, last_error)

        except Exception as e:
            if "Target page, context or browser has been closed" in str(e):
                return {"error": "Browser closed unexpectedly"}
            log.error("Tracking attempt %d error: %s", attempt, e)
            last_error = str(e)

        if attempt < max_attempts:
            page.wait_for_timeout(5000 * attempt)
            try: page.goto(url, timeout=30000)
            except: pass

    return {"error": f"Tracking failed after {max_attempts} attempts: {last_error}"}


def track_shipment_thread_safe(carrier_code: str, container_no: str,
                               folder_to_save: str, log_cb=None) -> dict:
    """
    THREAD-SAFE entry point. Spawns its own Playwright instance.
    Saves {container_no}_tracking.png + {container_no}_result.json into folder_to_save.
    """
    if log_cb:
        log_cb(f"[TRACKING] Starting {carrier_code} / {container_no}")

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=False,
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox"])
        context = browser.new_context(
            user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/124.0.0.0 Safari/537.36"),
            viewport={"width": 1280, "height": 800},
            locale="en-US", timezone_id="America/New_York")
        context.add_init_script(STEALTH_SCRIPT)
        page = context.new_page()
        try:
            result = track_on_page(page, carrier_code, container_no,
                                   save_dir=folder_to_save)
            if log_cb:
                if "error" not in result:
                    log_cb(f"[TRACKING] Done: {container_no} → {folder_to_save}")
                else:
                    log_cb(f"[TRACKING] Failed: {container_no} — {result['error']}")
            return result
        except Exception as e:
            if log_cb: log_cb(f"[TRACKING] Exception: {e}")
            return {"error": str(e)}
        finally:
            browser.close()
            if log_cb: log_cb(f"[TRACKING] Session closed: {container_no}")


# ──────────────────────────────────────────────────────────────────────────────
# HIGH-LEVEL FOLDER TRACKING
# Called by worker.py after extraction of an OI folder
# ──────────────────────────────────────────────────────────────────────────────

def run_tracking_for_folder(folder_name: str, shipments_dir: str,
                            add_log_fn=None) -> dict:
    """
    After extraction, load the JSON files from Shipments/<folder_name>/json/,
    find the MBL carrier, and track all HBL containers.

    Saves results to Shipments/<folder_name>/tracking/

    Returns a summary dict: {container_no: result_or_error}
    """
    def _log(msg):
        log.info(msg)
        if add_log_fn:
            add_log_fn("TRACKING", msg)

    folder_path = Path(shipments_dir) / folder_name
    json_path   = folder_path / "json"
    tracking_path = folder_path / "tracking"
    tracking_path.mkdir(parents=True, exist_ok=True)

    if not json_path.exists():
        _log(f"{folder_name}: No json/ folder, skipping tracking")
        return {}

    # ── Load all extracted JSONs ──────────────────────────────────────────────
    mbl_data = None
    hbl_data = None

    for jf in json_path.glob("*.json"):
        try:
            with open(jf) as f:
                d = json.load(f)
            doc_type = (d.get("document_type") or "").upper()
            if "MASTER" in doc_type and mbl_data is None:
                mbl_data = d
            elif "HOUSE" in doc_type and hbl_data is None:
                hbl_data = d
        except Exception as e:
            _log(f"Could not read {jf.name}: {e}")

    # ── Resolve carrier from MBL ──────────────────────────────────────────────
    carrier_raw = None
    if mbl_data:
        carrier_raw = mbl_data.get("carrier_name") or mbl_data.get("carrier_code")

    if not carrier_raw:
        _log(f"{folder_name}: No MBL carrier found, skipping tracking")
        return {}

    carrier_code = find_carrier_code(carrier_raw)
    if not carrier_code:
        _log(f"{folder_name}: Cannot map carrier '{carrier_raw}' to a known code")
        return {}

    _log(f"{folder_name}: MBL carrier resolved → '{carrier_code}' (from '{carrier_raw}')")

    # ── Get containers from HBL ───────────────────────────────────────────────
    containers_to_track = []
    source = hbl_data or mbl_data  # prefer HBL containers; fall back to MBL
    if source:
        for c in source.get("containers", []):
            cno = (c.get("container_no") or "").strip().upper()
            # Relaxed regex: 4 letters + 6 or 7 digits (fixes 10-digit skips)
            if cno and re.match(r'^[A-Z]{4}\d{6,7}$', cno):
                containers_to_track.append(cno)

    if not containers_to_track:
        _log(f"{folder_name}: No valid container numbers found, skipping tracking")
        return {}

    _log(f"{folder_name}: Tracking {len(containers_to_track)} container(s) IN PARALLEL: {containers_to_track}")

    # ── Track each container (Multi-threaded) ─────────────────────────────────
    results = {}
    # Use max 3 threads to avoid too many browser instances at once
    with ThreadPoolExecutor(max_workers=3) as executor:
        future_to_cno = {
            executor.submit(
                track_shipment_thread_safe,
                carrier_code=carrier_code,
                container_no=cno,
                folder_to_save=str(tracking_path),
                log_cb=_log
            ): cno for cno in containers_to_track
        }

        for future in as_completed(future_to_cno):
            cno = future_to_cno[future]
            try:
                result = future.result()
                results[cno] = result
                status = result.get("status") or result.get("error", "unknown")
                _log(f"{folder_name}: {cno} → {status}")
            except Exception as e:
                _log(f"{folder_name}: {cno} tracking exception: {e}")
                results[cno] = {"error": str(e)}

    return results


# ──────────────────────────────────────────────────────────────────────────────
# STANDALONE CLI TEST
# python Tracking.py HAPAG TCKU3530010
# ──────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import sys
    carrier = sys.argv[1] if len(sys.argv) > 1 else "Maersk"
    number  = sys.argv[2] if len(sys.argv) > 2 else "MRSU0402006"
    result = track_shipment_thread_safe(carrier, number, SCREENSHOTS_DIR)
    print(json.dumps(result, indent=2))
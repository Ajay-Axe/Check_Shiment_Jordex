
"""
Login.py — Jordex Authentication & Dashboard Filter Engine
==========================================================
Merged from: Login.py + CheckShipment.py

Responsibilities:
  1. Launch persistent Chromium context (session reuse ~90% of runs)
  2. Handle Auth0 → Azure AD → Microsoft login flow
  3. Handle MFA (authenticator approval)
  4. Apply dashboard filters (Backoffice import, Check shipment)
  5. Provide apply_date_filter() for custom date ranges

Session persists via launchPersistentContext.
Credentials: JORDEX_EMAIL / JORDEX_PASSWORD in .env
"""

import os
import sys
import logging
from pathlib import Path
from datetime import datetime
import threading
from playwright.sync_api import (
    sync_playwright,
    TimeoutError as PlaywrightTimeout,
    BrowserContext,
    Page,
)

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# ═══════════════════════════════════════════════════════════════════════
#  CONFIGURATION
# ═══════════════════════════════════════════════════════════════════════

DASHBOARD_URL = os.getenv("DASHBOARD_URL", "https://jit.jordex.com")
EMAIL         = os.getenv("JORDEX_EMAIL", "")
PASSWORD      = os.getenv("JORDEX_PASSWORD", "")
SESSION_DIR   = os.getenv("SESSION_DIR", "./session_data")

MFA_TIMEOUT = int(os.getenv("MFA_TIMEOUT", "120")) * 1000
NAV_TIMEOUT = int(os.getenv("NAVIGATION_TIMEOUT", "30")) * 1000
EL_TIMEOUT  = int(os.getenv("ELEMENT_TIMEOUT", "10")) * 1000

HEADLESS = os.getenv("HEADLESS", "false").lower() == "true"
SLOW_MO  = int(os.getenv("SLOW_MO", "0"))

# ═══════════════════════════════════════════════════════════════════════
#  LOGGING
# ═══════════════════════════════════════════════════════════════════════

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("login")

# ═══════════════════════════════════════════════════════════════════════
#  MFA STATE (shared with UI via API)
# ═══════════════════════════════════════════════════════════════════════

mfa_state = {
    "active": False,
    "number": None,
    "screenshot_b64": None,
}


def get_mfa_state() -> dict:
    return dict(mfa_state)


# ═══════════════════════════════════════════════════════════════════════
#  UTILITY HELPERS
# ═══════════════════════════════════════════════════════════════════════

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


def _visible(page: Page, selector: str, timeout: int = 3000) -> bool:
    try:
        page.locator(selector).first.wait_for(state="visible", timeout=timeout)
        return True
    except PlaywrightTimeout:
        return False


def _click_if_visible(page: Page, selector: str, timeout: int = 3000) -> bool:
    if _visible(page, selector, timeout):
        page.locator(selector).first.click()
        return True
    return False


def _is_on_app(page: Page) -> bool:
    url = page.url.lower()
    on_jordex = "jit.jordex.com" in url
    on_auth = any(x in url for x in [
        "auth.myfreight.nl", "login.microsoftonline", "login.live.com"
    ])
    return on_jordex and not on_auth


def _is_on_auth0(page: Page) -> bool:
    return "auth.myfreight.nl" in page.url.lower()


def _is_on_microsoft(page: Page) -> bool:
    url = page.url.lower()
    return any(x in url for x in ["login.microsoftonline", "login.live.com"])


def _has_error(page: Page) -> str | None:
    error_selectors = [
        '#usernameError', '#passwordError', '#errorText',
        '.alert-error', '#error_description', 'div[role="alert"]',
    ]
    for sel in error_selectors:
        if _visible(page, sel, timeout=1500):
            text = page.locator(sel).first.inner_text().strip()
            if text:
                return text
    return None


def _wait_for_dashboard_ready(page: Page, timeout: int = 20000) -> bool:
    log.info("    Checking for dashboard content...")
    markers = [
        'text=Shipments', 'th:has-text("Shipment")', 'th:has-text("Origin")',
        '.shipment-list', 'table', 'nav', 'button:has-text("Filters")',
    ]
    try:
        page.locator(" , ".join(markers)).first.wait_for(state="visible", timeout=timeout)
        log.info("    Dashboard content detected")
        return True
    except PlaywrightTimeout:
        log.warning("    Timeout waiting for dashboard content")
        return False


# ═══════════════════════════════════════════════════════════════════════
#  AUTH0: "Continue with Azure"
# ═══════════════════════════════════════════════════════════════════════

def handle_auth0(page: Page) -> None:
    log.info("AUTH0 page detected (auth.myfreight.nl)")
    page.wait_for_load_state("networkidle")
    page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
    page.wait_for_timeout(1000)

    azure_selectors = [
        'button:has-text("Continue with Azure")',
        'a:has-text("Continue with Azure")',
        '[data-provider="windowslive"]',
        '[data-provider="waad"]',
        'button:has-text("Azure")',
        'a:has-text("Azure")',
    ]

    clicked = False
    for sel in azure_selectors:
        if _click_if_visible(page, sel, timeout=EL_TIMEOUT):
            log.info("  Clicked 'Continue with Azure'")
            clicked = True
            break

    if not clicked:
        log.error("  'Continue with Azure' button not found")
        page.pause()
        return

    log.info("  Waiting for Microsoft redirect...")
    try:
        page.wait_for_url(
            lambda url: "login.microsoftonline" in url or "login.live" in url,
            timeout=NAV_TIMEOUT,
        )
        log.info("  Redirected to Microsoft")
    except PlaywrightTimeout:
        log.warning("  Redirect timeout — current URL: %s", page.url)


# ═══════════════════════════════════════════════════════════════════════
#  MICROSOFT LOGIN ROUTER
# ═══════════════════════════════════════════════════════════════════════

def handle_microsoft_login(page: Page) -> None:
    page.wait_for_load_state("domcontentloaded")
    page.wait_for_timeout(2000)

    if _visible(page, 'text="Pick an account"', timeout=5000):
        log.info("MICROSOFT — 'Pick an account' screen")
        _handle_account_picker(page)
        return

    if _visible(page, 'input[type="email"], input[name="loginfmt"]', timeout=3000):
        log.info("MICROSOFT — Direct email form")
        _fill_email_step(page)
        return

    if _visible(page, 'input[type="password"], input[name="passwd"]', timeout=3000):
        log.info("MICROSOFT — Password screen (email pre-filled)")
        _fill_password_step(page)
        _handle_post_login(page)
        return

    log.warning("MICROSOFT — Unexpected state. URL: %s", page.url)
    page.wait_for_timeout(5000)

    if _visible(page, 'text="Pick an account"', timeout=3000):
        _handle_account_picker(page)
    elif _visible(page, 'input[type="email"], input[name="loginfmt"]', timeout=3000):
        _fill_email_step(page)
    else:
        log.error("  Cannot determine state — opening inspector")
        page.pause()


# ═══════════════════════════════════════════════════════════════════════
#  ACCOUNT PICKER
# ═══════════════════════════════════════════════════════════════════════

def _handle_account_picker(page: Page) -> None:
    target = EMAIL.lower()
    body_text = (page.locator("body").inner_text() or "").lower()

    if target in body_text:
        log.info("  FLOW A — '%s' found in list", EMAIL)
        matched = _click_account_tile(page, target)

        if not matched:
            log.warning("    Email visible but tile not clickable — falling back to Flow B")
            _flow_use_another_account(page)
            return

        page.wait_for_load_state("domcontentloaded")
        page.wait_for_timeout(2000)

        if _visible(page, 'input[type="password"], input[name="passwd"]', timeout=5000):
            _fill_password_step(page)

        _handle_post_login(page)
    else:
        log.info("  FLOW B — '%s' not in list", EMAIL)
        _flow_use_another_account(page)


def _click_account_tile(page: Page, target_email: str) -> bool:
    log.info("    Searching for visible tile for %s...", target_email)

    locators = [
        page.get_by_text(target_email, exact=False),
        page.get_by_text(EMAIL, exact=False),
        page.locator(f'div[role="option"]:has-text("{target_email}")'),
        page.locator(f'div[role="button"]:has-text("{target_email}")'),
        page.locator(f'small:has-text("{target_email}")'),
    ]

    for loc in locators:
        try:
            if loc.count() > 0:
                target = loc.first
                log.info("    Found candidate element. Attempting click...")
                target.scroll_into_view_if_needed(timeout=2000)
                try:
                    target.click(timeout=3000)
                    log.info("    Clicked tile")
                    return True
                except PlaywrightTimeout:
                    log.warning("    Standard click timed out — trying force click...")
                    target.click(force=True, timeout=3000)
                    log.info("    Force clicked tile")
                    return True
        except Exception as e:
            log.debug("    (Strategy failed: %s)", e)
            continue

    # XPath fallback
    xpath = (
        f'//*[contains(translate(text(),'
        f'"ABCDEFGHIJKLMNOPQRSTUVWXYZ","abcdefghijklmnopqrstuvwxyz"), '
        f'"{target_email.lower()}")]'
    )
    tiles = page.locator(xpath)
    for i in range(tiles.count()):
        tile = tiles.nth(i)
        if tile.is_visible():
            try:
                tile.click(force=True, timeout=3000)
                log.info("    Clicked tile (XPath)")
                return True
            except Exception:
                continue

    # Dispatch event fallback
    for loc in locators:
        if loc.count() > 0:
            try:
                loc.first.dispatch_event("click")
                log.info("    Dispatched click event")
                return True
            except Exception:
                continue

    return False


def _flow_use_another_account(page: Page) -> None:
    for sel in [
        '#otherTile', 'text="Use another account"',
        'text="Sign in with a different account"',
        '[data-test-id="otherTile"]',
    ]:
        if _click_if_visible(page, sel, timeout=5000):
            log.info("    Clicked 'Use another account'")
            break

    page.wait_for_load_state("domcontentloaded")
    page.wait_for_timeout(1500)
    _fill_email_step(page)


# ═══════════════════════════════════════════════════════════════════════
#  EMAIL STEP
# ═══════════════════════════════════════════════════════════════════════

def _fill_email_step(page: Page) -> None:
    log.info("  Entering email: %s", EMAIL)

    email_input = page.locator('input[type="email"], input[name="loginfmt"]').first
    email_input.wait_for(state="visible", timeout=EL_TIMEOUT)
    email_input.click()
    email_input.fill("")
    email_input.fill(EMAIL)
    log.info("    Email entered")

    for attempt in range(1, 4):
        log.info("    Sending 'Next' (Attempt %d/3)...", attempt)
        email_input.focus()
        page.keyboard.press("Enter")
        page.wait_for_timeout(1000)
        _click_if_visible(page, '#idSIButton9', timeout=1000)
        _click_if_visible(page, 'input[type="submit"][value="Next"]', timeout=500)

        try:
            page.wait_for_function(
                """() => {
                    const emailField = document.querySelector('input[type="email"], input[name="loginfmt"]');
                    const pwField = document.querySelector('input[type="password"], input[name="passwd"]');
                    const isEmailHidden = !emailField || emailField.offsetParent === null;
                    const isPwVisible = pwField && pwField.offsetParent !== null;
                    return isEmailHidden || isPwVisible;
                }""",
                timeout=5000
            )
            log.info("    Transition detected")
            break
        except PlaywrightTimeout:
            error = _has_error(page)
            if error:
                log.error("  Microsoft error: %s", error)
                return
            if attempt == 3:
                log.error("  Email step failed after 3 attempts")
                page.screenshot(path="email_step_failed.png")
                return

    log.info("    Waiting for password field...")
    try:
        pw_selector = 'input[type="password"], input[name="passwd"]'
        page.wait_for_selector(pw_selector, state="visible", timeout=10000)
        _fill_password_step(page)
        _handle_post_login(page)
    except PlaywrightTimeout:
        if _is_on_app(page):
            log.info("    Logged in (skipped password)")
        else:
            log.error("  Password field never appeared")
            page.screenshot(path="password_missing.png")
            page.pause()


# ═══════════════════════════════════════════════════════════════════════
#  PASSWORD STEP
# ═══════════════════════════════════════════════════════════════════════

def _fill_password_step(page: Page) -> None:
    log.info("  Entering password...")

    pw_selector = 'input[type="password"], input[name="passwd"]'
    pw_input = page.locator(pw_selector).first
    pw_input.wait_for(state="visible", timeout=EL_TIMEOUT)
    pw_input.click()
    pw_input.fill(PASSWORD)
    log.info("    Password entered")

    pw_input.focus()
    page.keyboard.press("Enter")
    page.wait_for_timeout(1000)
    _click_if_visible(page, '#idSIButton9', timeout=1000)
    _click_if_visible(page, 'input[type="submit"][value="Sign in"]', timeout=500)

    log.info("    Submission sent")
    page.wait_for_timeout(3000)

    error = _has_error(page)
    if error:
        log.error("  Password rejected: %s", error)
        log.error("     Check JORDEX_PASSWORD in your .env file")
        page.pause()
        return

    if _visible(page, pw_selector, timeout=2000):
        error2 = _has_error(page)
        if error2:
            log.error("  Password error: %s", error2)
            page.pause()
            return
        else:
            log.info("    Waiting for post-password transition...")
            page.wait_for_timeout(3000)

    log.info("    Password step complete")


# ═══════════════════════════════════════════════════════════════════════
#  POST-LOGIN (Stay signed in? / Permissions / MFA)
# ═══════════════════════════════════════════════════════════════════════

def _handle_post_login(page: Page) -> None:
    if _visible(page, 'text="Stay signed in?"', timeout=5000):
        log.info("  'Stay signed in?' prompt")
        _click_if_visible(page, '#idSIButton9', timeout=3000) or \
        _click_if_visible(page, 'button:has-text("Yes")', timeout=2000) or \
        _click_if_visible(page, 'input[type="submit"][value="Yes"]', timeout=2000)
        log.info("    Clicked Yes")
        page.wait_for_timeout(2000)

    if _visible(page, 'text="Permissions requested"', timeout=3000):
        log.info("  Permissions screen — accepting")
        _click_if_visible(page, '#idBtn_Accept', timeout=3000) or \
        _click_if_visible(page, 'button:has-text("Accept")', timeout=2000) or \
        _click_if_visible(page, 'input[type="submit"][value="Accept"]', timeout=2000)
        log.info("    Accepted")
        page.wait_for_timeout(2000)

    if _visible(page, '#idBtn_Accept', timeout=2000):
        page.locator('#idBtn_Accept').click()
        log.info("    Clicked Accept")
        page.wait_for_timeout(2000)

    if not _is_on_app(page):
        _wait_for_mfa(page)

def _wait_for_mfa(page: Page) -> None:
    import base64
    if _is_on_app(page):
        return

    # Poll up to 10s for the MFA number to appear before locking in
    mfa_number = None
    for _ in range(20):
        try:
            mfa_number = page.evaluate("""() => {
                // Microsoft number-match: large 2-digit number
                const selectors = [
                    '#displaySign',
                    '[data-testid="displaySign"]',
                    '.display-sign',
                    '#idRichContext_DisplaySign',
                ];
                for (const sel of selectors) {
                    const el = document.querySelector(sel);
                    if (el && el.textContent.trim()) return el.textContent.trim();
                }
                // Fallback: any standalone 2-3 digit number rendered large
                const all = [...document.querySelectorAll('*')].filter(el => {
                    if (el.childElementCount > 0) return false;
                    const text = (el.textContent || '').trim();
                    if (!/^\d{2,3}$/.test(text)) return false;
                    const style = window.getComputedStyle(el);
                    const fs = parseFloat(style.fontSize);
                    return fs >= 24 && el.offsetParent !== null;
                });
                return all.length > 0 ? all[0].textContent.trim() : null;
            }""")
        except Exception:
            pass

        if mfa_number:
            break
        page.wait_for_timeout(500)

    # Take screenshot
    screenshot_b64 = None
    try:
        ss_bytes = page.screenshot()
        screenshot_b64 = base64.b64encode(ss_bytes).decode('utf-8')
    except Exception:
        pass

    # Update shared state — this is what /api/mfa/status reads
    mfa_state["active"] = True
    mfa_state["number"] = mfa_number
    mfa_state["screenshot_b64"] = screenshot_b64

    log.info("")
    log.info("  ╔══════════════════════════════════════════════╗")
    log.info("  ║  MFA REQUIRED — Approve on your phone       ║")
    if mfa_number:
        log.info("  ║  Number: %-36s  ║", mfa_number)
    log.info("  ║  Waiting up to %3ds...                      ║", MFA_TIMEOUT // 1000)
    log.info("  ╚══════════════════════════════════════════════╝")
    log.info("")

    # Keep screenshot fresh every 3s while waiting
    def _refresh_screenshot():
        for _ in range(MFA_TIMEOUT // 3000):
            if not mfa_state["active"]:
                break
            try:
                ss = page.screenshot()
                mfa_state["screenshot_b64"] = base64.b64encode(ss).decode('utf-8')
                # Re-try number extraction
                if not mfa_state["number"]:
                    try:
                        n = page.evaluate("""() => {
                            const el = document.querySelector('#displaySign, #idRichContext_DisplaySign');
                            return el ? el.textContent.trim() : null;
                        }""")
                        if n:
                            mfa_state["number"] = n
                    except Exception:
                        pass
            except Exception:
                break
            import time as _time
            _time.sleep(3)

    refresh_thread = threading.Thread(target=_refresh_screenshot, daemon=True)
    refresh_thread.start()

    try:
        page.wait_for_url(
            lambda url: "jit.jordex.com" in url,
            timeout=MFA_TIMEOUT,
        )
        log.info("  MFA approved — landed on Jordex")
    except PlaywrightTimeout:
        if _is_on_app(page):
            log.info("  MFA approved")
        else:
            log.warning("  MFA timeout — URL: %s", page.url)
            log.info("  Approve on your phone, then press Enter...")
            try:
                input("  >>> ")
            except EOFError:
                pass
    finally:
        mfa_state["active"] = False
        mfa_state["number"] = None
        mfa_state["screenshot_b64"] = None

# ═══════════════════════════════════════════════════════════════════════
#  LOGIN ORCHESTRATOR
# ═══════════════════════════════════════════════════════════════════════

def login(context: BrowserContext) -> Page:
    page = context.new_page()
    page.set_default_timeout(NAV_TIMEOUT)

    log.info("Navigating to %s...", DASHBOARD_URL)
    page.goto(DASHBOARD_URL, wait_until="domcontentloaded", timeout=90000)
    _apply_zoom(page)
    page.wait_for_timeout(3000)

    # Already logged in?
    if _is_on_app(page):
        log.info("App URL detected (possible session reuse)")
        if not _wait_for_dashboard_ready(page, timeout=8000):
            log.warning("    Page appears blank — refreshing...")
            try:
                page.reload(wait_until="load", timeout=15000)
            except Exception as re:
                log.warning("    Reload failed: %s. Continuing...", re)
            _wait_for_dashboard_ready(page, timeout=10000)

        if _is_on_app(page):
            log.info("Dashboard ready (session reused)")
            # page.pause()  # Open Playwright Inspector (comment out to disable)
            return page


    # Auth0 → "Continue with Azure"
    if _is_on_auth0(page):
        handle_auth0(page)

    # Microsoft login
    if _is_on_microsoft(page):
        handle_microsoft_login(page)
    elif _is_on_auth0(page):
        log.warning("Still on Auth0 — retrying...")
        handle_auth0(page)
        if _is_on_microsoft(page):
            handle_microsoft_login(page)

    # Final check
    _wait_for_dashboard_ready(page, timeout=15000)

    if not _is_on_app(page):
        log.info("Not on dashboard — navigating explicitly...")
        page.goto(DASHBOARD_URL, wait_until="load")
        page.wait_for_timeout(3000)

    if _is_on_app(page):
        log.info("Login successful — %s", page.url)
        # page.pause()  # Open Playwright Inspector (comment out to disable)
    else:
        log.warning("May not be logged in — URL: %s", page.url)

    return page


# ═══════════════════════════════════════════════════════════════════════
#  DASHBOARD FILTERS                                                        
# ═══════════════════════════════════════════════════════════════════════

def apply_filters(page: Page, status_callback=None):
    """Navigate to dashboard and apply: Backoffice import + Check shipment."""

    def log_status(msg):
        log.info(msg)
        if status_callback:
            status_callback(msg)

    # 1. Navigate to Dashboard
    log_status("Navigating to Task Dashboard...")

    dashboard_reached = False

    # Strategy 1: Click "Dashboard" by visible text
    try:
        dash_link = page.get_by_text("Dashboard", exact=True).first
        dash_link.click(timeout=90000)
        dashboard_reached = True
    except Exception as e:
        log.warning("Dashboard text click failed: %s", e)

    # Strategy 2: Role-based link
    if not dashboard_reached:
        try:
            page.get_by_role("link", name="Dashboard").click(timeout=10000)
            dashboard_reached = True
        except Exception as e:
            log.warning("Dashboard role link failed: %s", e)

    # Strategy 3: Direct URL
    if not dashboard_reached:
        log.warning("Both click strategies failed — attempting direct URL.")
        try:
            page.goto(
                "https://jit.jordex.com/shipments/ocean",
                wait_until="load", timeout=60000
            )
        except Exception as e:
            log.error("Direct URL navigation also failed: %s", e)

    log_status("Waiting for Dashboard to load...")
    try:
        page.wait_for_selector(".el-table__body, .el-menu", timeout=20000)
    except Exception:
        log.warning("Timeout waiting for dashboard content")
    page.wait_for_timeout(3000)

    # 2. Filter: Department → Backoffice import
    log_status("Filtering for Department: Backoffice import...")
    dept_dropdown = page.get_by_text("Backoffice export", exact=False).first
    if not dept_dropdown.is_visible():
        dept_dropdown = page.locator(".el-select").first

    try:
        dept_dropdown.click(timeout=5000)
        page.wait_for_timeout(1000)
        page.get_by_role("listitem").get_by_text(
            "Backoffice import", exact=True
        ).click()
        page.keyboard.press("Escape")
        page.wait_for_timeout(2000)
    except Exception as e:
        log.warning("Failed to set Department filter: %s", e)

    # 3. Clear existing assignee/search filters
    log_status("Clearing existing filters...")
    try:
        clear_btn = page.locator(".filter-clear").first
        if clear_btn.is_visible(timeout=2000):
            clear_btn.click()
            page.wait_for_timeout(1000)
    except Exception:
        pass

    # 4. Filter: Task type → Check shipment
    log_status("Filtering for Task Type: Check shipment...")
    try:
        task_dropdown = page.get_by_text("Task type All", exact=False).first
        if not task_dropdown.is_visible():
            task_dropdown = page.locator(".el-select").nth(1)

        if task_dropdown.is_visible():
            task_dropdown.click()
            page.wait_for_timeout(1000)
            page.keyboard.type("check")
            page.wait_for_timeout(1000)
            page.get_by_role("listitem").get_by_text(
                "Check shipment", exact=True
            ).click()
            page.keyboard.press("Escape")

            log_status("Waiting for table to reload filtered data...")
            page.wait_for_timeout(2000)
            try:
                page.locator(".el-loading-mask").wait_for(
                    state="visible", timeout=3000
                )
                page.locator(".el-loading-mask").wait_for(
                    state="hidden", timeout=15000
                )
            except Exception:
                pass
            page.wait_for_timeout(3000)
    except Exception as e:
        log.warning("Failed to set Task Type filter: %s", e)


def apply_date_filter(page: Page, start_date: str, end_date: str,
                      status_cb=None):
    """
    Set From/To date filters in Jordex.
    Args:
        start_date: ISO format "2026-05-09"
        end_date:   ISO format "2026-05-11"
    """
    def update(msg):
        log.info(msg)
        if status_cb:
            status_cb(msg)

    try:
        start_dt = datetime.strptime(start_date, "%Y-%m-%d")
        end_dt = datetime.strptime(end_date, "%Y-%m-%d")
    except ValueError:
        log.warning("Invalid date format: %s / %s. Skipping.", start_date, end_date)
        return

    def pick_date(filter_label: str, target_dt: datetime):
        update(f"  Setting '{filter_label}' to {target_dt.strftime('%d %b %Y')}...")

        # Click the filter container
        try:
            container = page.locator(
                f".filter-select-container:has-text('{filter_label}')"
            ).first
            if container.is_visible(timeout=3000):
                container.click()
            else:
                page.evaluate(f"""() => {{
                    const containers = document.querySelectorAll('.filter-select-container');
                    for (const c of containers) {{
                        if (c.textContent.includes('{filter_label}')) {{
                            c.click();
                            return;
                        }}
                    }}
                }}""")
        except Exception as e:
            update(f"  Could not click '{filter_label}' filter: {e}")
            return

        page.wait_for_timeout(1500)

        target_str = f"{target_dt.day} {target_dt.strftime('%b')} {target_dt.year}"
        date_filled = False

        # Strategy 1: Input fill
        try:
            date_input = page.locator(
                "input[placeholder='Select date']:visible, "
                "input[placeholder='Pick a date']:visible"
            ).last
            if date_input.is_visible(timeout=2000):
                date_input.click()
                page.wait_for_timeout(500)
                date_input.fill("")
                date_input.fill(target_str)
                date_input.press("Enter")
                page.wait_for_timeout(500)
                date_filled = True
                update(f"  Date input filled: {target_str}")
        except Exception:
            pass

        # Strategy 2: JS fallback
        if not date_filled:
            try:
                date_filled = page.evaluate(f"""() => {{
                    const inputs = document.querySelectorAll(
                        'input[placeholder="Select date"], '
                        + 'input[placeholder="Pick a date"]'
                    );
                    const last = inputs[inputs.length - 1];
                    if (last) {{
                        last.focus();
                        last.value = '{target_str}';
                        last.dispatchEvent(new Event('input', {{bubbles: true}}));
                        last.dispatchEvent(new Event('change', {{bubbles: true}}));
                        return true;
                    }}
                    return false;
                }}""")
                if date_filled:
                    page.wait_for_timeout(500)
                    update(f"  Date input filled via JS: {target_str}")
            except Exception:
                pass

        # Strategy 3: Calendar navigation
        if not date_filled:
            try:
                update(f"  Navigating calendar to {target_dt.strftime('%B %Y')}...")
                for _ in range(24):
                    header_text = ""
                    try:
                        header_spans = page.locator(
                            ".el-date-picker__header span:visible, "
                            ".el-date-picker__header button:visible"
                        ).all()
                        for span in header_spans:
                            t = span.inner_text(timeout=500).strip()
                            if t:
                                header_text += t + " "
                    except Exception:
                        pass

                    header_text = header_text.strip()
                    if (str(target_dt.year) in header_text and
                            target_dt.strftime("%B") in header_text):
                        break

                    try:
                        next_btn = page.locator(
                            ".el-date-picker__header .el-icon-arrow-right:visible, "
                            ".el-picker-panel__icon-btn.el-icon-arrow-right:visible"
                        ).first
                        if next_btn.is_visible(timeout=1000):
                            next_btn.click()
                            page.wait_for_timeout(500)
                    except Exception:
                        break

                day_num = target_dt.day
                day_clicked = page.evaluate(f"""() => {{
                    const cells = document.querySelectorAll(
                        '.el-date-table td:not(.prev-month):not(.next-month)'
                    );
                    for (const cell of cells) {{
                        const span = cell.querySelector('span') || cell;
                        if (span.textContent.trim() === '{day_num}') {{
                            cell.click();
                            return true;
                        }}
                    }}
                    return false;
                }}""")

                if day_clicked:
                    date_filled = True
                    update(f"  Clicked day {day_num} in calendar.")
                else:
                    update(f"  Could not find day {day_num} in calendar.")

            except Exception as e:
                update(f"  Calendar navigation error: {e}")

        # Dismiss popups
        page.wait_for_timeout(500)
        try:
            ok_btn = page.locator(
                "button:has-text('OK'):visible, "
                ".el-picker-panel__footer button:has-text('OK'):visible"
            ).first
            if ok_btn.is_visible(timeout=1500):
                ok_btn.click()
                page.wait_for_timeout(500)
        except Exception:
            pass

        try:
            page.locator("body").click(position={"x": 10, "y": 10})
        except Exception:
            pass

        page.wait_for_timeout(1000)

    # Apply From and To
    update("Applying date range filter...")
    pick_date("From", start_dt)
    pick_date("To", end_dt)

    # Wait for table reload
    update("Waiting for table to reload filtered data...")
    try:
        page.locator(".el-loading-mask").wait_for(state="visible", timeout=3000)
    except Exception:
        pass
    try:
        page.locator(".el-loading-mask").wait_for(state="hidden", timeout=15000)
    except Exception:
        pass
    page.wait_for_timeout(2000)

    update(f"Date filter applied: {start_date} -> {end_date}")


# ═══════════════════════════════════════════════════════════════════════
#  MAIN ENTRY POINT
# ═══════════════════════════════════════════════════════════════════════

# def main(headless: bool = None) -> Page:
#     """Launch browser, login, return authenticated Page."""
#     if not EMAIL or not PASSWORD:
#         log.error("JORDEX_EMAIL and JORDEX_PASSWORD must be set in .env")
#         sys.exit(1)

#     log.info("Email: %s", EMAIL)
#     log.info("Password: %s", "*" * len(PASSWORD))

#     session_path = str(Path(SESSION_DIR).resolve())
#     Path(session_path).mkdir(parents=True, exist_ok=True)
#     log.info("Session: %s", session_path)

#     pw = sync_playwright().start()
#     is_headless = HEADLESS if headless is None else headless

#     context = pw.chromium.launch_persistent_context(
#         user_data_dir=session_path,
#         headless=is_headless,
#         channel="chrome",
#         slow_mo=SLOW_MO,
#         viewport=None,
#         args=[
#             "--disable-blink-features=AutomationControlled",
#             "--start-maximized",
#         ],
#         ignore_default_args=["--enable-automation"],
#     )
#     context.add_init_script("""
# (() => {
#     const applyZoom = () => {
#         document.documentElement.style.zoom = '0.75';
#         document.documentElement.style.minHeight = '100vh';
#         document.documentElement.style.overflowY = 'auto';
#     };
#     if (document.readyState === 'loading') {
#         document.addEventListener('DOMContentLoaded', applyZoom);
#     } else {
#         applyZoom();
#     }
#     window.addEventListener('load', applyZoom);
# })();
# """)

#     page = login(context)

#     # Attach playwright instance so caller can shut down later
#     page._pw_instance = pw

#     return page

def main(headless: bool = None, session_dir: str = None) -> Page:
    """Launch browser, login, return authenticated Page."""
    if not EMAIL or not PASSWORD:
        log.error("JORDEX_EMAIL and JORDEX_PASSWORD must be set in .env")
        sys.exit(1)

    log.info("Email: %s", EMAIL)
    log.info("Password: %s", "*" * len(PASSWORD))

    # Use provided session_dir or fall back to env default
    effective_session_dir = session_dir or SESSION_DIR
    session_path = str(Path(effective_session_dir).resolve())
    Path(session_path).mkdir(parents=True, exist_ok=True)
    log.info("Session: %s", session_path)

    pw = sync_playwright().start()
    is_headless = HEADLESS if headless is None else headless

    context = pw.chromium.launch_persistent_context(
        user_data_dir=session_path,
        headless=is_headless,
        channel="chrome",
        slow_mo=SLOW_MO,
        viewport=None,
        args=[
            "--disable-blink-features=AutomationControlled",
            "--start-maximized",
        ],
        ignore_default_args=["--enable-automation"],
    )
    context.add_init_script("""
(() => {
    const applyZoom = () => {
        document.documentElement.style.zoom = '0.75';
        document.documentElement.style.minHeight = '100vh';
        document.documentElement.style.overflowY = 'auto';
    };
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', applyZoom);
    } else {
        applyZoom();
    }
    window.addEventListener('load', applyZoom);
})();
""")

    page = login(context)
    page._pw_instance = pw
    return page


if __name__ == "__main__":
    page = main()
    # page.pause()
    log.info("Login complete. Browser stays open.")
    log.info("Press Ctrl+C to close.")
    try:
        page.wait_for_timeout(999_999_999)
    except KeyboardInterrupt:
        log.info("Shutting down...")
        page.context.close()
        if hasattr(page, '_pw_instance'):
            page._pw_instance.stop()
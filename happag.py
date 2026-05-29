# from scrapling.fetchers import StealthyFetcher
# import time

# container_no = "NIDU2369827"
# vessel_name  = "MSC MUGE"

# def full_action(page):
#     print(f"📍 Current URL: {page.url}")
#     page.set_viewport_size({"width": 1280, "height": 720})

#     # ── STEP 1: Cookie consent ────────────────────────────────────────────
#     print("🍪 Checking for cookie consent...")
#     time.sleep(2)
#     try:
#         cookie_selectors = [
#             'button:has-text("Confirm my choices")',
#             'button:has-text("Accept all")',
#             'button:has-text("Accept All")',
#             'button:has-text("Confirm My Choices")',
#             'button:has-text("I agree")',
#             'button:has-text("Allow all")',
#         ]
#         for sel in cookie_selectors:
#             btn = page.locator(sel).first
#             if btn.is_visible():
#                 btn.click()
#                 print(f"✅ Cookie accepted: {sel}")
#                 time.sleep(1.5)
#                 break
#         else:
#             print("ℹ️ No cookie popup found")
#     except Exception as e:
#         print(f"⚠️ Cookie: {e}")

#     # ── STEP 2: Click 'by Container' sidebar tab ──────────────────────────
#     print("🖱️ Clicking 'by Container' tab...")
#     try:
#         tab = page.locator('span.bs-sidebar-nav__sublink-label:has-text("by Container")').first
#         tab.wait_for(state="visible", timeout=8000)
#         tab.click()
#         print("✅ Clicked 'by Container' tab")
#         time.sleep(1.5)
#     except Exception as e:
#         print(f"⚠️ Tab click failed, trying link fallback: {e}")
#         try:
#             page.locator('a.bs-sidebar-nav__sublink:has-text("by Container")').first.click()
#             print("✅ Clicked via link fallback")
#             time.sleep(1.5)
#         except Exception as e2:
#             print(f"⚠️ Fallback also failed: {e2}")

#     # ── STEP 3: Fill container number ─────────────────────────────────────
#     print(f"⌨️ Filling container: {container_no}")
#     try:
#         input_field = page.locator('input.hal-olb-input[maxlength="13"]').first
#         input_field.wait_for(state="visible", timeout=10000)
#         input_field.click()
#         time.sleep(0.3)
#         page.keyboard.press("Control+A")
#         page.keyboard.press("Delete")
#         input_field.fill(container_no)
#         print(f"✅ Container filled: {container_no}")
#         time.sleep(0.5)
#     except Exception as e:
#         print(f"❌ Container fill failed: {e}")
#         page.screenshot(path="fill_error.png")
#         return

#     # ── STEP 4: Click Find (container) ────────────────────────────────────
#     print("🔍 Clicking Find for container...")
#     try:
#         find_btn = page.locator('button.hal-button--primary:has-text("Find")').first
#         find_btn.wait_for(state="visible", timeout=5000)
#         find_btn.click()
#         print("✅ Container Find clicked!")
#     except Exception as e:
#         print(f"❌ Container Find failed: {e}")
#         page.screenshot(path="btn_error.png")
#         return

#     # ── STEP 5: Screenshot container result ───────────────────────────────
#     print("⏳ Waiting for container results...")
#     time.sleep(6)
#     page.screenshot(path="hapag_container_result.png")
#     print("📸 Screenshot saved: hapag_container_result.png")

#     # ── STEP 6: Navigate to Vessel Tracker page ───────────────────────────
#     print("🚢 Opening Vessel Tracker...")
#     try:
#         vessel_tab = page.locator('span.bs-sidebar-nav__sublink-label:has-text("Vessel Tracker")').first
#         vessel_tab.wait_for(state="visible", timeout=8000)
#         vessel_tab.click()
#         print("✅ Vessel Tracker tab clicked")
#         time.sleep(4)  # wait for the vessel tracker page and its ExtJS form to render
#     except Exception as e:
#         print(f"⚠️ Vessel Tracker tab failed, trying link fallback: {e}")
#         try:
#             page.locator('a.bs-sidebar-nav__sublink:has-text("Vessel Tracker")').first.click()
#             print("✅ Vessel Tracker opened via fallback")
#             time.sleep(4)
#         except Exception as e2:
#             print(f"❌ Vessel Tracker open failed: {e2}")
#             return

#     # ── STEP 7: Fill vessel name ───────────────────────────────────────────
#     # The input is NOT inside the Usabilla iframe — it is a plain page element.
#     # Selector from the page HTML:
#     #   <input type="text" id="ext-gen118"
#     #          class="x-form-text x-form-field inputCombo sizeGiganticCombo x-form-empty-field"
#     #          autocomplete="off" size="24">
#     # It lives inside a wrapper: div.x-form-field-wrap > input.inputCombo
#     print(f"⌨️ Filling vessel: {vessel_name}")
#     vessel_selectors = [
#         'input.inputCombo.sizeGiganticCombo',   # most specific class combo
#         'input.inputCombo',                      # simpler fallback
#         'div.x-form-field-wrap input[type="text"]',  # parent-based fallback
#         'input[id^="ext-gen"]',                  # ExtJS auto-generated id prefix
#     ]
#     vessel_input = None
#     for sel in vessel_selectors:
#         try:
#             loc = page.locator(sel).first
#             loc.wait_for(state="visible", timeout=5000)
#             vessel_input = loc
#             print(f"✅ Vessel input found via: {sel}")
#             break
#         except Exception:
#             continue

#     if vessel_input is None:
#         print("❌ Could not find vessel input with any selector")
#         page.screenshot(path="vessel_input_error.png")
#         return

#     try:
#         vessel_input.click()
#         time.sleep(0.3)
#         page.keyboard.press("Control+A")
#         page.keyboard.press("Delete")
#         vessel_input.type(vessel_name, delay=80)   # type slowly so autocomplete fires
#         print(f"✅ Vessel name typed: {vessel_name}")
#         time.sleep(2)   # wait for the autocomplete dropdown to appear
#     except Exception as e:
#         print(f"❌ Vessel input type failed: {e}")
#         page.screenshot(path="vessel_type_error.png")
#         return

#     # ── STEP 8: Select matching vessel from dropdown ───────────────────────
#     # The ExtJS combo renders suggestions in a floated div outside the input's parent.
#     # Common ExtJS dropdown selectors:
#     #   .x-combo-list-item   — each row in the dropdown list
#     print(f"🔽 Selecting '{vessel_name}' from dropdown...")
#     try:
#         # Wait for the ExtJS dropdown list to appear
#         dropdown_row = page.locator(
#             f'.x-combo-list-item:has-text("{vessel_name}")'
#         ).first
#         dropdown_row.wait_for(state="visible", timeout=8000)
#         dropdown_row.click()
#         print(f"✅ Vessel selected from dropdown: {vessel_name}")
#         time.sleep(1)
#     except Exception as e:
#         print(f"⚠️ .x-combo-list-item dropdown failed: {e}")
#         # Fallback: any visible element containing the vessel name
#         try:
#             fallback = page.get_by_text(vessel_name, exact=False).first
#             fallback.wait_for(state="visible", timeout=5000)
#             fallback.click()
#             print(f"✅ Vessel selected via text fallback")
#             time.sleep(1)
#         except Exception as e2:
#             print(f"⚠️ Text fallback failed too, pressing Enter: {e2}")
#             vessel_input.press("Enter")
#             time.sleep(1)

#     # ── STEP 9: Click Find (vessel) ────────────────────────────────────────
#     print("🔍 Clicking Find for vessel...")
#     try:
#         # The vessel tracker Find button — same primary button class as container page
#         find_btn = page.locator('button.hal-button--primary:has-text("Find")').first
#         find_btn.wait_for(state="visible", timeout=5000)
#         find_btn.click()
#         print("✅ Vessel Find clicked!")
#     except Exception as e:
#         print(f"⚠️ Primary Find button failed, trying input[type=button]: {e}")
#         try:
#             # ExtJS sometimes renders buttons as <button class="x-btn-text">
#             btn2 = page.locator('button:has-text("Find"), input[value="Find"]').first
#             btn2.wait_for(state="visible", timeout=5000)
#             btn2.click()
#             print("✅ Vessel Find clicked via fallback!")
#         except Exception as e2:
#             print(f"❌ Vessel Find failed: {e2}")
#             page.screenshot(path="vessel_find_error.png")
#             return

#     # ── STEP 10: Screenshot vessel result ─────────────────────────────────
#     print("⏳ Waiting for vessel results...")
#     time.sleep(8)
#     page.screenshot(path="hapag_vessel_result.png")
#     print("📸 Screenshot saved: hapag_vessel_result.png")
#     page.pause()

#     result = page.inner_text('body')
#     print(f"\n📦 Page text (first 1000 chars):\n{result[:1000]}")


# print("🌐 Bypassing Cloudflare...")
# StealthyFetcher.fetch(
#     "https://www.hapag-lloyd.com/en/online-business/track/track-by-container-solution.html",
#     headless=False,
#     solve_cloudflare=True,
#     browser_type="chromium",
#     executable_path=r"C:\Program Files\Google\Chrome\Application\chrome.exe",
#     block_webrtc=True,
#     hide_canvas=True,
#     network_idle=True,
#     google_search=True,
#     wait=3,
#     page_action=full_action,
# )

from scrapling.fetchers import StealthyFetcher
import time

container_no  = "NIDU2369827"
vessel_name   = "MSC MUGE"
arrival_place = "ROTTERDAM"       # ← Target arrival port (exact, uppercase)

def full_action(page):
    print(f"📍 Current URL: {page.url}")
    page.set_viewport_size({"width": 1280, "height": 720})

    # ── STEP 1: Cookie consent ────────────────────────────────────────────
    print("🍪 Checking for cookie consent...")
    time.sleep(2)
    try:
        cookie_selectors = [
            'button:has-text("Confirm my choices")',
            'button:has-text("Accept all")',
            'button:has-text("Accept All")',
            'button:has-text("Confirm My Choices")',
            'button:has-text("I agree")',
            'button:has-text("Allow all")',
        ]
        for sel in cookie_selectors:
            btn = page.locator(sel).first
            if btn.is_visible():
                btn.click()
                print(f"✅ Cookie accepted: {sel}")
                time.sleep(1.5)
                break
        else:
            print("ℹ️ No cookie popup found")
    except Exception as e:
        print(f"⚠️ Cookie: {e}")

    # ── STEP 2: Click 'by Container' sidebar tab ──────────────────────────
    print("🖱️ Clicking 'by Container' tab...")
    try:
        tab = page.locator('span.bs-sidebar-nav__sublink-label:has-text("by Container")').first
        tab.wait_for(state="visible", timeout=8000)
        tab.click()
        print("✅ Clicked 'by Container' tab")
        time.sleep(1.5)
    except Exception as e:
        print(f"⚠️ Tab click failed, trying link fallback: {e}")
        try:
            page.locator('a.bs-sidebar-nav__sublink:has-text("by Container")').first.click()
            print("✅ Clicked via link fallback")
            time.sleep(1.5)
        except Exception as e2:
            print(f"⚠️ Fallback also failed: {e2}")

    # ── STEP 3: Fill container number ─────────────────────────────────────
    print(f"⌨️ Filling container: {container_no}")
    try:
        input_field = page.locator('input.hal-olb-input[maxlength="13"]').first
        input_field.wait_for(state="visible", timeout=10000)
        input_field.click()
        time.sleep(0.3)
        page.keyboard.press("Control+A")
        page.keyboard.press("Delete")
        input_field.fill(container_no)
        print(f"✅ Container filled: {container_no}")
        time.sleep(0.5)
    except Exception as e:
        print(f"❌ Container fill failed: {e}")
        page.screenshot(path="fill_error.png", full_page=True)
        return

    # ── STEP 4: Click Find (container) ────────────────────────────────────
    print("🔍 Clicking Find for container...")
    try:
        find_btn = page.locator('button.hal-button--primary:has-text("Find")').first
        find_btn.wait_for(state="visible", timeout=5000)
        find_btn.click()
        print("✅ Container Find clicked!")
    except Exception as e:
        print(f"❌ Container Find failed: {e}")
        page.screenshot(path="btn_error.png", full_page=True)
        return

    # ── STEP 5: Full page screenshot — container result ───────────────────
    print("⏳ Waiting for container results...")
    time.sleep(6)
    page.screenshot(path="hapag_container_result.png", full_page=True)
    print("📸 Full-page screenshot saved: hapag_container_result.png")

    # ── STEP 6: Navigate to Vessel Tracker page ───────────────────────────
    print("🚢 Opening Vessel Tracker...")
    try:
        vessel_tab = page.locator('span.bs-sidebar-nav__sublink-label:has-text("Vessel Tracker")').first
        vessel_tab.wait_for(state="visible", timeout=8000)
        vessel_tab.click()
        print("✅ Vessel Tracker tab clicked")
        time.sleep(4)
    except Exception as e:
        print(f"⚠️ Vessel Tracker tab failed, trying link fallback: {e}")
        try:
            page.locator('a.bs-sidebar-nav__sublink:has-text("Vessel Tracker")').first.click()
            print("✅ Vessel Tracker opened via fallback")
            time.sleep(4)
        except Exception as e2:
            print(f"❌ Vessel Tracker open failed: {e2}")
            return

    # ── STEP 7: Fill vessel name ───────────────────────────────────────────
    print(f"⌨️ Filling vessel: {vessel_name}")
    vessel_selectors = [
        'input.inputCombo.sizeGiganticCombo',
        'input.inputCombo',
        'div.x-form-field-wrap input[type="text"]',
        'input[id^="ext-gen"]',
    ]
    vessel_input = None
    for sel in vessel_selectors:
        try:
            loc = page.locator(sel).first
            loc.wait_for(state="visible", timeout=5000)
            vessel_input = loc
            print(f"✅ Vessel input found via: {sel}")
            break
        except Exception:
            continue

    if vessel_input is None:
        print("❌ Could not find vessel input")
        page.screenshot(path="vessel_input_error.png", full_page=True)
        return

    try:
        vessel_input.click()
        time.sleep(0.3)
        page.keyboard.press("Control+A")
        page.keyboard.press("Delete")
        vessel_input.type(vessel_name, delay=80)
        print(f"✅ Vessel name typed: {vessel_name}")
        time.sleep(2)
    except Exception as e:
        print(f"❌ Vessel input type failed: {e}")
        page.screenshot(path="vessel_type_error.png", full_page=True)
        return

    # ── STEP 8: Select matching vessel from dropdown ───────────────────────
    print(f"🔽 Selecting '{vessel_name}' from dropdown...")
    try:
        dropdown_row = page.locator(f'.x-combo-list-item:has-text("{vessel_name}")').first
        dropdown_row.wait_for(state="visible", timeout=8000)
        dropdown_row.click()
        print(f"✅ Vessel selected: {vessel_name}")
        time.sleep(1)
    except Exception as e:
        print(f"⚠️ Dropdown selection failed, trying text fallback: {e}")
        try:
            fallback = page.get_by_text(vessel_name, exact=False).first
            fallback.wait_for(state="visible", timeout=5000)
            fallback.click()
            print("✅ Vessel selected via text fallback")
            time.sleep(1)
        except Exception as e2:
            print(f"⚠️ Text fallback failed, pressing Enter: {e2}")
            vessel_input.press("Enter")
            time.sleep(1)

    # ── STEP 9: Click Find (vessel) ────────────────────────────────────────
    print("🔍 Clicking Find for vessel...")
    try:
        find_btn = page.locator('button.hal-button--primary:has-text("Find")').first
        find_btn.wait_for(state="visible", timeout=5000)
        find_btn.click()
        print("✅ Vessel Find clicked!")
    except Exception as e:
        print(f"⚠️ Primary Find button failed: {e}")
        try:
            btn2 = page.locator('button:has-text("Find"), input[value="Find"]').first
            btn2.wait_for(state="visible", timeout=5000)
            btn2.click()
            print("✅ Vessel Find clicked via fallback!")
        except Exception as e2:
            print(f"❌ Vessel Find failed: {e2}")
            page.screenshot(path="vessel_find_error.png", full_page=True)
            return

    # Wait for vessel results to load
    print("⏳ Waiting for vessel results...")
    time.sleep(8)

    # ── STEP 10: Full page screenshot — vessel schedule loaded ───────────
    page.screenshot(path="hapag_vessel_result.png", full_page=True)
    print("📸 Full-page screenshot saved: hapag_vessel_result.png")

    # ── STEP 11: Select the correct Arrival Place row ─────────────────────
    # Results are on the main page. Port names sit in <td> cells that often
    # have leading/trailing whitespace, so text-is() returns 0 matches.
    # We use an XPath normalize-space() approach instead, targeting only
    # cells in the schedule body (not the voyage header rows).
    print(f"\n📍 Looking for Arrival Place: {arrival_place}")
    try:
        # The port name lives inside a child element of <td>, not as a direct
        # text node — so normalize-space(text()) misses it.
        # Use normalize-space(.) which collapses ALL descendant text.
        xpath_sel = f'xpath=//td[normalize-space(.)="{arrival_place}"]'
        arrival_cells = page.locator(xpath_sel)
        count = arrival_cells.count()
        print(f"   XPath normalize-space(.): {count} match(es)")

        if count == 0:
            # Fallback: CSS partial match
            arrival_cells = page.locator(f'td:has-text("{arrival_place}")')
            count = arrival_cells.count()
            print(f"   CSS partial match fallback: {count} match(es)")

        if count == 0:
            raise RuntimeError(f"No cells found for '{arrival_place}'")

        # Debug: show exactly what each match contains so we know which index is right
        print(f"   Matched cells (showing up to 10):")
        for i in range(min(count, 10)):
            try:
                txt = arrival_cells.nth(i).inner_text().strip().replace("\n", " | ")
                print(f"     [{i}] '{txt[:120]}'")
            except Exception:
                pass

        # The schedule rows contain ONLY the port name in that <td>.
        # Header/voyage rows contain more text. Find the first cell whose
        # stripped text is exactly the port name (handles the CSS fallback case).
        target_idx = 0
        for i in range(count):
            try:
                txt = arrival_cells.nth(i).inner_text().strip()
                if txt == arrival_place:
                    target_idx = i
                    print(f"   ✅ Best match at index [{i}]")
                    break
            except Exception:
                pass

        # Click the best-matched row
        arrival_cells.nth(target_idx).scroll_into_view_if_needed()
        arrival_cells.nth(target_idx).click()
        print(f"✅ Arrival Place clicked: {arrival_place} (index {target_idx})")
        time.sleep(1.5)

    except Exception as e:
        print(f"❌ Could not select Arrival Place '{arrival_place}': {e}")
        page.screenshot(path="arrival_error.png", full_page=True)
        print("📸 Error screenshot: arrival_error.png")

    # ── STEP 12: Click the Terminal button ────────────────────────────────
    # After selecting a row the page shows a "Terminal" button for that port.
    # Tried in order: role button → text selectors (ExtJS variants).
    print("🖥️ Clicking Terminal button...")
    terminal_clicked = False

    # Attempt 1: standard accessible role (works when rendered as <button>)
    try:
        terminal_btn = page.get_by_role("button", name="Terminal").first
        terminal_btn.wait_for(state="visible", timeout=6000)
        terminal_btn.scroll_into_view_if_needed()
        terminal_btn.click()
        print("✅ Terminal clicked (role=button)")
        terminal_clicked = True
        time.sleep(3)
    except Exception as e:
        print(f"   role=button miss: {e}")

    # Attempt 2: ExtJS / custom button elements
    if not terminal_clicked:
        for sel in [
            'button:has-text("Terminal")',
            'a:has-text("Terminal")',
            'span.x-btn-text:has-text("Terminal")',
            'td.x-btn-mc:has-text("Terminal")',
            'div.x-btn:has-text("Terminal")',
            '[class*="btn"]:has-text("Terminal")',
        ]:
            try:
                btn = page.locator(sel).first
                btn.wait_for(state="visible", timeout=4000)
                btn.scroll_into_view_if_needed()
                btn.click()
                print(f"✅ Terminal clicked via: {sel}")
                terminal_clicked = True
                time.sleep(3)
                break
            except Exception:
                continue

    if not terminal_clicked:
        print("❌ Could not click Terminal button with any selector")
        page.screenshot(path="terminal_error.png", full_page=True)
        print("📸 Error screenshot: terminal_error.png")

    # ── STEP 13: Full page screenshot — Terminal details ──────────────────
    print("⏳ Waiting for Terminal detail to load...")
    time.sleep(5)
    page.screenshot(path="hapag_vessel_terminal_result.png", full_page=True)
    print("📸 Full-page screenshot saved: hapag_vessel_terminal_result.png")
    page.pause()

    result = page.inner_text('body')
    print(f"\n📦 Page text (first 2000 chars):\n{result[:2000]}")


print("🌐 Bypassing Cloudflare...")
StealthyFetcher.fetch(
    "https://www.hapag-lloyd.com/en/online-business/track/track-by-container-solution.html",
    headless=False,
    solve_cloudflare=True,
    browser_type="chromium",
    executable_path=r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    block_webrtc=True,
    hide_canvas=True,
    network_idle=True,
    google_search=True,
    wait=3,
    page_action=full_action,
)
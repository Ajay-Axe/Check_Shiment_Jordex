# -*- coding: utf-8 -*-
"""Apply 4 targeted fixes to Check Shipment codebase."""

# ══════════════════════════════════════════════════
# Change 1: Tracking.py — Remove date overwrite in Hapag terminal
# ══════════════════════════════════════════════════

with open('Tracking.py', 'r', encoding='utf-8') as f:
    tracking = f.read()

old_hapag_date = '''                        if terminal_date:
                            m = _re.match(r'(\\d{4})-(\\d{2})-(\\d{2})', terminal_date)
                            if m:
                                terminal_date = f"{m.group(3)}-{m.group(2)}-{m.group(1)}"
                            if _re.match(r'\\d{2}-\\d{2}-\\d{4}', terminal_date):
                                final_result["eta"] = terminal_date
                                log.info("Hapag-Lloyd: ETA overwritten \xe2\x86\x90 '%s'", terminal_date)
                            else:
                                log.warning("Hapag-Lloyd: Skipping non-date value: '%s'", terminal_date)
                        else:
                            log.warning("Hapag-Lloyd: No terminal date found, ETA unchanged")'''

new_hapag_date = '''                        if terminal_date:
                            log.info("Hapag-Lloyd: Terminal date found '%s' — keeping existing ETA, not overwriting.", terminal_date)
                        else:
                            log.warning("Hapag-Lloyd: No terminal date found.")'''

# The active version (not commented out)
count = tracking.count(old_hapag_date)
if count >= 1:
    # Only replace the ACTIVE (non-commented) version
    tracking = tracking.replace(old_hapag_date, new_hapag_date, 1)
    print(f"Change 1: Replaced Hapag terminal date block ({count} occurrence(s) found, replaced 1)")
else:
    # Try alternate encoding of the arrow character
    old_alt = old_hapag_date.replace('\xe2\x86\x90', '\x1a')
    count2 = tracking.count(old_alt)
    if count2 >= 1:
        tracking = tracking.replace(old_alt, new_hapag_date, 1)
        print(f"Change 1: Replaced Hapag terminal date block (alt encoding, {count2} found)")
    else:
        # Try line-by-line search
        lines = tracking.split('\n')
        found = False
        for i, line in enumerate(lines):
            if 'final_result["eta"] = terminal_date' in line and '#' not in lines[i].lstrip()[:2]:
                # Found the active version - replace the block
                # Find start (if terminal_date:) and end (log.warning...No terminal date)
                start = i
                while start > 0 and 'if terminal_date:' not in lines[start]:
                    start -= 1
                end = i
                while end < len(lines) - 1 and 'No terminal date' not in lines[end]:
                    end += 1
                
                # Get indentation
                indent = '                        '
                replacement = [
                    indent + 'if terminal_date:',
                    indent + '    log.info("Hapag-Lloyd: Terminal date found \'%s\' — keeping existing ETA, not overwriting.", terminal_date)',
                    indent + 'else:',
                    indent + '    log.warning("Hapag-Lloyd: No terminal date found.")',
                ]
                lines[start:end+1] = replacement
                tracking = '\n'.join(lines)
                found = True
                print(f"Change 1: Replaced Hapag terminal date block (line-by-line, around line {start})")
                break
        if not found:
            print("Change 1: WARNING - Could not find Hapag terminal date block to replace")

with open('Tracking.py', 'w', encoding='utf-8') as f:
    f.write(tracking)

print("Change 1: Tracking.py saved")


# ══════════════════════════════════════════════════
# Changes 2-4: Shipment_Process.py
# ══════════════════════════════════════════════════

with open('Shipment_Process.py', 'r', encoding='utf-8') as f:
    sp = f.read()


# ── Change 2a: _compare_routing_string — skip Terminal if carrier_on_carriage ──

old_route_pairs = '''        route_pairs = [
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
            ("Terminal", route.get("Destination", {}).get("Terminal", ""),
             track.get("pod_terminal", "")),
        ]'''

new_route_pairs = '''        has_on_carriage = bool(track.get("carrier_on_carriage"))
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
            )'''

if old_route_pairs in sp:
    sp = sp.replace(old_route_pairs, new_route_pairs)
    print("Change 2a: _compare_routing_string Terminal skip applied")
else:
    print("Change 2a: WARNING - route_pairs block not found")


# ── Change 2b: _compare_routing_with_ai — skip Terminal if carrier_on_carriage ──

old_ai_terminal = '''6. Terminal: Jordex Destination.Terminal vs tracking pod_terminal'''
new_ai_terminal_block = '''6. Terminal: {terminal_rule}'''

old_ai_prompt_start = '''            prompt = f"""You are a logistics routing comparator.
Compare the Jordex routing data for container {cno} against the carrier tracking result.

JORDEX ROUTING DATA (from view_routing.json):
{json.dumps(route, indent=2)}

CARRIER TRACKING RESULT:
{json.dumps(track, indent=2, default=str)}'''

new_ai_prompt_start = '''            has_on_carriage = bool(track.get("carrier_on_carriage"))
            terminal_rule = "SKIP this field entirely — do NOT include Terminal in output at all, because carrier_on_carriage data exists and the actual final delivery point is the on-carriage destination, not the ocean terminal." if has_on_carriage else "Jordex Destination.Terminal vs tracking pod_terminal"

            prompt = f"""You are a logistics routing comparator.
Compare the Jordex routing data for container {cno} against the carrier tracking result.

JORDEX ROUTING DATA (from view_routing.json):
{json.dumps(route, indent=2)}

CARRIER TRACKING RESULT:
{json.dumps(track, indent=2, default=str)}'''

if old_ai_prompt_start in sp:
    sp = sp.replace(old_ai_prompt_start, new_ai_prompt_start, 1)
    print("Change 2b: AI prompt start replaced (added has_on_carriage)")
else:
    print("Change 2b: WARNING - AI prompt start not found")

if old_ai_terminal in sp:
    sp = sp.replace(old_ai_terminal, new_ai_terminal_block, 1)
    print("Change 2b: AI Terminal line replaced with dynamic terminal_rule")
else:
    print("Change 2b: WARNING - Terminal line not found in AI prompt")


# ── Change 3a: PACKAGE_TYPE_MAP — add CARTON(S) ──

old_pkg_map = '''    "CTN": "Carton", "CTNS": "Carton", "CARTONS": "Carton", "CARTON": "Carton",'''
new_pkg_map = '''    "CTN": "Carton", "CTNS": "Carton", "CARTONS": "Carton", "CARTON": "Carton", "CARTON(S)": "Carton",'''

if old_pkg_map in sp:
    sp = sp.replace(old_pkg_map, new_pkg_map)
    print("Change 3a: PACKAGE_TYPE_MAP updated")
else:
    print("Change 3a: WARNING - PACKAGE_TYPE_MAP line not found")


# ── Change 3b: _normalize_pkg_type — handle plural fallback ──

old_normalize = '''def _normalize_pkg_type(raw: str) -> str:
    if not raw:
        return "Package"
    v = str(raw).strip().upper()
    if v in PACKAGE_TYPE_MAP:
        return PACKAGE_TYPE_MAP[v]
    return v.title() if v else "Package"'''

new_normalize = '''def _normalize_pkg_type(raw: str) -> str:
    if not raw:
        return "Package"
    v = str(raw).strip().upper()
    if v in PACKAGE_TYPE_MAP:
        return PACKAGE_TYPE_MAP[v]
    # Strip trailing S for plural forms not in map
    if v.endswith("S") and v[:-1] in PACKAGE_TYPE_MAP:
        return PACKAGE_TYPE_MAP[v[:-1]]
    return v.title() if v else "Package"'''

if old_normalize in sp:
    sp = sp.replace(old_normalize, new_normalize)
    print("Change 3b: _normalize_pkg_type updated with plural fallback")
else:
    print("Change 3b: WARNING - _normalize_pkg_type not found")


# ── Change 3c: Package popup save — enhanced save with cargo detail save ──

old_popup_save = '''                        # Save popup
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
                            pass'''

new_popup_save = '''                        # Save package popup — try footer buttons first, then generic save
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
                        page.wait_for_timeout(1000)'''

count = sp.count(old_popup_save)
if count >= 1:
    sp = sp.replace(old_popup_save, new_popup_save, 1)
    print(f"Change 3c: Package popup save block replaced (first of {count})")
else:
    print("Change 3c: WARNING - Package popup save block not found")


# ── Change 4a: process_single_shipment — LCL container skip ──

old_container_check_1 = '''                containers = [c.get("Container_No", "").strip() for c in cargo_data if c.get("Container_No", "").strip()]
                if not containers:
                    missing.append("Container Number")'''

new_container_check_1 = '''                containers = [c.get("Container_No", "").strip() for c in cargo_data if c.get("Container_No", "").strip()]
                # Check load type — LCL shipments don't have container numbers
                is_lcl_shipment = any(
                    "LCL" in str(c.get("Load_Type", "")).upper()
                    for c in cargo_data
                )
                if not is_lcl_shipment and not containers:
                    missing.append("Container Number")'''

if old_container_check_1 in sp:
    sp = sp.replace(old_container_check_1, new_container_check_1, 1)
    print("Change 4a: process_single_shipment Container Number check updated for LCL")
else:
    print("Change 4a: WARNING - process_single_shipment container check not found")


# ── Change 4b: old_file_mode main() — LCL container skip ──

old_container_check_2 = '''                        if not any(c.get("Container_No", "").strip() for c in cg):
                            missing.append("Container Number")
                        if not cd.get("Carrier", "").strip():'''

new_container_check_2 = '''                        # Only check container number for FCL — LCL shipments don't have container numbers
                        is_lcl = any(
                            "LCL" in str(c.get("Load_Type", "")).upper()
                            for c in cg
                        )
                        if not is_lcl:
                            if not any(c.get("Container_No", "").strip() for c in cg):
                                missing.append("Container Number")
                        if not cd.get("Carrier", "").strip():'''

if old_container_check_2 in sp:
    sp = sp.replace(old_container_check_2, new_container_check_2, 1)
    print("Change 4b: old_file_mode Container Number check updated for LCL")
else:
    print("Change 4b: WARNING - old_file_mode container check not found")


with open('Shipment_Process.py', 'w', encoding='utf-8') as f:
    f.write(sp)

print("\nAll changes applied to Shipment_Process.py")
print("Done!")

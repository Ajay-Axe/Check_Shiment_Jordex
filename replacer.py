import os

filepath = r"f:\Axe Global\Check Shipment\Shipment_Process.py"

with open(filepath, "r", encoding="utf-8") as f:
    lines = f.readlines()

print(f"Total lines before: {len(lines)}")

# Remove lines 6989-7272 (1-indexed), which is index 6988-7271 (0-indexed)
start_remove = 6988  # 0-indexed
end_remove = 7272     # 0-indexed (exclusive), so line 7272 in 1-indexed

new_lines = lines[:start_remove] + lines[end_remove:]

print(f"Total lines after: {len(new_lines)}")

with open(filepath, "w", encoding="utf-8") as f:
    f.writelines(new_lines)

print("Done!")

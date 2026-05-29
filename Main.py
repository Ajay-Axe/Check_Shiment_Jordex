# """
# main.py — FastAPI Server for Jordex Shipment Verification
# =========================================================
# API layer connecting dashboard/documents UI to Shipment_Process engine.

# Routes:
#   /                          → Dashboard
#   /documents                 → Documents viewer
#   /carrier                   → Carrier tracking portal (standalone)

#   /api/orchestrator/start    → Start processing
#   /api/orchestrator/cancel   → Cancel processing
#   /api/orchestrator/status   → Get orchestrator state + logs

#   /api/settings              → GET/POST settings (ai_comparison, no_doc_mode, dates)

#   /api/shipments             → List processed shipment folders
#   /api/shipment/{folder}/comparison  → 4 comparison tables
#   /api/shipment/{folder}/files       → List files in folder
#   /api/shipment/{folder}/update      → POST: queue update for selected fields
#   /api/shipment/{folder}/complete    → POST: queue task completion
#   /api/shipment/{folder}/update-status → GET: poll update queue progress

#   /api/no-doc                → GET no_doc.json entries

#   /files/{folder}/{filename} → Serve PDFs, images, JSONs from Shipments/
# """

# import os
# import json
# import logging
# import threading
# from pathlib import Path
# from datetime import datetime

# from fastapi import FastAPI, HTTPException, Request
# from fastapi.responses import (
#     HTMLResponse, JSONResponse, FileResponse, Response,
# )
# from fastapi.staticfiles import StaticFiles
# from fastapi.templating import Jinja2Templates

# import Shipment_Process as sp

# # ═══════════════════════════════════════════════════════════════════════
# #  SETUP
# # ═══════════════════════════════════════════════════════════════════════

# logging.basicConfig(
#     level=logging.INFO,
#     format="%(asctime)s  %(levelname)-8s  %(message)s",
#     datefmt="%H:%M:%S",
# )
# log = logging.getLogger("main")

# app = FastAPI(title="Jordex Shipment Verification", version="5.0")

# BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# TEMPLATES_DIR = os.path.join(BASE_DIR, "templates")
# SHIPMENTS_DIR = sp.SHIPMENTS_ROOT

# Path(SHIPMENTS_DIR).mkdir(parents=True, exist_ok=True)
# Path(TEMPLATES_DIR).mkdir(parents=True, exist_ok=True)

# templates = Jinja2Templates(directory=TEMPLATES_DIR)

# # Orchestrator thread reference
# _orchestrator_thread = None
# _cancel_event = threading.Event()
# _log_buffer = []
# _log_lock = threading.Lock()
# MAX_LOG_LINES = 500


# def _add_log(msg: str):
#     with _log_lock:
#         ts = datetime.now().strftime("%H:%M:%S")
#         _log_buffer.append(f"[{ts}] {msg}")
#         if len(_log_buffer) > MAX_LOG_LINES:
#             _log_buffer.pop(0)
#     sp.orchestrator_state["logs"] = list(_log_buffer)


# # ═══════════════════════════════════════════════════════════════════════
# #  TEMPLATE ROUTES
# # ═══════════════════════════════════════════════════════════════════════

# @app.get("/", response_class=HTMLResponse)
# async def dashboard_page(request: Request):
#     return templates.TemplateResponse("dashboard.html", {"request": request})


# @app.get("/documents", response_class=HTMLResponse)
# async def documents_page(request: Request):
#     return templates.TemplateResponse("documents.html", {"request": request})


# @app.get("/carrier", response_class=HTMLResponse)
# async def carrier_page(request: Request):
#     return templates.TemplateResponse("index.html", {"request": request})


# # ═══════════════════════════════════════════════════════════════════════
# #  ORCHESTRATOR ENDPOINTS
# # ═══════════════════════════════════════════════════════════════════════

# @app.post("/api/orchestrator/start")
# async def orchestrator_start():
#     global _orchestrator_thread

#     if sp.orchestrator_state["is_running"]:
#         return {"status": "already_running"}

#     _cancel_event.clear()
#     with _log_lock:
#         _log_buffer.clear()

#     sp.orchestrator_state["status"] = "Starting"
#     sp.orchestrator_state["is_running"] = True

#     def run():
#         try:
#             _add_log("Orchestrator starting...")
#             sp.main(
#                 status_callback=_add_log,
#                 headless=False,
#                 cancel_event_ext=_cancel_event,
#             )
#             _add_log("Orchestrator finished.")
#         except KeyboardInterrupt:
#             _add_log("Orchestrator cancelled.")
#         except Exception as e:
#             _add_log(f"Orchestrator error: {e}")
#             log.error("Orchestrator error: %s", e, exc_info=True)
#         finally:
#             sp.orchestrator_state["is_running"] = False
#             sp.orchestrator_state["status"] = "Idle"

#     _orchestrator_thread = threading.Thread(target=run, daemon=True)
#     _orchestrator_thread.start()

#     return {"status": "started"}


# @app.post("/api/orchestrator/cancel")
# async def orchestrator_cancel():
#     if not sp.orchestrator_state["is_running"]:
#         return {"status": "not_running"}

#     _cancel_event.set()
#     sp.orchestrator_state["status"] = "Cancelling"
#     _add_log("Cancel requested.")
#     return {"status": "cancelling"}


# @app.get("/api/orchestrator/status")
# async def orchestrator_status():
#     checked = sp.load_checked()
#     no_doc = sp.load_no_doc()

#     # Count shipments with comparison results
#     processed = 0
#     if os.path.isdir(SHIPMENTS_DIR):
#         for folder in os.listdir(SHIPMENTS_DIR):
#             comp = os.path.join(SHIPMENTS_DIR, folder, "Comparison_Result.json")
#             if os.path.exists(comp):
#                 processed += 1

#     return {
#         "status": sp.orchestrator_state["status"],
#         "is_running": sp.orchestrator_state["is_running"],
#         "logs": list(_log_buffer),
#         "stats": {
#             "processed": processed,
#             "checked": len(checked),
#             "no_doc": len(no_doc),
#             "log_count": len(_log_buffer),
#         },
#     }


# # ═══════════════════════════════════════════════════════════════════════
# #  SETTINGS ENDPOINTS
# # ═══════════════════════════════════════════════════════════════════════

# @app.get("/api/settings")
# async def get_settings():
#     return sp.load_settings()


# @app.post("/api/settings")
# async def update_settings(request: Request):
#     body = await request.json()
#     allowed_keys = {"ai_comparison", "no_doc_mode", "date_start", "date_end"}
#     updates = {k: v for k, v in body.items() if k in allowed_keys}
#     sp.save_settings(updates)
#     _add_log(f"Settings updated: {updates}")
#     return {"status": "saved", "settings": sp.load_settings()}


# # ═══════════════════════════════════════════════════════════════════════
# #  SHIPMENT DATA ENDPOINTS
# # ═══════════════════════════════════════════════════════════════════════

# @app.get("/api/shipments")
# async def list_shipments():
#     """List all shipment folders with summary info."""
#     if not os.path.isdir(SHIPMENTS_DIR):
#         return {"shipments": []}

#     shipments = []
#     for folder in sorted(os.listdir(SHIPMENTS_DIR), reverse=True):
#         folder_path = os.path.join(SHIPMENTS_DIR, folder)
#         if not os.path.isdir(folder_path):
#             continue

#         comp_path = os.path.join(folder_path, "Comparison_Result.json")
#         has_comparison = os.path.exists(comp_path)

#         # Parse folder name: "9-May-2026__OI123456"
#         parts = folder.split("__", 1)
#         date_str = parts[0].replace("-", " ") if parts else folder
#         ref_no = parts[1] if len(parts) > 1 else ""

#         # Count mismatches
#         mismatch_count = 0
#         match_count = 0
#         if has_comparison:
#             try:
#                 with open(comp_path, "r") as f:
#                     comp = json.load(f)
#                 for section in ["Parties", "Carrier"]:
#                     for item in comp.get(section, []):
#                         if item.get("status") == "MISMATCH":
#                             mismatch_count += 1
#                         elif item.get("status") == "MATCH":
#                             match_count += 1
#                 for section in ["Cargo", "Routing"]:
#                     for container in comp.get(section, []):
#                         for field in container.get("fields", []):
#                             if field.get("status") == "MISMATCH":
#                                 mismatch_count += 1
#                             elif field.get("status") == "MATCH":
#                                 match_count += 1
#             except Exception:
#                 pass

#         # List files
#         files = []
#         for f in os.listdir(folder_path):
#             fpath = os.path.join(folder_path, f)
#             if os.path.isfile(fpath):
#                 files.append({
#                     "name": f,
#                     "size": os.path.getsize(fpath),
#                     "ext": os.path.splitext(f)[1].lower(),
#                 })

#         shipments.append({
#             "folder": folder,
#             "date": date_str,
#             "ref": ref_no,
#             "has_comparison": has_comparison,
#             "mismatch_count": mismatch_count,
#             "match_count": match_count,
#             "file_count": len(files),
#             "files": files,
#         })

#     return {"shipments": shipments}


# @app.get("/api/shipment/{folder}/comparison")
# async def get_comparison(folder: str):
#     """Return the 4 comparison tables for a shipment."""
#     comp_path = os.path.join(SHIPMENTS_DIR, folder, "Comparison_Result.json")
#     if not os.path.exists(comp_path):
#         raise HTTPException(404, "Comparison result not found")

#     with open(comp_path, "r", encoding="utf-8") as f:
#         comparison = json.load(f)

#     return comparison


# @app.get("/api/shipment/{folder}/files")
# async def list_shipment_files(folder: str):
#     """List all files in a shipment folder."""
#     folder_path = os.path.join(SHIPMENTS_DIR, folder)
#     if not os.path.isdir(folder_path):
#         raise HTTPException(404, "Folder not found")

#     files = []
#     for f in sorted(os.listdir(folder_path)):
#         fpath = os.path.join(folder_path, f)
#         if os.path.isfile(fpath):
#             files.append({
#                 "name": f,
#                 "size": os.path.getsize(fpath),
#                 "ext": os.path.splitext(f)[1].lower(),
#             })
#     return {"files": files}


# @app.get("/api/shipment/{folder}/json/{filename}")
# async def get_shipment_json(folder: str, filename: str):
#     """Serve a JSON file from a shipment folder."""
#     if not filename.endswith(".json"):
#         raise HTTPException(400, "Only JSON files")
#     fpath = os.path.join(SHIPMENTS_DIR, folder, filename)
#     if not os.path.exists(fpath):
#         raise HTTPException(404, "File not found")
#     with open(fpath, "r", encoding="utf-8") as f:
#         return json.load(f)


# # ═══════════════════════════════════════════════════════════════════════
# #  UPDATE + COMPLETE ENDPOINTS
# # ═══════════════════════════════════════════════════════════════════════

# @app.post("/api/shipment/{folder}/update")
# async def update_shipment(folder: str, request: Request):
#     """
#     Queue field updates for a shipment.
#     Body: { "fields": { "Carrier": {"Vessel_Name": "...", ...}, ... } }
#     """
#     body = await request.json()
#     fields = body.get("fields", {})

#     if not fields:
#         raise HTTPException(400, "No fields to update")

#     sp.queue_update(folder, fields, action="update")
#     _add_log(f"Update queued: {folder}")

#     return {"status": "queued", "folder": folder}


# @app.post("/api/shipment/{folder}/complete")
# async def complete_shipment(folder: str):
#     """Queue task completion for a shipment."""
#     sp.queue_update(folder, {}, action="complete")
#     _add_log(f"Complete queued: {folder}")
#     return {"status": "queued", "folder": folder}


# @app.get("/api/shipment/{folder}/update-status")
# async def get_shipment_update_status(folder: str):
#     """Poll update queue progress for a shipment."""
#     return sp.get_update_status(folder)


# # ═══════════════════════════════════════════════════════════════════════
# #  NO-DOC ENDPOINT
# # ═══════════════════════════════════════════════════════════════════════

# @app.get("/api/no-doc")
# async def get_no_doc():
#     return sp.load_no_doc()


# # ═══════════════════════════════════════════════════════════════════════
# #  FILE SERVING
# # ═══════════════════════════════════════════════════════════════════════

# @app.get("/files/{folder}/{filename}")
# async def serve_file(folder: str, filename: str):
#     """Serve files (PDFs, images, JSONs) from Shipments/."""
#     fpath = os.path.join(SHIPMENTS_DIR, folder, filename)
#     if not os.path.exists(fpath):
#         raise HTTPException(404, "File not found")

#     ext = os.path.splitext(filename)[1].lower()
#     media_types = {
#         ".pdf": "application/pdf",
#         ".json": "application/json",
#         ".png": "image/png",
#         ".jpg": "image/jpeg",
#         ".jpeg": "image/jpeg",
#         ".html": "text/html",
#     }
#     media_type = media_types.get(ext, "application/octet-stream")

#     return FileResponse(fpath, media_type=media_type, filename=filename)


# # ═══════════════════════════════════════════════════════════════════════
# #  CARRIER TRACKING API (standalone portal)
# # ═══════════════════════════════════════════════════════════════════════

# @app.post("/api/carrier/track")
# async def carrier_track(request: Request):
#     """Direct carrier tracking from the Carrier Portal page."""
#     try:
#         import Tracking
#     except ImportError:
#         raise HTTPException(500, "Tracking module not available")

#     body = await request.json()
#     carrier = body.get("carrier", "")
#     container = body.get("container", "")

#     if not carrier or not container:
#         raise HTTPException(400, "carrier and container required")

#     try:
#         result = Tracking.track_shipment_thread_safe(
#             carrier, container, save_dir=None
#         )
#         return {"status": "ok", "result": result}
#     except Exception as e:
#         log.error("Carrier track error: %s", e)
#         return {"status": "error", "error": str(e)}


# @app.get("/api/carrier/list")
# async def carrier_list():
#     """Return supported carriers for the tracking portal."""
#     return {
#         "carriers": [
#             "MSC", "MAERSK", "ONE", "HAPAG", "YANG MING",
#             "EVERGREEN", "COSCO", "HMM", "OOCL",
#         ]
#     }


# # ═══════════════════════════════════════════════════════════════════════
# #  STARTUP                                                              
# # ═══════════════════════════════════════════════════════════════════════

# @app.on_event("startup")
# async def startup():
#     log.info("Jordex Verification Server v5.0")
#     log.info("Shipments dir: %s", SHIPMENTS_DIR)
#     log.info("Templates dir: %s", TEMPLATES_DIR)
#     sp.load_settings()


# if __name__ == "__main__":
#     import uvicorn
#     uvicorn.run(
#         app,
#         host="0.0.0.0",
#         port=int(os.getenv("PORT", "8000")),
#     )

"""
main.py — FastAPI Server for Jordex Shipment Verification
=========================================================
API layer connecting dashboard/documents UI to Shipment_Process engine.

Routes:
  /                          → Dashboard
  /documents                 → Documents viewer
  /carrier                   → Carrier tracking portal (standalone)

  /api/orchestrator/start    → Start processing
  /api/orchestrator/cancel   → Cancel processing
  /api/orchestrator/status   → Get orchestrator state + logs

  /api/settings              → GET/POST settings (ai_comparison, no_doc_mode, dates)

  /api/shipments             → List processed shipment folders
  /api/shipment/{folder}/comparison  → 4 comparison tables
  /api/shipment/{folder}/files       → List files in folder
  /api/shipment/{folder}/update      → POST: queue update for selected fields
  /api/shipment/{folder}/complete    → POST: queue task completion
  /api/shipment/{folder}/update-status → GET: poll update queue progress

  /api/no-doc                → GET no_doc.json entries

  /files/{folder}/{filename} → Serve PDFs, images, JSONs from Shipments/
"""

import os
import json
import logging
import threading
from pathlib import Path
from datetime import datetime

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import (
    HTMLResponse, JSONResponse, FileResponse, Response,
)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

import Shipment_Process as sp

# ═══════════════════════════════════════════════════════════════════════
#  SETUP
# ═══════════════════════════════════════════════════════════════════════

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("main")

app = FastAPI(title="Jordex Shipment Verification", version="5.0")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TEMPLATES_DIR = os.path.join(BASE_DIR, "templates")
SHIPMENTS_DIR = sp.SHIPMENTS_ROOT

Path(SHIPMENTS_DIR).mkdir(parents=True, exist_ok=True)
Path(TEMPLATES_DIR).mkdir(parents=True, exist_ok=True)

templates = Jinja2Templates(directory=TEMPLATES_DIR)

# Orchestrator thread reference
_orchestrator_thread = None
_cancel_event = threading.Event()
_log_buffer = []
_log_lock = threading.Lock()
MAX_LOG_LINES = 500


def _add_log(msg: str):
    with _log_lock:
        ts = datetime.now().strftime("%H:%M:%S")
        _log_buffer.append(f"[{ts}] {msg}")
        if len(_log_buffer) > MAX_LOG_LINES:
            _log_buffer.pop(0)
    sp.orchestrator_state["logs"] = list(_log_buffer)


# ═══════════════════════════════════════════════════════════════════════
#  TEMPLATE ROUTES
# ═══════════════════════════════════════════════════════════════════════

@app.get("/", response_class=HTMLResponse)
async def dashboard_page(request: Request):
    return templates.TemplateResponse("dashboard.html", {"request": request})


@app.get("/documents", response_class=HTMLResponse)
async def documents_page(request: Request):
    return templates.TemplateResponse("documents.html", {"request": request})


@app.get("/carrier", response_class=HTMLResponse)
async def carrier_page(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})


# ═══════════════════════════════════════════════════════════════════════
#  ORCHESTRATOR ENDPOINTS
# ═══════════════════════════════════════════════════════════════════════

@app.post("/api/orchestrator/start")
async def orchestrator_start():
    global _orchestrator_thread

    if sp.orchestrator_state["is_running"]:
        return {"status": "already_running"}

    _cancel_event.clear()
    with _log_lock:
        _log_buffer.clear()

    sp.orchestrator_state["status"] = "Starting"
    sp.orchestrator_state["is_running"] = True

    def run():
        try:
            _add_log("Orchestrator starting...")
            sp.main(
                status_callback=_add_log,
                headless=False,
                cancel_event_ext=_cancel_event,
            )
            _add_log("Orchestrator finished.")
        except KeyboardInterrupt:
            _add_log("Orchestrator cancelled.")
        except Exception as e:
            _add_log(f"Orchestrator error: {e}")
            log.error("Orchestrator error: %s", e, exc_info=True)
        finally:
            sp.orchestrator_state["is_running"] = False
            sp.orchestrator_state["status"] = "Idle"

    _orchestrator_thread = threading.Thread(target=run, daemon=True)
    _orchestrator_thread.start()

    return {"status": "started"}


@app.post("/api/orchestrator/cancel")
async def orchestrator_cancel():
    if not sp.orchestrator_state["is_running"]:
        return {"status": "not_running"}

    _cancel_event.set()
    sp.orchestrator_state["status"] = "Cancelling"
    _add_log("Cancel requested.")
    return {"status": "cancelling"}


@app.get("/api/orchestrator/status")
async def orchestrator_status():
    checked = sp.load_checked()
    no_doc = sp.load_no_doc()

    # Count shipments with comparison results
    processed = 0
    if os.path.isdir(SHIPMENTS_DIR):
        for folder in os.listdir(SHIPMENTS_DIR):
            comp = os.path.join(SHIPMENTS_DIR, folder, "Comparison_Result.json")
            if os.path.exists(comp):
                processed += 1

    return {
        "status": sp.orchestrator_state["status"],
        "is_running": sp.orchestrator_state["is_running"],
        "logs": list(_log_buffer),
        "stats": {
            "processed": processed,
            "checked": len(checked),
            "no_doc": len(no_doc),
            "log_count": len(_log_buffer),
        },
    }


# ═══════════════════════════════════════════════════════════════════════
#  SETTINGS ENDPOINTS
# ═══════════════════════════════════════════════════════════════════════

@app.get("/api/settings")
async def get_settings():
    return sp.load_settings()


@app.post("/api/settings")
async def update_settings(request: Request):
    body = await request.json()
    allowed_keys = {"ai_comparison", "no_doc_mode", "old_file_mode", "date_start", "date_end"}
    updates = {k: v for k, v in body.items() if k in allowed_keys}
    sp.save_settings(updates)
    _add_log(f"Settings updated: {updates}")
    return {"status": "saved", "settings": sp.load_settings()}


# ═══════════════════════════════════════════════════════════════════════
#  SHIPMENT DATA ENDPOINTS
# ═══════════════════════════════════════════════════════════════════════

@app.get("/api/old-doc-items")
async def list_old_doc_items():
    if not os.path.isdir(SHIPMENTS_DIR):
        return {"items": []}
        
    items = []
    for folder in sorted(os.listdir(SHIPMENTS_DIR), reverse=True):
        folder_path = os.path.join(SHIPMENTS_DIR, folder)
        if not os.path.isdir(folder_path):
            continue
            
        res_path = os.path.join(folder_path, "result.json")
        if os.path.exists(res_path):
            try:
                with open(res_path, "r", encoding="utf-8") as f:
                    res_data = json.load(f)
                if res_data.get("status") in ["pending_old_doc_support", "completed_old_doc_support"]:
                    items.append({
                        "folder": folder,
                        "ref": res_data.get("ref_no", ""),
                        "date": res_data.get("date_str", ""),
                        "comment": res_data.get("comment", ""),
                        "status": res_data.get("status")
                    })
            except Exception:
                pass
    return {"items": items}

@app.post("/api/old-doc-submit-all")
async def submit_all_old_docs(request: Request):
    body = await request.json()
    selected_folders = body.get("folders", [])
    if not selected_folders:
        raise HTTPException(400, "No folders selected")
        
    sp.queue_update("__old_doc_batch__", {"selected_folders": selected_folders}, action="old_doc_batch")
    _add_log("Old Doc Support batch submission queued")
    return {"status": "queued", "folder": "__old_doc_batch__"}

@app.get("/api/shipments")
async def list_shipments():
    """List all shipment folders with summary info."""
    if not os.path.isdir(SHIPMENTS_DIR):
        return {"shipments": []}

    shipments = []
    folders = [f for f in os.listdir(SHIPMENTS_DIR) if os.path.isdir(os.path.join(SHIPMENTS_DIR, f))]
    folders.sort(key=lambda x: os.path.getctime(os.path.join(SHIPMENTS_DIR, x)), reverse=True)
    
    for folder in folders:
        folder_path = os.path.join(SHIPMENTS_DIR, folder)

        comp_path = os.path.join(folder_path, "Comparison_Result.json")
        has_comparison = os.path.exists(comp_path)

        # Parse folder name: "9-May-2026__OI123456"
        parts = folder.split("__", 1)
        date_str = parts[0].replace("-", " ") if parts else folder
        ref_no = parts[1] if len(parts) > 1 else ""

        # Count mismatches
        mismatch_count = 0
        match_count = 0
        if has_comparison:
            try:
                with open(comp_path, "r") as f:
                    comp = json.load(f)
                for section in ["Parties", "Carrier"]:
                    for item in comp.get(section, []):
                        if item.get("status") == "MISMATCH":
                            mismatch_count += 1
                        elif item.get("status") == "MATCH":
                            match_count += 1
                for section in ["Cargo", "Routing"]:
                    for container in comp.get(section, []):
                        for field in container.get("fields", []):
                            if field.get("status") == "MISMATCH":
                                mismatch_count += 1
                            elif field.get("status") == "MATCH":
                                match_count += 1
            except Exception:
                pass

        # List files
        files = []
        for f in os.listdir(folder_path):
            fpath = os.path.join(folder_path, f)
            if os.path.isfile(fpath):
                files.append({
                    "name": f,
                    "size": os.path.getsize(fpath),
                    "ext": os.path.splitext(f)[1].lower(),
                })

        shipments.append({
            "folder": folder,
            "date": date_str,
            "ref": ref_no,
            "has_comparison": has_comparison,
            "mismatch_count": mismatch_count,
            "match_count": match_count,
            "file_count": len(files),
            "files": files,
        })

    return {"shipments": shipments}


@app.get("/api/shipment/{folder}/comparison")
async def get_comparison(folder: str):
    """Return the 4 comparison tables for a shipment."""
    comp_path = os.path.join(SHIPMENTS_DIR, folder, "Comparison_Result.json")
    if not os.path.exists(comp_path):
        raise HTTPException(404, "Comparison result not found")

    with open(comp_path, "r", encoding="utf-8") as f:
        comparison = json.load(f)

    return comparison
@app.post("/api/shipment/{folder}/re-extract")
async def re_extract_shipment(folder: str):
    """Re-run AI extraction on PDFs."""
    try:
        new_extraction = sp.re_extract_shipment_documents(folder)
        return {"success": True, "extraction": new_extraction}
    except FileNotFoundError as e:
        raise HTTPException(404, str(e))
    except Exception as e:
        import traceback
        traceback.print_exc()
        raise HTTPException(500, f"Re-extraction failed: {e}")



@app.post("/api/shipment/{folder}/recompare")
async def recompare_shipment(folder: str):
    """Re-run AI comparison on demand."""
    try:
        new_comparison = sp.recompare_shipment_with_ai(folder)
        return {"success": True, "comparison": new_comparison}
    except FileNotFoundError as e:
        raise HTTPException(404, str(e))
    except Exception as e:
        import traceback
        traceback.print_exc()
        raise HTTPException(500, f"Re-comparison failed: {e}")


@app.post("/api/shipment/{folder}/save-comparison")
async def save_comparison(folder: str, request: Request):
    """
    Apply manual edits to Comparison_Result.json and re-calculate match statuses.
    """
    comp_path = os.path.join(SHIPMENTS_DIR, folder, "Comparison_Result.json")
    if not os.path.exists(comp_path):
        raise HTTPException(404, "Comparison result not found")

    try:
        body = await request.json()
        edits = body.get("edits", [])
    except Exception as e:
        raise HTTPException(400, f"Invalid JSON body: {e}")

    try:
        with open(comp_path, "r", encoding="utf-8") as f:
            comparison = json.load(f)
    except Exception as e:
        raise HTTPException(500, f"Failed to load Comparison_Result.json: {e}")

    # Process each edit
    for edit in edits:
        tab = edit.get("tab")
        field = edit.get("field")
        container_no = edit.get("container", "")
        new_val = edit.get("value", "")

        if tab == "Parties":
            for item in comparison.get("Parties", []):
                if item.get("field") == field:
                    item["document_value"] = new_val
                    jdx_val = item.get("jordex_value", "")
                    item["status"] = sp._field_status(jdx_val, new_val)
                    break

        elif tab == "Carrier":
            for item in comparison.get("Carrier", []):
                if item.get("field") == field:
                    item["document_value"] = new_val
                    jdx_val = item.get("jordex_value", "")
                    if field == "Vessel_Name":
                        if not jdx_val or sp._is_empty(jdx_val):
                            status = "MATCH" if new_val == "No tracking result" or sp._is_empty(new_val) else "ADDED"
                        elif new_val == "No tracking result":
                            status = "MISMATCH"
                        elif jdx_val and new_val:
                            j_vessel = sp._strip_vessel_imo(jdx_val).upper().strip()
                            d_vessel = sp._strip_vessel_imo(new_val).upper().strip()
                            if j_vessel == d_vessel or sp._norm(j_vessel) == sp._norm(d_vessel):
                                status = "MATCH"
                            else:
                                status = "MISMATCH"
                        else:
                            status = "MISMATCH"
                        item["status"] = status
                    elif field == "Carrier":
                        if jdx_val and new_val:
                            j_norm = sp._normalize_carrier_name(jdx_val)
                            d_norm = sp._normalize_carrier_name(new_val)
                            status = "MATCH" if j_norm == d_norm else "MISMATCH"
                        elif not jdx_val and new_val:
                            status = "ADDED"
                        else:
                            status = "MATCH"
                        item["status"] = status
                    elif field == "MBL_Number":
                        if jdx_val and new_val:
                            j_clean = jdx_val.replace(" ", "")
                            d_clean = new_val.replace(" ", "")
                            if j_clean == d_clean or j_clean.endswith(d_clean) or d_clean.endswith(j_clean):
                                status = "MATCH"
                            else:
                                status = "MISMATCH"
                        else:
                            status = sp._field_status(jdx_val, new_val)
                        item["status"] = status
                    elif field in ("MBL_Type", "HBL_Type"):
                        j_norm = "ORIGINAL" if "ORIGINAL" in (jdx_val or "").upper() else "SEA WAYBILL" if "WAYBILL" in (jdx_val or "").upper() else jdx_val
                        d_norm = "ORIGINAL" if "ORIGINAL" in (new_val or "").upper() else "SEA WAYBILL" if "WAYBILL" in (new_val or "").upper() else new_val
                        status = "MATCH" if sp._norm(j_norm) == sp._norm(d_norm) else "MISMATCH"
                        item["status"] = status
                    else:
                        item["status"] = sp._field_status(jdx_val, new_val)
                    break

        elif tab == "Cargo":
            for c_entry in comparison.get("Cargo", []):
                if c_entry.get("container") == container_no:
                    for item in c_entry.get("fields", []):
                        if item.get("field") == field:
                            item["document_value"] = new_val
                            jdx_val = item.get("jordex_value", "")
                            
                            jdx_norm_val = jdx_val
                            new_norm_val = new_val
                            if field == "Container_Type":
                                jdx_norm_val = sp._normalize_container_type(jdx_val)
                                new_norm_val = sp._normalize_container_type(new_val)
                            elif field == "Package_Type":
                                jdx_norm_val = sp._normalize_pkg_type(jdx_val)
                                new_norm_val = sp._normalize_pkg_type(new_val)
                                
                            is_num = field in ("Total_Gross_Weight", "Volume", "Qty")
                            is_goods = field == "Goods_Description"
                            item["status"] = sp._field_status(jdx_norm_val, new_norm_val, is_numeric=is_num, is_goods=is_goods)
                            break
                    break

        elif tab == "Routing":
            for r_entry in comparison.get("Routing", []):
                if r_entry.get("container") == container_no:
                    for item in r_entry.get("fields", []):
                        if item.get("field") == field:
                            item["document_value"] = new_val
                            jdx_val = item.get("jordex_value", "")
                            item["status"] = sp._field_status(jdx_val, new_val)
                            break
                    break

    # Recompute is_oocl flag
    jdx_carrier = ""
    doc_carrier = ""
    for item in comparison.get("Carrier", []):
        if item.get("field") == "Carrier":
            jdx_carrier = item.get("jordex_value", "")
            doc_carrier = item.get("document_value", "")
            break
    j_c = sp._normalize_carrier_name(jdx_carrier)
    d_c = sp._normalize_carrier_name(doc_carrier)
    comparison["is_oocl"] = (j_c in ["OOCL", "CMACGM", "CMA", "CMA CGM"] or d_c in ["OOCL", "CMACGM", "CMA", "CMA CGM"])

    # Save to disk
    try:
        with open(comp_path, "w", encoding="utf-8") as f:
            json.dump(comparison, f, indent=2, ensure_ascii=False)
    except Exception as e:
        raise HTTPException(500, f"Failed to save updated Comparison_Result.json: {e}")

    return {"status": "success", "comparison": comparison}


@app.get("/api/shipment/{folder}/files")
async def list_shipment_files(folder: str):
    """List all files in a shipment folder."""
    folder_path = os.path.join(SHIPMENTS_DIR, folder)
    if not os.path.isdir(folder_path):
        raise HTTPException(404, "Folder not found")

    files = []
    for f in sorted(os.listdir(folder_path)):
        fpath = os.path.join(folder_path, f)
        if os.path.isfile(fpath):
            files.append({
                "name": f,
                "size": os.path.getsize(fpath),
                "ext": os.path.splitext(f)[1].lower(),
            })
    return {"files": files}


@app.get("/api/shipment/{folder}/json/{filename}")
async def get_shipment_json(folder: str, filename: str):
    """Serve a JSON file from a shipment folder."""
    if not filename.endswith(".json"):
        raise HTTPException(400, "Only JSON files")
    fpath = os.path.join(SHIPMENTS_DIR, folder, filename)
    if not os.path.exists(fpath):
        raise HTTPException(404, "File not found")
    with open(fpath, "r", encoding="utf-8") as f:
        return json.load(f)


# ═══════════════════════════════════════════════════════════════════════
#  UPDATE + COMPLETE ENDPOINTS
# ═══════════════════════════════════════════════════════════════════════

@app.post("/api/shipment/{folder}/update")
async def update_shipment(folder: str, request: Request):
    """
    Queue field updates for a shipment.
    Body: { "fields": { "Carrier": {"Vessel_Name": "...", ...}, ... } }
    """
    body = await request.json()
    fields = body.get("fields", {})

    if not fields:
        raise HTTPException(400, "No fields to update")

    sp.queue_update(folder, fields, action="update")
    _add_log(f"Update queued: {folder}")

    return {"status": "queued", "folder": folder}


@app.post("/api/shipment/{folder}/complete")
async def complete_shipment(folder: str):
    """Queue task completion for a shipment."""
    sp.queue_update(folder, {}, action="complete")
    _add_log(f"Complete queued: {folder}")
    return {"status": "queued", "folder": folder}


@app.get("/api/shipment/{folder}/update-status")
async def get_shipment_update_status(folder: str):
    """Poll update queue progress for a shipment."""
    return sp.get_update_status(folder)

# ═══════════════════════════════════════════════════════════════════════
#  QUEUE ENDPOINTS
# ═══════════════════════════════════════════════════════════════════════

@app.get("/api/queue")
async def get_queue():
    """Return the current queue."""
    return sp.load_queue()

@app.post("/api/queue/add")
async def add_to_queue(request: Request):
    body = await request.json()
    folder = body.get("folder")
    fields = body.get("fields", {})
    if not folder:
        raise HTTPException(400, "Missing folder")
    sp.add_to_queue(folder, fields)
    return {"status": "success", "queue": sp.load_queue()}

@app.post("/api/queue/remove")
async def remove_from_queue(request: Request):
    body = await request.json()
    folder = body.get("folder")
    if not folder:
        raise HTTPException(400, "Missing folder")
    sp.remove_from_queue(folder)
    return {"status": "success", "queue": sp.load_queue()}

@app.post("/api/queue/submit")
async def submit_queue():
    """Submit all queued items for update_and_complete."""
    queue_data = sp.load_queue()
    if not queue_data:
        return {"status": "empty", "message": "Queue is empty"}
        
    for folder, data in queue_data.items():
        fields = data.get("fields", {})
        sp.queue_update(folder, fields, action="update_and_complete")
        _add_log(f"Queue submit queued: {folder}")
        sp.remove_from_queue(folder)
        
    return {"status": "queued", "message": f"Queued {len(queue_data)} items for submission"}


# ═══════════════════════════════════════════════════════════════════════
#  MFA STATUS ENDPOINT
# ═══════════════════════════════════════════════════════════════════════

@app.get("/api/mfa/status")
async def mfa_status():
    """Return current MFA state (number + screenshot) for UI popup."""
    try:
        from Login import get_mfa_state
        return get_mfa_state()
    except ImportError:
        return {"active": False, "number": None, "screenshot_b64": None}


# ═══════════════════════════════════════════════════════════════════════
#  NO-DOC ENDPOINT
# ═══════════════════════════════════════════════════════════════════════

@app.get("/api/no-doc")
async def get_no_doc():
    return sp.load_no_doc()


# ═══════════════════════════════════════════════════════════════════════
#  FILE SERVING
# ═══════════════════════════════════════════════════════════════════════

@app.get("/files/{folder}/{filename}")
async def serve_file(folder: str, filename: str):
    """Serve files (PDFs, images, JSONs) from Shipments/ — inline, not download."""
    fpath = os.path.join(SHIPMENTS_DIR, folder, filename)
    if not os.path.exists(fpath):
        raise HTTPException(404, "File not found")

    ext = os.path.splitext(filename)[1].lower()
    media_types = {
        ".pdf":  "application/pdf",
        ".json": "application/json",
        ".png":  "image/png",
        ".jpg":  "image/jpeg",
        ".jpeg": "image/jpeg",
        ".html": "text/html",
    }
    media_type = media_types.get(ext, "application/octet-stream")

    # Read file bytes and return with inline disposition
    with open(fpath, "rb") as f:
        content = f.read()

    from fastapi.responses import Response
    return Response(
        content=content,
        media_type=media_type,
        headers={
            "Content-Disposition": f"inline; filename=\"{filename}\"",
            "Cache-Control": "private, max-age=3600",
        }
    )


# ═══════════════════════════════════════════════════════════════════════
#  CARRIER TRACKING API (standalone portal)
# ═══════════════════════════════════════════════════════════════════════

@app.post("/api/carrier/track")
async def carrier_track(request: Request):
    """Direct carrier tracking from the Carrier Portal page."""
    try:
        import Tracking
    except ImportError:
        raise HTTPException(500, "Tracking module not available")

    body = await request.json()
    carrier = body.get("carrier", "")
    container = body.get("container", "")

    if not carrier or not container:
        raise HTTPException(400, "carrier and container required")

    try:
        result = Tracking.track_shipment_thread_safe(
            carrier, container, save_dir=None
        )
        return {"status": "ok", "result": result}
    except Exception as e:
        log.error("Carrier track error: %s", e)
        return {"status": "error", "error": str(e)}


@app.get("/api/carrier/list")
async def carrier_list():
    """Return supported carriers for the tracking portal."""
    return {
        "carriers": [
            "MSC", "MAERSK", "ONE", "HAPAG", "YANG MING",
            "EVERGREEN", "COSCO", "HMM", "OOCL",
        ]
    }

@app.post("/api/queue/update-only")
async def queue_update_only():
    """Submit all queue items as UPDATE only (no status change)."""
    q = sp.load_queue()
    if not q:
        return {"status": "empty", "message": "Queue is empty"}
    for folder, item in q.items():
        sp.queue_update(folder, item.get("fields", {}), action="update")
    sp.save_queue({})
    return {"status": "queued", "message": f"{len(q)} items queued for update-only"}


@app.post("/api/queue/submit-complete")
async def queue_submit_complete():
    """Submit all queue items as UPDATE + COMPLETE."""
    q = sp.load_queue()
    if not q:
        return {"status": "empty", "message": "Queue is empty"}
    for folder, item in q.items():
        sp.queue_update(folder, item.get("fields", {}), action="update_and_complete")
    sp.save_queue({})
    return {"status": "queued", "message": f"{len(q)} items queued for update+complete"}



@app.get("/api/pool/status")
async def pool_status():
    """Return current browser pool slot states."""
    slots = []
    for slot in sp._browser_pool._slots:
        slots.append({
            "id": slot["id"] + 1,
            "session_dir": slot["session_dir"],
            "busy": slot["busy"],
            "alive": sp._browser_pool._is_page_alive(slot["page"]),
        })
    return {"slots": slots}

# ═══════════════════════════════════════════════════════════════════════
#  STARTUP
# ═══════════════════════════════════════════════════════════════════════
@app.on_event("startup")
async def startup():
    log.info("Jordex Verification Server v5.0")
    log.info("Shipments dir: %s", SHIPMENTS_DIR)
    log.info("Templates dir: %s", TEMPLATES_DIR)
    sp.load_settings()
    for sdir in sp.SESSION_DIRS:
        Path(sdir).mkdir(parents=True, exist_ok=True)
    log.info("Session dirs initialized: %d", len(sp.SESSION_DIRS))
    
@app.on_event("shutdown")
async def shutdown():
    log.info("Shutting down browser pool...")
    sp._browser_pool.shutdown()
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=int(os.getenv("PORT", "8000")),
        access_log=False
    )

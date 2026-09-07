"""web.server

FastAPI + uvicorn front-end for the faceproof pipeline. Streams
faceproof.pipeline.run_pipeline() over Server-Sent Events so the browser UI
(web/static/*) can render the same 4-stage progress
the CLI prints.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import uuid
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, Query, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from faceproof.pipeline import run_pipeline
from faceproof.types import ChainError
from faceproof.canonical import payload_hash
from faceproof.chain import verify_record

BASE_DIR = Path(__file__).resolve().parent.parent
OUT_DIR = BASE_DIR / "out"
UPLOADS_DIR = OUT_DIR / "uploads"
STATIC_DIR = Path(__file__).resolve().parent / "static"

OUT_DIR.mkdir(parents=True, exist_ok=True)
UPLOADS_DIR.mkdir(parents=True, exist_ok=True)

# Chain the dashboard seals to. `faceproof serve --network X` sets this
# via the environment; it defaults to the in-process chain so the UI
# still works with no wallet, no RPC and no funds.
DEFAULT_NETWORK = os.environ.get("FACEPROOF_NETWORK", "local")

app = FastAPI(title="faceproof")


@app.on_event("startup")
def _warm_up() -> None:
    """Load the face models and compile the contract before serving.

    Both are process-wide one-time costs (~4s for the torch weights, plus
    solc on a cold cache). Paying them at startup keeps them out of the
    first /api/run, so a request measures the pipeline rather than the
    import. Failures are non-fatal: the request path raises its own errors
    with better context.
    """
    try:
        from faceproof.detect import _get_models

        _get_models()
    except Exception as exc:  # noqa: BLE001
        print(f"[faceproof.server] face model warmup skipped: {exc}")

    try:
        from faceproof.chain import compile_contract

        compile_contract()
    except Exception as exc:  # noqa: BLE001
        print(f"[faceproof.server] contract warmup skipped: {exc}")

# Permissive CORS for localhost (the browser UI is served from this same
# process, but keep this open so a dev server on a different port also works).
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"http://(localhost|127\.0\.0\.1)(:\d+)?",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/")
def index():
    index_path = STATIC_DIR / "index.html"
    if not index_path.exists():
        raise HTTPException(status_code=404, detail="web/static/index.html not found")
    return FileResponse(str(index_path))


# ---------------------------------------------------------------------------
# POST /api/upload
# ---------------------------------------------------------------------------
@app.post("/api/upload")
async def api_upload(file: UploadFile = File(...)):
    ext = Path(file.filename or "").suffix.lower() or ".bin"
    stem = uuid.uuid4().hex
    filename = f"{stem}{ext}"
    dest = UPLOADS_DIR / filename

    with dest.open("wb") as out:
        shutil.copyfileobj(file.file, out)
    await file.close()

    return {"image_id": filename, "url": f"/out/uploads/{filename}"}


def _resolve_upload(image_id: str) -> Path:
    """Resolve an image_id (with or without extension) to an uploaded file."""
    direct = UPLOADS_DIR / image_id
    if direct.exists():
        return direct
    matches = sorted(UPLOADS_DIR.glob(f"{image_id}.*"))
    if matches:
        return matches[0]
    raise HTTPException(status_code=404, detail=f"Unknown image id: {image_id}")


# ---------------------------------------------------------------------------
# GET /api/run  (Server-Sent Events)
# ---------------------------------------------------------------------------
def _sse_event(name: str, payload: dict) -> str:
    return f"event: {name}\ndata: {json.dumps(payload, default=str)}\n\n"


@app.get("/api/run")
async def api_run(
    image: str = Query(..., description="image_id returned by /api/upload"),
    network: str = Query(DEFAULT_NETWORK),
    threshold: float = Query(0.60),
):
    image_path = _resolve_upload(image)

    def event_stream():
        yield ": connected\n\n"
        for event_name, payload in run_pipeline(
            image_path=str(image_path),
            network=network,
            threshold=threshold,
            out_dir=str(OUT_DIR),
        ):
            yield _sse_event(event_name, payload)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# ---------------------------------------------------------------------------
# POST /api/verify
# ---------------------------------------------------------------------------
def _read_record_and_receipt():
    record_path = OUT_DIR / "record.json"
    receipt_path = OUT_DIR / "receipt.json"
    if not record_path.exists() or not receipt_path.exists():
        raise HTTPException(
            status_code=404,
            detail="out/record.json / out/receipt.json not found; run the pipeline first",
        )
    record = json.loads(record_path.read_text(encoding="utf-8"))
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    return record, receipt


@app.post("/api/verify")
async def api_verify():
    record, receipt = _read_record_and_receipt()
    local_hash = payload_hash(record)
    try:
        ok, onchain = verify_record(
            local_hash,
            network=receipt.get("network", "local"),
            contract_address=receipt.get("contract_address"),
            retries=4,
        )
    except ChainError as exc:
        raise HTTPException(status_code=502, detail=f"chain lookup failed: {exc}")

    return {"pass": ok, "local_hash": local_hash, "onchain": onchain}


# ---------------------------------------------------------------------------
# POST /api/tamper
# ---------------------------------------------------------------------------
def _mutate_one_char(raw: str):
    candidates = [m.start() for m in re.finditer(r"[0-9a-fA-F]", raw)]
    if not candidates:
        candidates = [m.start() for m in re.finditer(r"[A-Za-z0-9]", raw)]
    if not candidates:
        idx = len(raw) // 2
        return raw[:idx] + "X" + raw[idx + 1 :], idx
    idx = candidates[len(candidates) // 2]
    ch = raw[idx]
    swap = {"0": "1", "1": "0"}
    new_ch = swap.get(ch, "0" if ch != "0" else "1")
    return raw[:idx] + new_ch + raw[idx + 1 :], idx


@app.post("/api/tamper")
async def api_tamper():
    record_path = OUT_DIR / "record.json"
    receipt_path = OUT_DIR / "receipt.json"
    if not record_path.exists() or not receipt_path.exists():
        raise HTTPException(
            status_code=404,
            detail="out/record.json / out/receipt.json not found; run the pipeline first",
        )

    raw = record_path.read_text(encoding="utf-8")
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))

    tampered_raw, diff_index = _mutate_one_char(raw)
    tampered_path = OUT_DIR / "record.tampered.json"
    tampered_path.write_text(tampered_raw, encoding="utf-8")

    tampered_record = json.loads(tampered_raw)
    local_hash = payload_hash(tampered_record)
    try:
        ok, onchain = verify_record(
            local_hash,
            network=receipt.get("network", "local"),
            contract_address=receipt.get("contract_address"),
        )
    except ChainError as exc:
        raise HTTPException(status_code=502, detail=f"chain lookup failed: {exc}")

    onchain_hash = onchain.get("payload_hash") if isinstance(onchain, dict) else None

    return {
        "pass": ok,
        "local_hash": local_hash,
        "onchain_hash": onchain_hash,
        "diff_index": diff_index,
    }


# ---------------------------------------------------------------------------
# Static mounts: web/static (the dashboard) and out/ (crop images,
# thumbnails, JSON audit trails referenced by crop_url / image_url fields).
# ---------------------------------------------------------------------------
if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
app.mount("/out", StaticFiles(directory=str(OUT_DIR)), name="out")

"""faceproof.pipeline

Orchestrates the full faceproof flow: face detect -> public upload ->
reverse image search -> face-similarity verify -> blockchain seal.

`run_pipeline` is a GENERATOR yielding (event_name, payload) tuples so both
the CLI (faceproof.cli) and the SSE web server (web.server) consume the
exact same code path — no logic duplication, no drift between the two
front-ends.
"""

from __future__ import annotations

import dataclasses
import json
import os
from typing import Any, Dict, Iterator, Tuple

from faceproof.types import (
    ChainError,
    FaceProofError,
    FaceRecord,
    NoCandidatesFound,
    NoFaceDetected,
    NoVerifiedMatch,
    Receipt,
)
from faceproof.detect import detect_and_encode
from faceproof.upload import upload_public
from faceproof.search import load_api_key, reverse_image_search
from faceproof.verify_match import best_score, verify_candidates
from faceproof.chain import record_match

Event = Tuple[str, Dict[str, Any]]


def _asdict(obj: Any) -> dict:
    """Best-effort conversion of a dataclass/namedtuple/dict to a plain dict."""
    if dataclasses.is_dataclass(obj):
        return dataclasses.asdict(obj)
    if isinstance(obj, dict):
        return dict(obj)
    if hasattr(obj, "_asdict"):
        return dict(obj._asdict())
    raise TypeError(f"Cannot convert {type(obj)!r} to a plain dict")


def run_pipeline(
    image_path: str,
    network: str = "local",
    threshold: float = 0.60,
    out_dir: str = "out",
    social_only: bool = True,
    dry_run: bool = False,
) -> Iterator[Event]:
    """Run the full faceproof pipeline, yielding progress events.

    Stage order:
      1. scan  - detect_and_encode(image_path)
      2. search - upload_public(original) + upload_public(crop),
                  reverse_image_search([...both urls...])
      3. verify - verify_candidates(candidates, query_embedding)
      4. seal   - record_match(winning_match, embedding)  (skipped if dry_run)

    Writes out/record.json (asdict of the winning Match) as soon as a match
    is verified, and out/receipt.json (asdict of the Receipt) once the chain
    write succeeds. Never fabricates a result to keep the pipeline "alive" --
    any failure yields a terminal ("error", {...}) event and returns.
    """
    os.makedirs(out_dir, exist_ok=True)

    # ---- Stage 1: scan (face detection + embedding) ----------------------
    yield ("stage", {"n": 1, "name": "scan", "status": "running"})
    try:
        face_record: FaceRecord = detect_and_encode(image_path, out_dir=out_dir)
    except NoFaceDetected as exc:
        yield ("stage", {"n": 1, "name": "scan", "status": "fail"})
        yield ("error", {"message": str(exc), "stage": "scan", "exit_code": 2})
        return
    except FaceProofError as exc:
        yield ("stage", {"n": 1, "name": "scan", "status": "fail"})
        yield ("error", {"message": str(exc), "stage": "scan", "exit_code": 2})
        return

    yield ("stage", {"n": 1, "name": "scan", "status": "ok"})
    yield (
        "face",
        {
            "bbox": list(face_record.bbox),
            "confidence": face_record.confidence,
            "embedding": list(face_record.embedding),
            "crop_url": "/out/" + os.path.basename(face_record.crop_path),
        },
    )

    # ---- Stage 2: search (public upload + reverse image search) ----------
    yield ("stage", {"n": 2, "name": "search", "status": "running"})
    try:
        api_key = load_api_key()
        original_url = upload_public(image_path)
        crop_url = upload_public(face_record.crop_path)
        candidates = reverse_image_search(
            [original_url, crop_url],
            api_key=api_key,
            out_dir=out_dir,
            social_only=social_only,
        )
        if not candidates:
            raise NoCandidatesFound(
                "Reverse image search returned no candidates for this face."
            )
    except NoCandidatesFound as exc:
        yield ("stage", {"n": 2, "name": "search", "status": "fail"})
        yield ("error", {"message": str(exc), "stage": "search", "exit_code": 3})
        return
    except FaceProofError as exc:
        yield ("stage", {"n": 2, "name": "search", "status": "fail"})
        yield ("error", {"message": str(exc), "stage": "search", "exit_code": 3})
        return

    yield ("stage", {"n": 2, "name": "search", "status": "ok"})
    yield (
        "candidates",
        {
            "items": [
                {
                    "post_url": c.post_url,
                    "source_domain": c.source_domain,
                    "title": c.title,
                    "image_url": c.image_url,
                    "engine": c.engine,
                }
                for c in candidates
            ],
            # The effective threshold travels with the event so a client
            # never has to hardcode its own copy and drift from the run.
            "threshold": threshold,
        },
    )

    # ---- Stage 3: verify (face-similarity match) --------------------------
    yield ("stage", {"n": 3, "name": "verify", "status": "running"})
    try:
        matches = verify_candidates(
            candidates, face_record.embedding, threshold=threshold, out_dir=out_dir
        )
        if not matches:
            raise NoVerifiedMatch(
                f"No candidate cleared the face-similarity threshold ({threshold})."
            )
    except NoVerifiedMatch as exc:
        yield ("stage", {"n": 3, "name": "verify", "status": "fail"})
        score = best_score(scored_path=os.path.join(out_dir, "candidates_scored.json"))
        yield (
            "error",
            {
                "message": f"{exc} Best score seen: {score:.4f}.",
                "stage": "verify",
                "exit_code": 4,
            },
        )
        return
    except FaceProofError as exc:
        yield ("stage", {"n": 3, "name": "verify", "status": "fail"})
        yield ("error", {"message": str(exc), "stage": "verify", "exit_code": 4})
        return

    yield ("stage", {"n": 3, "name": "verify", "status": "ok"})
    # verify_candidates() returns Matches sorted descending by face_similarity.
    winning_match = matches[0]
    yield ("match", _asdict(winning_match))

    record_path = os.path.join(out_dir, "record.json")
    with open(record_path, "w", encoding="utf-8") as f:
        json.dump(_asdict(winning_match), f, indent=2, default=str)

    # ---- Stage 4: seal (blockchain record) --------------------------------
    yield ("stage", {"n": 4, "name": "seal", "status": "running"})
    if dry_run:
        yield ("stage", {"n": 4, "name": "seal", "status": "ok"})
        yield ("done", {"record_path": record_path, "receipt_path": None})
        return

    try:
        receipt: Receipt = record_match(
            winning_match, face_record.embedding, network=network
        )
    except ChainError as exc:
        yield ("stage", {"n": 4, "name": "seal", "status": "fail"})
        yield ("error", {"message": str(exc), "stage": "seal", "exit_code": 5})
        return
    except FaceProofError as exc:
        yield ("stage", {"n": 4, "name": "seal", "status": "fail"})
        yield ("error", {"message": str(exc), "stage": "seal", "exit_code": 5})
        return

    yield ("stage", {"n": 4, "name": "seal", "status": "ok"})
    yield ("chain", _asdict(receipt))

    receipt_path = os.path.join(out_dir, "receipt.json")
    with open(receipt_path, "w", encoding="utf-8") as f:
        json.dump(_asdict(receipt), f, indent=2, default=str)

    yield ("done", {"record_path": record_path, "receipt_path": receipt_path})

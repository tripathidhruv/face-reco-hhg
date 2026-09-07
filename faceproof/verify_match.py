"""Turns raw reverse-image-search candidates into face-verified Matches.

This is what elevates faceproof above "just ran Google Lens": a candidate is
only promoted to a `Match` if the face embedded in the fetched image actually
matches the query face above a similarity threshold. Every candidate that was
considered — accepted or rejected, plus any that couldn't even be scored —
is written to an audit-trail JSON file, so the process is fully inspectable
after the fact.
"""

import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from typing import Optional

from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

from faceproof.detect import cosine_similarity, embed_image_bytes
from faceproof.types import Candidate, Match  # noqa: F401 - Candidate used in type hints below

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36 FaceProofBot/1.0"
)
DOWNLOAD_TIMEOUT_SECONDS = 15
SCORED_FILENAME = "candidates_scored.json"


MAX_DOWNLOAD_WORKERS = 12


def _download_many(urls: list[str]) -> dict:
    """Fetch every URL concurrently and return {url: bytes or None}.

    Candidate thumbnails are independent HTTP fetches, so downloading them in
    parallel removes almost all of this stage's wall-clock cost. Duplicate
    URLs (several posts often share one thumbnail) are fetched once. Face
    encoding still happens on the caller's thread, keeping torch inference
    single-threaded.
    """
    unique = [u for u in dict.fromkeys(urls) if u]
    if not unique:
        return {}

    fetched: dict = {}
    workers = min(MAX_DOWNLOAD_WORKERS, len(unique))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_download_image, url): url for url in unique}
        for future in as_completed(futures):
            url = futures[future]
            try:
                fetched[url] = future.result()
            except Exception as exc:  # noqa: BLE001 - one bad fetch must not abort the batch
                print(f"[verify_match] download raised for {url}: {exc}", file=sys.stderr)
                fetched[url] = None

    return fetched


MAX_ENCODE_WORKERS = 4


def _encode_many(fetched: dict) -> dict:
    """Encode every downloaded image's face and return {url: embedding or None}.

    torch releases the GIL during inference, so a small pool overlaps the
    per-image detect/encode work. Kept deliberately small: these are tiny
    thumbnails and oversubscribing the CPU makes it slower, not faster.
    """
    items = [(url, data) for url, data in fetched.items() if data]
    if not items:
        return {}

    encoded: dict = {}
    workers = min(MAX_ENCODE_WORKERS, len(items))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(embed_image_bytes, data): url for url, data in items}
        for future in as_completed(futures):
            url = futures[future]
            try:
                encoded[url] = future.result()
            except Exception as exc:  # noqa: BLE001 - one bad image must not abort the batch
                print(f"[verify_match] encode raised for {url}: {exc}", file=sys.stderr)
                encoded[url] = None

    return encoded


def _download_image(url: str) -> Optional[bytes]:
    """Fetch `url` and return its bytes, or None (logging why) on any failure or non-image response."""
    try:
        response = requests.get(
            url,
            timeout=DOWNLOAD_TIMEOUT_SECONDS,
            headers={"User-Agent": USER_AGENT},
        )
    except requests.RequestException as exc:
        print(f"[verify_match] download failed for {url}: {exc}", file=sys.stderr)
        return None

    if response.status_code != 200:
        print(
            f"[verify_match] download failed for {url}: HTTP {response.status_code}",
            file=sys.stderr,
        )
        return None

    content_type = response.headers.get("Content-Type", "").split(";")[0].strip().lower()
    if not content_type.startswith("image/"):
        print(
            f"[verify_match] skipping {url}: non-image content-type {content_type!r}",
            file=sys.stderr,
        )
        return None

    return response.content


def verify_candidates(
    candidates: list[Candidate],
    query_embedding: list[float],
    threshold: float = 0.60,
    out_dir: str = "out",
) -> list[Match]:
    """Score every candidate's face similarity against `query_embedding`, returning verified Matches.

    For each candidate: download the image, hash it (sha256), embed the face
    in it, and compare against `query_embedding` with cosine similarity.
    Candidates scoring >= `threshold` become `Match` records. A failure at
    any stage for one candidate (download error, non-image response, no face
    detected) is logged to stderr and that candidate is skipped — it never
    aborts the loop.

    The full audit trail — every candidate considered, whether verified,
    rejected, or unscored, along with its score if one was computed — is
    written to `{out_dir}/candidates_scored.json`.

    Returns the verified Matches sorted descending by face_similarity, or an
    empty list if none clear the bar (the caller is expected to raise
    NoVerifiedMatch in that case).
    """
    os.makedirs(out_dir, exist_ok=True)

    scored_records = []
    matches = []

    fetched = _download_many([c.image_url for c in candidates])
    encoded = _encode_many(fetched)

    for candidate in candidates:
        record = {
            "post_url": candidate.post_url,
            "source_domain": candidate.source_domain,
            "title": candidate.title,
            "image_url": candidate.image_url,
            "engine": candidate.engine,
            "image_sha256": None,
            "face_similarity": None,
            "status": "error",
            "reason": None,
        }

        try:
            image_bytes = fetched.get(candidate.image_url)
            if image_bytes is None:
                record["reason"] = "download_failed_or_non_image"
                scored_records.append(record)
                continue

            image_sha256 = hashlib.sha256(image_bytes).hexdigest()
            record["image_sha256"] = image_sha256

            embedding = encoded.get(candidate.image_url)
            if embedding is None:
                print(
                    f"[verify_match] no face detected for {candidate.image_url}",
                    file=sys.stderr,
                )
                record["status"] = "no_face"
                record["reason"] = "no_face_detected"
                scored_records.append(record)
                continue

            similarity = cosine_similarity(embedding, query_embedding)
            record["face_similarity"] = similarity

            if similarity >= threshold:
                record["status"] = "verified"
                matches.append(
                    Match(
                        post_url=candidate.post_url,
                        source_domain=candidate.source_domain,
                        title=candidate.title,
                        image_url=candidate.image_url,
                        image_sha256=image_sha256,
                        face_similarity=similarity,
                        verified_at=datetime.now(timezone.utc).isoformat(),
                    )
                )
            else:
                record["status"] = "rejected"

            scored_records.append(record)

        except Exception as exc:  # noqa: BLE001 - one bad candidate must never abort the loop
            print(
                f"[verify_match] unexpected error scoring {candidate.image_url}: {exc}",
                file=sys.stderr,
            )
            record["reason"] = f"unexpected_error: {exc}"
            scored_records.append(record)
            continue

    scored_path = os.path.join(out_dir, SCORED_FILENAME)
    with open(scored_path, "w", encoding="utf-8") as f:
        json.dump(scored_records, f, indent=2, sort_keys=True)

    matches.sort(key=lambda m: m.face_similarity, reverse=True)
    return matches


def best_score(scored_path: str = "out/candidates_scored.json") -> float:
    """Return the highest face_similarity seen in the audit-trail file at `scored_path`.

    Used so the CLI can tell the user how close the closest miss got, even
    when verify_candidates() returned no Matches. Returns 0.0 if the file is
    missing, empty, or contains no scored (non-None) similarities.
    """
    if not os.path.exists(scored_path):
        return 0.0

    with open(scored_path, "r", encoding="utf-8") as f:
        records = json.load(f)

    scores = [
        r["face_similarity"]
        for r in records
        if r.get("face_similarity") is not None
    ]
    return max(scores) if scores else 0.0

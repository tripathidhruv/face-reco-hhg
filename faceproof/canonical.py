"""Canonical serialization and hashing primitives — the tamper-evidence foundation of faceproof.

Everything downstream (on-chain commitments, audit trails, re-verification of a
previously-recorded Match) depends on one guarantee: the SAME logical record
always produces the SAME bytes, every time, on every machine. That guarantee is
what lets a hash stored on-chain be re-derived later and compared for equality.

The single biggest threat to that guarantee is IEEE-754 float formatting.
Python's `repr(float)` (and therefore the default `json.dumps` float encoder)
is *not* guaranteed to be stable across implementations, and even within
CPython, floats that arrive via different code paths (e.g. `0.1 + 0.2` vs a
literal `0.30000000000000004`) can print differently while being "the same
number" for all practical purposes. A face-similarity score or an embedding
component that round-trips to a different string representation than the one
used when the record was first hashed would silently break verification —
the on-chain hash would never match again, even though nothing meaningful
changed.

To eliminate this, `canonical_json` recursively walks the object BEFORE
serializing and rewrites every float to a fixed-precision string
(`f"{v:.6f}"`), stored as a JSON string, not a JSON number. Because it is
serialized as a string, `json.dumps` never gets a chance to re-format it with
its own (less predictable) float repr. This trades a small amount of
precision (6 decimal places) for total, permanent determinism.
"""

import json
import math
from typing import Any

from eth_utils import keccak

FLOAT_FORMAT = "{:.6f}"


def _normalize(obj: Any) -> Any:
    """Recursively rewrite floats to fixed-precision strings; leave everything else structurally intact.

    Order matters: `bool` is a subclass of `int` in Python, so the bool check
    must come before the float/int checks or every `True`/`False` would be
    coerced to a float string.
    """
    if isinstance(obj, bool):
        return obj
    if isinstance(obj, float):
        if not math.isfinite(obj):
            # allow_nan=False on the final dumps() will already reject these,
            # but fail fast here with a clearer error before we've thrown
            # away the offending value's context.
            raise ValueError(f"Cannot canonicalize non-finite float: {obj!r}")
        return FLOAT_FORMAT.format(obj)
    if isinstance(obj, dict):
        return {str(k): _normalize(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_normalize(v) for v in obj]
    return obj


def canonical_json(obj: Any) -> str:
    """Serialize `obj` to a byte-for-byte deterministic JSON string.

    Floats are normalized to fixed 6-decimal-place strings (see module
    docstring) before serialization, so the same logical record always
    produces identical output regardless of how its floats were originally
    computed or rounded. Keys are sorted and separators are compact so no
    incidental whitespace or key-ordering differences can creep in either.

    A float that round-trips to a different string representation than the
    one used at record-creation time would silently break on-chain
    verification — this function exists specifically to prevent that.
    """
    normalized = _normalize(obj)
    return json.dumps(
        normalized,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    )


def keccak_hex(data: bytes) -> str:
    """Return the keccak256 digest of `data` as a '0x'-prefixed hex string."""
    return "0x" + keccak(data).hex()


def payload_hash(match_dict: dict) -> str:
    """Hash a Match (as a plain dict) via its canonical JSON form.

    This is the value that gets committed on-chain as evidence that a
    particular Match record existed, unmodified, at the time of recording.
    """
    return keccak_hex(canonical_json(match_dict).encode())


def face_hash(embedding: list) -> str:
    """Hash a face embedding via its canonical form.

    This commits to the biometric WITHOUT publishing it: the raw embedding
    vector never needs to leave the verifier's machine (and never needs to be
    written on-chain), yet anyone holding the original embedding can later
    recompute this hash and prove it matches the one recorded — without the
    embedding itself ever being exposed on a public ledger.
    """
    return keccak_hex(canonical_json(embedding).encode())


def similarity_bps(sim: float) -> int:
    """Convert a [0, 1] similarity score to basis points (0..10000), clamped to that range."""
    bps = round(sim * 10000)
    return max(0, min(10000, bps))

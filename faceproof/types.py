"""faceproof.types

Shared data contracts for the faceproof pipeline: face detection -> reverse
image search -> blockchain record. Every sibling module imports its dataclass
and exception types from here, so this module has no dependencies on the
rest of the package.
"""

from dataclasses import dataclass, asdict  # noqa: F401 - asdict re-exported for callers


@dataclass
class FaceRecord:
    bbox: tuple[int, int, int, int]
    confidence: float
    embedding: list[float]
    crop_path: str
    crop_sha256: str


@dataclass
class Candidate:
    post_url: str
    source_domain: str
    title: str
    image_url: str
    engine: str


@dataclass
class Match:
    post_url: str
    source_domain: str
    title: str
    image_url: str
    image_sha256: str
    face_similarity: float
    verified_at: str


@dataclass
class Receipt:
    network: str
    chain_id: int
    contract_address: str
    tx_hash: str
    block_number: int
    explorer_url: str
    payload_hash: str
    face_hash: str


class FaceProofError(Exception):
    """Base exception for all faceproof pipeline errors."""


class NoFaceDetected(FaceProofError):
    """Raised when no face could be detected in an input image."""


class NoCandidatesFound(FaceProofError):
    """Raised when reverse image search returns no candidates."""


class NoVerifiedMatch(FaceProofError):
    """Raised when no candidate clears the face-similarity verification bar."""


class ChainError(FaceProofError):
    """Raised when writing or reading the blockchain record fails."""

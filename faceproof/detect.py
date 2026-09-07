"""faceproof.detect

Face detection and embedding via facenet-pytorch (MTCNN + InceptionResnetV1).

Model construction (weight loading) is deferred to a cached module-level
getter so that merely importing this module stays cheap; the heavy lifting
only happens the first time detect_and_encode / embed_image_bytes actually
runs. Runs on CPU only -- no CUDA assumptions.
"""

import hashlib
import io
import os
import sys
from functools import lru_cache

import numpy as np
import torch
from PIL import Image

from faceproof.types import FaceRecord, NoFaceDetected

IMAGE_SIZE = 160


@lru_cache(maxsize=1)
def _get_models():
    """Lazily construct (and cache) the MTCNN detector and InceptionResnetV1
    embedder on CPU. Only runs once per process, on first use."""
    from facenet_pytorch import MTCNN, InceptionResnetV1

    device = torch.device("cpu")
    mtcnn = MTCNN(image_size=IMAGE_SIZE, keep_all=True, device=device)
    resnet = InceptionResnetV1(pretrained="vggface2").eval().to(device)
    return device, mtcnn, resnet


def _best_box(mtcnn, img, source_name: str):
    """Run detection and return (box, confidence) for the highest-confidence
    face, raising NoFaceDetected (naming source_name) if none was found."""
    boxes, probs = mtcnn.detect(img)
    if boxes is None or len(boxes) == 0:
        raise NoFaceDetected(f"No face detected in image: {source_name}")

    best_idx = int(np.argmax(probs))
    box = boxes[best_idx]
    confidence = float(probs[best_idx])
    return box, confidence


def _embed_face(device, mtcnn, resnet, img, box, save_path=None):
    """Extract the aligned 160x160 crop for `box` (optionally saving it as a
    PNG to save_path), embed it, and return (embedding_list, face_tensor)."""
    face_tensor = mtcnn.extract(img, box.reshape(1, 4), save_path=save_path)
    with torch.no_grad():
        embedding_tensor = resnet(face_tensor.to(device))[0]
    embedding_np = embedding_tensor.cpu().numpy().astype(np.float64)
    norm = np.linalg.norm(embedding_np)
    if norm > 0:
        embedding_np = embedding_np / norm
    return [float(x) for x in embedding_np]


def detect_and_encode(image_path: str, out_dir: str = "out") -> FaceRecord:
    """Detect the highest-confidence face in `image_path`, crop it aligned to
    160x160, embed it with InceptionResnetV1, and save both crop + embedding
    under `out_dir`. Raises NoFaceDetected if no face is found."""
    device, mtcnn, resnet = _get_models()

    img = Image.open(image_path).convert("RGB")
    box, confidence = _best_box(mtcnn, img, image_path)

    os.makedirs(out_dir, exist_ok=True)
    crop_path = os.path.join(out_dir, "face_crop.png")

    embedding = _embed_face(device, mtcnn, resnet, img, box, save_path=crop_path)

    np.save(os.path.join(out_dir, "embedding.npy"), np.array(embedding, dtype=np.float32))

    with open(crop_path, "rb") as f:
        crop_bytes = f.read()
    crop_sha256 = hashlib.sha256(crop_bytes).hexdigest()

    x1, y1, x2, y2 = box
    bbox = (int(round(x1)), int(round(y1)), int(round(x2 - x1)), int(round(y2 - y1)))

    return FaceRecord(
        bbox=bbox,
        confidence=confidence,
        embedding=embedding,
        crop_path=crop_path,
        crop_sha256=crop_sha256,
    )


def embed_image_bytes(data: bytes) -> list[float] | None:
    """Detect + embed the highest-confidence face in in-memory image bytes.

    Returns None (never raises) if no face is found or the bytes are not a
    valid/decodable image -- callers use this to score search-result images
    without a single bad response aborting the whole run.
    """
    try:
        device, mtcnn, resnet = _get_models()
        img = Image.open(io.BytesIO(data)).convert("RGB")
        box, _confidence = _best_box(mtcnn, img, "<in-memory bytes>")
        return _embed_face(device, mtcnn, resnet, img, box, save_path=None)
    except NoFaceDetected:
        return None
    except Exception:
        return None


def cosine_similarity(a: list[float], b: list[float]) -> float:
    """Plain numpy cosine similarity. Inputs are expected to already be
    L2-normalized, but this does not assume it."""
    va = np.asarray(a, dtype=np.float64)
    vb = np.asarray(b, dtype=np.float64)
    denom = np.linalg.norm(va) * np.linalg.norm(vb)
    if denom == 0:
        return 0.0
    return float(np.dot(va, vb) / denom)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python -m faceproof.detect <image_path>", file=sys.stderr)
        sys.exit(1)

    record = detect_and_encode(sys.argv[1])
    print(f"bbox: {record.bbox}")
    print(f"confidence: {record.confidence}")
    print(f"embedding[:8]: {record.embedding[:8]}")

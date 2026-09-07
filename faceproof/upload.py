"""Put a local image somewhere publicly fetchable, so a reverse-image-search
API can retrieve it.

SerpAPI's Google Lens / reverse-image engines take an image *URL*, not a file
upload, so the pipeline needs a temporary public URL for the query image.

Privacy note: this necessarily publishes the query image to a third-party host
for the duration of the search. Ephemeral providers are tried first so the URL
stops resolving on its own; the permanent host is a last resort only.

Providers are tried in order and the first success wins. Free hosts come and
go - 0x0.st disabled uploads entirely, and litterbox intermittently blocks
non-browser clients - so the chain matters more than any single provider.
"""

from __future__ import annotations

import os

from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

TIMEOUT = 45

# A browser-ish UA: several of these hosts reject obvious script clients.
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


def _headers() -> dict[str, str]:
    return {"User-Agent": USER_AGENT}


def _valid(url: str) -> bool:
    return url.startswith("http://") or url.startswith("https://")


def _litterbox(path: str) -> str:
    """catbox's ephemeral sibling. Expires in 1 hour."""
    with open(path, "rb") as fh:
        resp = requests.post(
            "https://litterbox.catbox.moe/resources/internals/api.php",
            data={"reqtype": "fileupload", "time": "1h"},
            files={"fileToUpload": fh},
            headers=_headers(),
            timeout=TIMEOUT,
        )
    resp.raise_for_status()
    return resp.text.strip()


def _uguu(path: str) -> str:
    """Direct-link host. Expires in 48 hours."""
    with open(path, "rb") as fh:
        resp = requests.post(
            "https://uguu.se/upload?output=text",
            files={"files[]": fh},
            headers=_headers(),
            timeout=TIMEOUT,
        )
    resp.raise_for_status()
    return resp.text.strip()


def _tmpfiles(path: str) -> str:
    """Expires in 1 hour. Returns a viewer URL that must be rewritten to /dl/
    to serve the raw bytes - a search engine fetching the viewer page would
    get HTML, not an image."""
    with open(path, "rb") as fh:
        resp = requests.post(
            "https://tmpfiles.org/api/v1/upload",
            files={"file": fh},
            headers=_headers(),
            timeout=TIMEOUT,
        )
    resp.raise_for_status()
    url = resp.json()["data"]["url"].strip()
    if "/dl/" not in url:
        url = url.replace("tmpfiles.org/", "tmpfiles.org/dl/", 1)
    return url


def _catbox(path: str) -> str:
    """Last resort: this host is PERMANENT. Only used when every ephemeral
    provider is unreachable."""
    with open(path, "rb") as fh:
        resp = requests.post(
            "https://catbox.moe/user/api.php",
            data={"reqtype": "fileupload"},
            files={"fileToUpload": fh},
            headers=_headers(),
            timeout=TIMEOUT,
        )
    resp.raise_for_status()
    return resp.text.strip()


# Ordered by measured reliability, then by how soon the URL expires.
#
# uguu leads because litterbox currently rejects non-browser clients with
# HTTP 403 and burns 1.2-2.3s per upload doing it, while uguu succeeds in
# under a second. Both are ephemeral; uguu simply holds the file for 48h
# instead of 1h. litterbox stays in the chain as the shorter-lived option
# should it start accepting these requests again.
PROVIDERS = (
    ("uguu.se (48h)", _uguu),
    ("litterbox.catbox.moe (1h)", _litterbox),
    ("tmpfiles.org (1h)", _tmpfiles),
    ("catbox.moe (permanent)", _catbox),
)


def upload_public(path: str) -> str:
    """Upload `path` to the first reachable public host and return its URL.

    Raises RuntimeError naming every provider's failure if none succeed.
    """
    if not os.path.isfile(path):
        raise FileNotFoundError(f"No such image to upload: {path}")

    failures: list[str] = []
    for name, fn in PROVIDERS:
        try:
            url = fn(path)
        except Exception as exc:  # noqa: BLE001 - any failure moves to the next host
            failures.append(f"  {name} failed: {type(exc).__name__}: {exc}")
            continue

        if _valid(url):
            return url
        failures.append(f"  {name} returned an unusable response: {url[:200]!r}")

    raise RuntimeError(
        "Failed to upload the image to a public host - tried "
        f"{len(PROVIDERS)} providers.\n" + "\n".join(failures)
    )


def upload_public_many(paths: list[str]) -> list[str]:
    """Upload several images concurrently and return their URLs in input order.

    Each upload is an independent chain of network round trips, so doing them
    in parallel roughly halves the wall-clock cost of uploading the original
    photo plus its face crop. Any single failure still raises, since a missing
    URL means that search query cannot be made.
    """
    if not paths:
        return []
    if len(paths) == 1:
        return [upload_public(paths[0])]

    urls: list[str | None] = [None] * len(paths)
    with ThreadPoolExecutor(max_workers=len(paths)) as pool:
        futures = {pool.submit(upload_public, path): i for i, path in enumerate(paths)}
        for future in as_completed(futures):
            urls[futures[future]] = future.result()

    return [u for u in urls if u is not None]


if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("usage: python -m faceproof.upload <image path>")
        raise SystemExit(1)

    print(upload_public(sys.argv[1]))

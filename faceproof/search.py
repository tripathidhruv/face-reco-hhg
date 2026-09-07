"""faceproof.search

Reverse-image search via SerpAPI (real HTTP calls only — never fabricated).

For each candidate image URL we query two SerpAPI engines:
  - google_lens            (params: engine, url, api_key)
  - google_reverse_image    (params: engine, image_url, api_key)

Every raw response (or error) is written to {out_dir}/search_raw.json as the
audit trail proving the search actually happened against SerpAPI.
"""

import json
import os
import sys
import urllib.parse

import requests

from faceproof.types import Candidate

SERPAPI_URL = "https://serpapi.com/search"

SOCIAL_DOMAINS = {
    "instagram.com",
    "x.com",
    "twitter.com",
    "facebook.com",
    "fb.com",
    "linkedin.com",
    "tiktok.com",
    "reddit.com",
    "youtube.com",
    "threads.net",
    "pinterest.com",
}

TIMEOUT_SECONDS = 30


def _domain(url: str) -> str:
    """Extract a normalized registrable-ish domain (host, minus www.) from a URL."""
    try:
        netloc = urllib.parse.urlparse(url).netloc.lower()
    except Exception:
        return ""
    if "@" in netloc:
        netloc = netloc.rsplit("@", 1)[-1]
    if ":" in netloc:
        netloc = netloc.split(":", 1)[0]
    if netloc.startswith("www."):
        netloc = netloc[4:]
    return netloc


def _is_social_domain(domain: str) -> bool:
    return any(domain == d or domain.endswith("." + d) for d in SOCIAL_DOMAINS)


def _write_raw(out_dir: str, raw_records: list) -> None:
    os.makedirs(out_dir, exist_ok=True)
    raw_path = os.path.join(out_dir, "search_raw.json")
    with open(raw_path, "w", encoding="utf-8") as fh:
        json.dump(raw_records, fh, indent=2, ensure_ascii=False, default=str)


def _query_serpapi(engine: str, params: dict, out_dir: str, raw_records: list) -> dict:
    """Issue one SerpAPI call, record it (success or failure) in raw_records,
    persist the audit trail immediately, and return the response dict
    (or an {"error": ...} dict on failure)."""
    try:
        query_url = requests.Request("GET", SERPAPI_URL, params=params).prepare().url
    except Exception:
        query_url = SERPAPI_URL

    response: dict
    try:
        resp = requests.get(SERPAPI_URL, params=params, timeout=TIMEOUT_SECONDS)
        response = resp.json()
        if not isinstance(response, dict):
            response = {"error": f"unexpected non-object JSON response: {response!r}"}
    except Exception as exc:  # noqa: BLE001 - one dead engine must not kill the run
        response = {"error": f"{type(exc).__name__}: {exc}"}

    raw_records.append({"engine": engine, "query_url": query_url, "response": response})
    # Write after every call so the audit trail survives even if a later
    # call raises something unexpected.
    _write_raw(out_dir, raw_records)
    return response


def _extract_candidates(engine: str, response: dict) -> list:
    candidates = []

    if "error" in response:
        print(f"[faceproof.search] SerpAPI error ({engine}): {response['error']}", file=sys.stderr)
        return candidates

    if engine == "google_lens":
        items = response.get("visual_matches") or []
        for item in items:
            link = item.get("link")
            if not link:
                continue
            candidates.append(
                Candidate(
                    post_url=link,
                    source_domain=_domain(link),
                    title=item.get("title") or "",
                    image_url=item.get("thumbnail") or "",
                    engine=engine,
                )
            )
    else:  # google_reverse_image
        items = response.get("image_results") or []
        for item in items:
            link = item.get("link")
            if not link:
                continue
            candidates.append(
                Candidate(
                    post_url=link,
                    source_domain=_domain(link),
                    title=item.get("title") or "",
                    image_url=item.get("thumbnail") or "",
                    engine=engine,
                )
            )

    return candidates


def reverse_image_search(
    image_urls: list[str],
    api_key: str,
    out_dir: str = "out",
    social_only: bool = True,
) -> list[Candidate]:
    """Reverse-search each public image URL against SerpAPI's google_lens and
    google_reverse_image engines and return deduped Candidate results.

    Returns [] if nothing is found — callers are responsible for raising
    NoCandidatesFound themselves.
    """
    raw_records: list = []
    all_candidates: list[Candidate] = []

    for image_url in image_urls:
        lens_params = {"engine": "google_lens", "url": image_url, "api_key": api_key}
        lens_response = _query_serpapi("google_lens", lens_params, out_dir, raw_records)
        all_candidates.extend(_extract_candidates("google_lens", lens_response))

        reverse_params = {"engine": "google_reverse_image", "image_url": image_url, "api_key": api_key}
        reverse_response = _query_serpapi("google_reverse_image", reverse_params, out_dir, raw_records)
        all_candidates.extend(_extract_candidates("google_reverse_image", reverse_response))

    deduped: dict[str, Candidate] = {}
    for candidate in all_candidates:
        if social_only and not _is_social_domain(candidate.source_domain):
            continue
        if candidate.post_url not in deduped:
            deduped[candidate.post_url] = candidate

    return list(deduped.values())


def load_api_key() -> str:
    """Read SERPAPI_KEY from the environment, falling back to a .env file
    (via python-dotenv, if installed)."""
    key = os.environ.get("SERPAPI_KEY")
    if key:
        return key

    try:
        from dotenv import load_dotenv  # type: ignore

        load_dotenv()
        key = os.environ.get("SERPAPI_KEY")
        if key:
            return key
    except ImportError:
        pass

    raise RuntimeError(
        "SERPAPI_KEY is not set. Sign up for a free API key at https://serpapi.com "
        "and set the SERPAPI_KEY environment variable (or put SERPAPI_KEY=... in a .env file)."
    )


if __name__ == "__main__":
    from faceproof.upload import upload_public

    if len(sys.argv) < 2:
        print("Usage: python -m faceproof.search <local_image_path>", file=sys.stderr)
        sys.exit(1)

    image_path = sys.argv[1]
    key = load_api_key()

    print(f"Uploading {image_path} to a public host...")
    public_url = upload_public(image_path)
    print(f"Public URL: {public_url}")

    print("Querying SerpAPI (google_lens + google_reverse_image)...")
    found = reverse_image_search([public_url], key)

    if not found:
        print("No candidates found.")
    else:
        print(f"Found {len(found)} candidate(s):")
        for c in found:
            print(f"  [{c.engine}] {c.source_domain} — {c.title}")
            print(f"    post_url:  {c.post_url}")
            print(f"    image_url: {c.image_url}")

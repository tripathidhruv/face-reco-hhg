# faceproof

Face identification + blockchain verification, built for Hacker House Goa 2026 Shortlisting Task 3.

Given a photo, faceproof finds public social-media posts that plausibly contain the same
face, re-verifies each candidate by actually comparing faces (not just images), and writes
a tamper-evident, append-only record of the verified match to a blockchain.

## What it does

```
                    [1] DETECT + ENCODE                [2] REVERSE IMAGE SEARCH
   input image  ─────────────────────────▶  512-d   ─────────────────────────▶  candidate
                  MTCNN face detect +      embedding    upload to litterbox.      posts on
                  InceptionResnetV1                     catbox.moe -> public      social
                  (vggface2) encode                     URL -> SerpAPI            media
                                                         (google_lens +
                                                         google_reverse_image)
                                                                │
                                                                ▼
   on-chain record  ◀─────────────────────────  [4] BLOCKCHAIN WRITE  ◀── verified
   (FaceMatchRegistry,                          keccak256(canonical      Match(es)
   append-only, immutable)                      JSON record) + keccak
        ▲                                       (embedding) as faceHash
        │
        └── [3] FACE RE-VERIFICATION: download each candidate's image, re-encode
            its face, cosine-compare to the query embedding (default threshold 0.60)
            -- this is what makes it face matching, not mere image similarity.
```

## The 4 stages

1. **Face detect + encode** — `facenet-pytorch` (MTCNN for detection, InceptionResnetV1
   pretrained on `vggface2`) turns the input photo into a 512-dimensional face embedding.
   No face detected -> exit code `2`.
2. **Genuine reverse image search** — the image is uploaded to the first reachable
   ephemeral public host (`uguu.se` -> `litterbox.catbox.moe` -> `tmpfiles.org`, with
   permanent `catbox.moe` only as a last resort) to get a short-lived public URL. This
   is required because SerpAPI's image-search engines take a URL, not a raw upload.
   Then real SerpAPI calls hit the
   `google_lens` and `google_reverse_image` engines with that URL. Every raw response is
   saved to `out/search_raw.json` as an audit trail before any filtering happens. Results
   are then filtered down to known social-media domains. No candidates -> exit code `3`.
3. **Face re-verification** — each candidate's image is downloaded, a face is detected and
   re-encoded, and its embedding is cosine-compared against the original query embedding.
   Only candidates scoring at or above the threshold (default **0.60**) are promoted to a
   `Match`. This step is the whole point: Google Lens returns *visually similar images*,
   not verified faces, so without this step "face matching" would really just be "image
   matching." No candidate clears the bar -> exit code `4`.
4. **Blockchain write** — each verified `Match` is serialized to canonical JSON
   (deterministic key ordering + fixed-precision floats) and hashed with `keccak256` to get
   a `payloadHash`; the query face embedding is separately hashed to a `faceHash` so the
   biometric itself never has to leave the machine or touch the chain. Both are written to
   a `FaceMatchRegistry` Solidity contract, compiled with `py-solc-x` and deployed/called via
   `web3.py`. Chain error (bad RPC, no funds, revert) -> exit code `5`.

## Quickstart

```bash
python -m venv venv
venv\Scripts\activate                       # Windows

# CPU-only torch is much smaller than the CUDA build:
pip install torch --index-url https://download.pytorch.org/whl/cpu
pip install -r requirements.txt

copy .env.example .env                      # then fill in SERPAPI_KEY / PRIVATE_KEY / RPC_URL

python -m faceproof.cli deploy --network base-sepolia
python -m faceproof.cli run --image path\to\photo.jpg --network base-sepolia
python -m faceproof.cli verify                # network is read from out/receipt.json
python -m faceproof.cli tamper-demo           # same, then mutates one byte
python -m faceproof.cli serve                # optional animated web dashboard (FastAPI + SSE)
```

### Networks

| `--network` | What it is | Persists between commands? |
|---|---|---|
| `local` | In-process EVM (`eth-tester`). Zero setup: no RPC, no faucet, no wallet. | **No** — the chain lives and dies with the Python process |
| `ganache` | A persistent local node on `http://127.0.0.1:8545` | Yes |
| `base-sepolia` | Public testnet, chain `84532`, real block explorer | Yes |
| `polygon-amoy` | Public testnet, chain `80002` | Yes |

**Important:** `local` is fine for a single one-shot `run` and for the test suite, but it
**cannot** demonstrate re-verification, because `verify` runs in a new process and gets a
fresh, empty chain that has never heard of the contract. For an offline demo that includes
`verify` and `tamper-demo`, use `ganache`:

```bash
npm install -g ganache
ganache --wallet.deterministic --chain.chainId 1337 --port 8545
```

Then `--network ganache` needs no credentials — the deterministic wallet is prefunded.

## Which blockchain, and why

**Base Sepolia (chain `84532`) is the primary target**, with a **persistent local
`ganache` node** as the fallback so the demo can never be blocked by a dry faucet, a rate
limit, or a flaky RPC endpoint at the worst possible moment. (The in-process `local` EVM
is a third option, but see the network table above for why it cannot demonstrate
re-verification.) Base Sepolia was chosen over
mainnet for the obvious reason (no real funds needed — get test ETH at
`portal.cdp.coinbase.com/products/faucet`) and over other testnets mainly for faucet
reliability. Polygon Amoy (`80002`) is wired up as a second option for the same reasons.
Contract logic and CLI behavior are identical across all three targets — only the RPC
endpoint and chain ID change.

## Live deployment (Base Sepolia)

The pipeline has been run end to end against the public Base Sepolia testnet, not just a
local chain:

| | |
|---|---|
| Contract | [`0x2927Ebb7701Daf8Bea581de5b7F622c8406d2e98`](https://sepolia.basescan.org/address/0x2927Ebb7701Daf8Bea581de5b7F622c8406d2e98) |
| Record tx | [`0xd2dc1224c580f91da4012589bd84c79cd4a63bc8b5bf65ab9f1beee818c93d48`](https://sepolia.basescan.org/tx/0xd2dc1224c580f91da4012589bd84c79cd4a63bc8b5bf65ab9f1beee818c93d48) |
| Block | 46512563 (tx status 1, gas used 276,244) |
| Chain ID | 84532 |
| Matched post | a facebook.com post located by reverse image search |
| Face similarity | 0.9852 cosine |
| `payload_hash` | `0xfbf816c8fb5d7ac064a3d4fd59a818a18a7dbf7042ac517ded750dff59a8f2dd` |

`verify` re-read that record from the chain and passed. `tamper-demo` then mutated one
character of the local record and the recomputed hash no longer matched. Deploying the
contract plus writing the record cost 0.0000068 ETH in total, so one faucet drip covers
many runs.

## How re-verification works

Every record is designed to be independently re-checked without trusting faceproof's own
output:

1. Recompute `payload_hash` by canonicalizing the `Match` record to deterministic JSON
   (sorted keys, fixed 6-decimal-place floats — see `faceproof/canonical.py`) and hashing it
   with `keccak256`, entirely locally.
2. Call the read-only `verifyRecord(payloadHash)` view function on the deployed
   `FaceMatchRegistry` contract.
3. Compare: if the contract reports `exists=True` and its stored hash equals the one just
   recomputed locally, the record is provably unmodified since it was written on-chain.

`tamper-demo` proves this isn't just theater: it takes a real, previously-recorded record,
flips a single byte in one field (e.g. one character of a URL), recomputes the hash, and
shows that the tampered hash no longer matches what's on-chain — while `verifyRecord` on
the *original*, untouched hash still succeeds. `FaceMatchRegistry.recordMatch` also reverts
outright on a duplicate `payloadHash`, so records are append-only by construction, not just
by convention.

## `out/` artifacts

| File | Produced by | Contents |
|---|---|---|
| `face_crop.png` | Stage 1 | Cropped face region used for encoding and reverse search |
| `embedding.npy` | Stage 1 | 512-d face embedding (numpy array, never written on-chain) |
| `search_raw.json` | Stage 2 | Unfiltered raw responses from every SerpAPI call — the audit trail |
| `candidates_scored.json` | Stage 3 | Every candidate considered, its face-similarity score, and accept/reject reason |
| `record.json` | Stage 4 | The canonical `Match` record(s) that were hashed and committed |
| `receipt.json` | Stage 4 | Transaction receipt(s) from `recordMatch` (tx hash, block, gas used) |
| `deployment.json` | `deploy` | Deployed contract address + ABI + network, keyed by network name |

## Exit codes

| Code | Meaning |
|---|---|
| `0` | OK |
| `2` | No face detected in the input image |
| `3` | Reverse image search returned no candidates |
| `4` | No candidate passed face re-verification |
| `5` | Blockchain error (RPC, funds, or contract revert) |

## Performance

A warm run (models already loaded) completes end to end in **~7s**, including the
blockchain write:

| Stage | Seconds |
|---|---|
| Face detect + encode | 0.2 |
| Upload query images | 0.9 |
| Reverse image search (4 SerpAPI calls) | ~5.0 |
| Re-verify all candidates | 1.6 |
| Contract deploy + write | ~0.5 |

Every stage that is a set of independent network round trips runs concurrently: the four
SerpAPI calls, both image uploads, and all candidate thumbnail downloads. Candidate face
encoding uses a small thread pool (torch releases the GIL during inference). None of this
changes which candidates are considered or how they are scored - it is the same work, just
not serialized. Search is now the floor, bounded by the slowest of the four API calls.

A cold `python -m faceproof.cli run` adds roughly 4s on top for importing torch and
loading the model weights. `serve` pays that once at startup instead, so a request through
the dashboard measures only the pipeline.

## Known limitations

This section is meant to be read, so it's deliberately blunt:

- **Google Lens is image similarity, not face recognition.** Recall depends heavily on
  whether the *exact* photo (or something close to it) is already indexed by Google — a
  different photo of the same person, cropped, filtered, or otherwise modified, may simply
  not be found at all. Absence of a hit proves nothing.
- **Re-verification runs against thumbnails/og:images**, not the original posted photo,
  because that's what reverse-search results expose. These often contain no detectable
  face at all (group shots, cropped previews, unrelated preview art), which silently drops
  otherwise-valid candidates before they're ever scored.
- **The 0.60 cosine-similarity threshold is a heuristic**, not a calibrated statistical
  guarantee. It carries real false-positive risk (different people falsely matched) and
  false-negative risk (same person missed), especially across lighting, age, and pose
  differences.
- **SerpAPI's free tier is 100 searches/month, and one run costs 4** (two engines x two
  calls each in this pipeline's usage pattern) — budget accordingly during a live demo.
- **The query image is published to a third-party host.** Reverse-image search needs a
  publicly fetchable URL, so the input face is briefly uploaded to a free file host.
  Ephemeral hosts are tried first (1h-48h expiry), but this still means the photo leaves
  your machine.
- **Free file hosts are unreliable and disappear.** During development `0x0.st` disabled
  uploads entirely and `litterbox` began intermittently returning HTTP 403 to non-browser
  clients, which is why `upload.py` tries four providers in order. If all four are
  unreachable, the search stage fails outright.
- **The in-process `local` chain cannot be re-verified across commands.** It exists only
  inside one Python process. Use `--network ganache` or a public testnet whenever the demo
  includes `verify` or `tamper-demo`.
- **On Windows, do not install the `web3[tester]` extra.** It pulls in `safe-pysha3`,
  which requires a C toolchain and fails to build. `requirements.txt` depends on
  `eth-tester` directly instead.
- **There is no liveness or anti-spoof detection.** A printed photo, a screen, or another
  photo of a photo will encode and match exactly like a live face would.
- **A public post URL and hashes go on-chain permanently and immutably.** There is no
  delete, no edit, and no way to later retract or correct a record once `recordMatch`
  succeeds — that's the point of the design, but it also means mistakes are permanent.

## Privacy and ethics

Only **hashes** are ever written on-chain — never the raw face embedding and never the
image itself. That said, running face search against a photo of someone who has not
consented has obvious misuse potential (stalking, doxxing, unwanted identification), and
nothing in this tool prevents that misuse technically — it's a policy and consent problem,
not a code problem. **This project is a demo built for a hackathon shortlisting task, not a
surveillance product**, and should not be pointed at real people without their knowledge and
consent.

"""faceproof.cli

Command-line front-end for the faceproof pipeline. Numbered stage banners
and plain ANSI colors so a screen recording of `faceproof run` reads
clearly.

Subcommands:
    run          --image PATH [--network ...] [--threshold 0.60] [--dry-run] [--all-domains]
    verify       [--record out/record.json] [--receipt out/receipt.json]
    tamper-demo  [--record out/record.json] [--receipt out/receipt.json]
    deploy       [--network ...]
    serve        [--port 8000]
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
from typing import Optional, Tuple

from faceproof.pipeline import run_pipeline
from faceproof.types import ChainError, FaceProofError
from faceproof.canonical import payload_hash
from faceproof.chain import NETWORKS
from faceproof.chain import deploy as chain_deploy
from faceproof.chain import verify_record


# ---------------------------------------------------------------------------
# ANSI colors — plain escape codes, no external dependency.
# ---------------------------------------------------------------------------
class C:
    RESET = "\033[0m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    RED = "\033[31m"
    GREEN = "\033[32m"
    YELLOW = "\033[33m"
    BLUE = "\033[34m"
    MAGENTA = "\033[35m"
    CYAN = "\033[36m"


NETWORK_CHOICES = list(NETWORKS)


def _enable_windows_ansi() -> None:
    """Best-effort enable of ANSI escape processing on Windows consoles."""
    if os.name != "nt":
        return
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
        mode = ctypes.c_uint32()
        if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            kernel32.SetConsoleMode(handle, mode.value | 0x0004)  # ENABLE_VIRTUAL_TERMINAL_PROCESSING
    except Exception:
        pass


def _c(text: str, color: str) -> str:
    return f"{color}{text}{C.RESET}"


def _banner(title: str) -> None:
    line = "=" * max(60, len(title) + 8)
    print(_c(line, C.CYAN))
    print(_c(f"  {title}", C.BOLD + C.CYAN))
    print(_c(line, C.CYAN))


_STAGE_LABELS = {
    1: "SCAN    (face detect)",
    2: "SEARCH  (reverse image search)",
    3: "VERIFY  (face match)",
    4: "SEAL    (blockchain record)",
}


def _stage_line(n: int, name: str, status: str) -> None:
    label = _STAGE_LABELS.get(n, name.upper())
    if status == "running":
        tag = _c("RUNNING", C.YELLOW)
    elif status == "ok":
        tag = _c("OK", C.GREEN + C.BOLD)
    else:
        tag = _c("FAIL", C.RED + C.BOLD)
    print(f"[{n}/4] {label:<32} {tag}")


# ---------------------------------------------------------------------------
# run
# ---------------------------------------------------------------------------
def cmd_run(args: argparse.Namespace) -> int:
    _banner("FACEPROOF - RUN")
    print(f"image     : {args.image}")
    print(f"network   : {args.network}")
    print(f"threshold : {args.threshold}")
    print(f"dry_run   : {args.dry_run}")
    print(f"domains   : {'all' if args.all_domains else 'social only'}")
    print()

    exit_code = 0
    for event_name, payload in run_pipeline(
        image_path=args.image,
        network=args.network,
        threshold=args.threshold,
        out_dir="out",
        social_only=not args.all_domains,
        dry_run=args.dry_run,
    ):
        if event_name == "stage":
            _stage_line(payload["n"], payload["name"], payload["status"])
        elif event_name == "face":
            print(
                _c(
                    f"      face detected: bbox={payload['bbox']} "
                    f"confidence={payload['confidence']:.3f}",
                    C.DIM,
                )
            )
            print(_c(f"      crop saved: {payload['crop_url']}", C.DIM))
        elif event_name == "candidates":
            items = payload["items"]
            print(_c(f"      {len(items)} candidate(s) found:", C.DIM))
            for it in items[:10]:
                print(
                    _c(
                        f"        - [{it['engine']}] {it['source_domain']}: {it['title']}",
                        C.DIM,
                    )
                )
            if len(items) > 10:
                print(_c(f"        ... and {len(items) - 10} more", C.DIM))
        elif event_name == "match":
            print(_c("      MATCH FOUND:", C.GREEN + C.BOLD))
            print(f"        post_url        : {payload['post_url']}")
            print(f"        source_domain   : {payload['source_domain']}")
            print(f"        title           : {payload['title']}")
            print(f"        face_similarity : {payload['face_similarity']:.4f}")
            print(f"        verified_at     : {payload['verified_at']}")
        elif event_name == "chain":
            print(_c("      CHAIN RECEIPT:", C.MAGENTA + C.BOLD))
            print(f"        network          : {payload['network']}")
            print(f"        chain_id         : {payload['chain_id']}")
            print(f"        contract_address : {payload['contract_address']}")
            print(f"        tx_hash          : {payload['tx_hash']}")
            print(f"        block_number     : {payload['block_number']}")
            print(f"        explorer_url     : {payload['explorer_url']}")
            print(f"        payload_hash     : {payload['payload_hash']}")
            print(f"        face_hash        : {payload['face_hash']}")
        elif event_name == "done":
            print()
            print(_c("DONE.", C.GREEN + C.BOLD))
            print(f"  record  : {payload['record_path']}")
            print(f"  receipt : {payload.get('receipt_path')}")
        elif event_name == "error":
            print()
            print(_c(f"ERROR [{payload['stage']}]: {payload['message']}", C.RED + C.BOLD))
            exit_code = payload["exit_code"]

    return exit_code


# ---------------------------------------------------------------------------
# verify
# ---------------------------------------------------------------------------
def _load_json(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _do_verify(record: dict, receipt: dict) -> Tuple[bool, str, dict]:
    """Recompute payload_hash locally and compare against the on-chain record.

    Returns (passed, local_hash, onchain_dict).
    """
    local_hash = payload_hash(record)
    network = receipt.get("network", "local")
    contract_address = receipt.get("contract_address")
    onchain_ok, onchain = verify_record(
        local_hash, network=network, contract_address=contract_address
    )
    return onchain_ok, local_hash, onchain


def cmd_verify(args: argparse.Namespace) -> int:
    _banner("FACEPROOF - VERIFY")
    try:
        record = _load_json(args.record)
        receipt = _load_json(args.receipt)
    except (OSError, json.JSONDecodeError) as exc:
        print(_c(f"ERROR: could not read record/receipt: {exc}", C.RED + C.BOLD))
        return 1

    try:
        passed, local_hash, onchain = _do_verify(record, receipt)
    except ChainError as exc:
        print(_c(f"ERROR: chain lookup failed: {exc}", C.RED + C.BOLD))
        return 1

    print(f"local payload_hash : {local_hash}")
    print(f"on-chain record    : {onchain}")
    print()
    if passed:
        print(_c(">>> PASS: record matches the on-chain seal <<<", C.GREEN + C.BOLD))
        return 0
    print(_c(">>> FAIL: record does NOT match the on-chain seal <<<", C.RED + C.BOLD))
    return 1


# ---------------------------------------------------------------------------
# tamper-demo — the money shot: mutate one byte, prove verification fails.
# ---------------------------------------------------------------------------
def _mutate_one_char(raw: str) -> Tuple[str, int]:
    """Flip a single hex/alnum character near the middle of `raw`.

    Returns (mutated_text, character_index).
    """
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


def cmd_tamper_demo(args: argparse.Namespace) -> int:
    _banner("FACEPROOF - TAMPER DEMO")
    record_path = args.record
    receipt_path = args.receipt

    if not os.path.exists(record_path) or not os.path.exists(receipt_path):
        print(
            _c(
                f"ERROR: {record_path} / {receipt_path} not found. "
                "Run `faceproof run` first.",
                C.RED + C.BOLD,
            )
        )
        return 1

    print(_c("Step 1: baseline verification (untampered record)", C.CYAN + C.BOLD))
    record = _load_json(record_path)
    receipt = _load_json(receipt_path)
    try:
        passed, local_hash, _onchain = _do_verify(record, receipt)
    except ChainError as exc:
        print(_c(f"ERROR: chain lookup failed: {exc}", C.RED + C.BOLD))
        return 1
    print(f"  local payload_hash : {local_hash}")
    print(
        f"  result             : "
        f"{_c('PASS', C.GREEN + C.BOLD) if passed else _c('FAIL', C.RED + C.BOLD)}"
    )
    print()

    print(_c("Step 2: tampering with the record (mutating one character)...", C.CYAN + C.BOLD))
    tampered_path = os.path.join(os.path.dirname(record_path) or ".", "record.tampered.json")
    shutil.copyfile(record_path, tampered_path)
    with open(tampered_path, "r", encoding="utf-8") as f:
        raw = f.read()

    tampered_raw, diff_index = _mutate_one_char(raw)
    with open(tampered_path, "w", encoding="utf-8") as f:
        f.write(tampered_raw)
    print(f"  tampered copy written to : {tampered_path}")
    print(f"  mutated character offset : {diff_index}")
    print()

    print(_c("Step 3: re-verifying the TAMPERED record...", C.CYAN + C.BOLD))
    tampered_record = json.loads(tampered_raw)
    try:
        passed2, local_hash2, _onchain2 = _do_verify(tampered_record, receipt)
    except ChainError as exc:
        print(_c(f"ERROR: chain lookup failed: {exc}", C.RED + C.BOLD))
        return 1

    print(f"  local payload_hash (tampered) : {local_hash2}")
    print(f"  local payload_hash (original) : {local_hash}")
    print()
    if not passed2 and local_hash2 != local_hash:
        print(_c("#" * 62, C.RED + C.BOLD))
        print(_c("###   TAMPER DETECTED -- VERIFICATION FAILS AS EXPECTED   ###", C.RED + C.BOLD))
        print(_c("#" * 62, C.RED + C.BOLD))
        return 0
    print(_c("!!! UNEXPECTED: tampered record still verified. !!!", C.YELLOW + C.BOLD))
    return 1


# ---------------------------------------------------------------------------
# deploy
# ---------------------------------------------------------------------------
def cmd_deploy(args: argparse.Namespace) -> int:
    _banner("FACEPROOF - DEPLOY")
    try:
        address = chain_deploy(network=args.network)
    except ChainError as exc:
        print(_c(f"ERROR: deploy failed: {exc}", C.RED + C.BOLD))
        return 1
    print(_c(f"Contract deployed on '{args.network}':", C.GREEN + C.BOLD))
    print(f"  address: {address}")
    return 0


# ---------------------------------------------------------------------------
# serve
# ---------------------------------------------------------------------------
def cmd_serve(args: argparse.Namespace) -> int:
    _banner("FACEPROOF - SERVE")
    try:
        import uvicorn
    except ImportError:
        print(_c("ERROR: uvicorn is not installed (`pip install uvicorn fastapi`).", C.RED + C.BOLD))
        return 1

    print(f"Starting web server on http://127.0.0.1:{args.port}")
    uvicorn.run("web.server:app", host="127.0.0.1", port=args.port, reload=False)
    return 0


# ---------------------------------------------------------------------------
# argparse wiring
# ---------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="faceproof",
        description="Face detect -> genuine reverse image search -> blockchain tamper-evident record.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_run = sub.add_parser("run", help="Run the full pipeline against an image.")
    p_run.add_argument("--image", required=True, help="Path to the input image.")
    p_run.add_argument(
        "--network", default="local", choices=NETWORK_CHOICES
    )
    p_run.add_argument("--threshold", type=float, default=0.60)
    p_run.add_argument("--dry-run", action="store_true")
    p_run.add_argument(
        "--all-domains",
        action="store_true",
        help="Do not restrict search candidates to known social domains.",
    )
    p_run.set_defaults(func=cmd_run)

    p_verify = sub.add_parser(
        "verify", help="Recompute the payload hash and compare it against the on-chain record."
    )
    p_verify.add_argument("--record", default=os.path.join("out", "record.json"))
    p_verify.add_argument("--receipt", default=os.path.join("out", "receipt.json"))
    p_verify.set_defaults(func=cmd_verify)

    p_tamper = sub.add_parser(
        "tamper-demo",
        help="Mutate one byte of the record and show that verification fails.",
    )
    p_tamper.add_argument("--record", default=os.path.join("out", "record.json"))
    p_tamper.add_argument("--receipt", default=os.path.join("out", "receipt.json"))
    p_tamper.set_defaults(func=cmd_tamper_demo)

    p_deploy = sub.add_parser("deploy", help="Deploy the tamper-evident record contract.")
    p_deploy.add_argument(
        "--network", default="local", choices=NETWORK_CHOICES
    )
    p_deploy.set_defaults(func=cmd_deploy)

    p_serve = sub.add_parser("serve", help="Launch the web UI via uvicorn.")
    p_serve.add_argument("--port", type=int, default=8000)
    p_serve.set_defaults(func=cmd_serve)

    return parser


def main(argv: Optional[list] = None) -> int:
    _enable_windows_ansi()
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except FaceProofError as exc:
        print(_c(f"ERROR: {exc}", C.RED + C.BOLD))
        return 1
    except KeyboardInterrupt:
        print()
        print(_c("Interrupted.", C.YELLOW))
        return 130


if __name__ == "__main__":
    sys.exit(main())

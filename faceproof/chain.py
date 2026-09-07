"""Blockchain layer for faceproof.

Compiles contracts/FaceMatchRegistry.sol with py-solc-x and talks to it with
web3.py — no Hardhat, no Foundry, no Node anywhere in this path.

Public API:
    compile_contract() -> (abi, bytecode)
    get_web3(network) -> Web3
    get_account(w3, network) -> address (str, "local") | LocalAccount (others)
    deploy(network="local") -> address
    record_match(match, embedding, network="local", contract_address=None) -> Receipt
    verify_record(payload_hash_hex, network="local", contract_address=None) -> (bool, dict)
"""

from __future__ import annotations

import dataclasses
import json
import os
from pathlib import Path

import solcx
from web3 import Web3

from faceproof.canonical import face_hash, payload_hash, similarity_bps
from faceproof.types import ChainError, Match, Receipt

# --------------------------------------------------------------------------
# Paths / constants
# --------------------------------------------------------------------------

_ROOT = Path(__file__).resolve().parent.parent
CONTRACT_PATH = _ROOT / "contracts" / "FaceMatchRegistry.sol"
BUILD_CACHE_PATH = _ROOT / "out" / "contract_build.json"
DEPLOYMENT_PATH = _ROOT / "out" / "deployment.json"

SOLC_VERSION = "0.8.20"
CONTRACT_NAME = "FaceMatchRegistry"

NETWORKS = {
    "local": {
        "rpc_url": None,  # in-process EthereumTesterProvider, no RPC endpoint
        "chain_id": None,  # whatever the tester provider reports at runtime
        "explorer_tx": "",  # no explorer for a local chain
    },
    "ganache": {
        # A persistent local node (`ganache --wallet.deterministic`). Unlike
        # "local", this chain outlives the Python process, so `run` then
        # `verify` in two separate commands can hit the same contract - which
        # is what re-verification actually has to demonstrate.
        "rpc_url": "http://127.0.0.1:8545",
        "chain_id": 1337,
        "explorer_tx": "",  # a local node has no block explorer
    },
    "base-sepolia": {
        "rpc_url": "https://sepolia.base.org",
        "chain_id": 84532,
        "explorer_tx": "https://sepolia.basescan.org/tx/{tx}",
    },
    "polygon-amoy": {
        "rpc_url": "https://polygon-amoy-bor-rpc.publicnode.com",
        "chain_id": 80002,
        "explorer_tx": "https://amoy.polygonscan.com/tx/{tx}",
    },
}

# EthereumTesterProvider's chain lives only in process memory: there is no
# node to reconnect to, so a single Web3 instance (and whatever it deployed)
# is cached here and reused for the lifetime of the process. A "local"
# address written to out/deployment.json by a *previous* process is not
# valid against a fresh tester chain, so it is intentionally not reloaded
# across runs — only within the same run.
# Account 0 of `ganache --wallet.deterministic`. A well-known throwaway test
# key with no value on any real network - never fund this address.
GANACHE_DETERMINISTIC_KEY = (
    "0x4f3edf983ac636a65a842ce7c78d9aa706d3b113bce9c46f30d7d21715b23b1d"
)

_LOCAL_W3: Web3 | None = None
_LOCAL_DEPLOYMENT_ADDRESS: str | None = None


# --------------------------------------------------------------------------
# Compilation
# --------------------------------------------------------------------------

def compile_contract() -> tuple[list, str]:
    """Compile FaceMatchRegistry.sol, caching {abi, bytecode} to out/contract_build.json."""
    source_mtime = CONTRACT_PATH.stat().st_mtime

    if BUILD_CACHE_PATH.exists():
        try:
            cached = json.loads(BUILD_CACHE_PATH.read_text())
        except (json.JSONDecodeError, OSError):
            cached = {}
        if cached.get("source_mtime") == source_mtime and cached.get("solc_version") == SOLC_VERSION:
            return cached["abi"], cached["bytecode"]

    installed = {str(v) for v in solcx.get_installed_solc_versions()}
    if SOLC_VERSION not in installed:
        solcx.install_solc(SOLC_VERSION)

    compiled = solcx.compile_files(
        [str(CONTRACT_PATH)],
        output_values=["abi", "bin"],
        solc_version=SOLC_VERSION,
    )
    contract_key = next(k for k in compiled if k.endswith(f":{CONTRACT_NAME}"))
    contract_data = compiled[contract_key]

    abi = contract_data["abi"]
    bytecode = contract_data["bin"]
    if not bytecode.startswith("0x"):
        bytecode = "0x" + bytecode

    BUILD_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    BUILD_CACHE_PATH.write_text(
        json.dumps(
            {
                "source_mtime": source_mtime,
                "solc_version": SOLC_VERSION,
                "abi": abi,
                "bytecode": bytecode,
            },
            indent=2,
        )
    )

    return abi, bytecode


# --------------------------------------------------------------------------
# Connection / accounts
# --------------------------------------------------------------------------

def get_web3(network: str) -> Web3:
    global _LOCAL_W3

    if network not in NETWORKS:
        raise ChainError(f"Unknown network {network!r}; choose one of {list(NETWORKS)}")

    if network == "local":
        if _LOCAL_W3 is None:
            _LOCAL_W3 = Web3(Web3.EthereumTesterProvider())
        return _LOCAL_W3

    cfg = NETWORKS[network]
    w3 = Web3(Web3.HTTPProvider(cfg["rpc_url"]))

    # Both Base and Polygon are PoA-style chains whose block headers carry
    # extraData longer than pre-London mainnet expects; inject whichever
    # middleware name this web3.py version ships, best-effort.
    for module_name, attr_name in (
        ("web3.middleware", "ExtraDataToPOAMiddleware"),
        ("web3.middleware", "geth_poa_middleware"),
    ):
        try:
            module = __import__(module_name, fromlist=[attr_name])
            middleware = getattr(module, attr_name)
            w3.middleware_onion.inject(middleware, layer=0)
            break
        except (ImportError, AttributeError):
            continue

    if not w3.is_connected():
        raise ChainError(f"Could not connect to {network!r} at {cfg['rpc_url']}")

    return w3


def get_account(w3: Web3, network: str):
    """Return the sender for `network`: an address string for "local"
    (the tester's first prefunded account, no key required), or a signing
    LocalAccount built from the PRIVATE_KEY env var for every other network.
    """
    if network == "local":
        return w3.eth.accounts[0]

    if network == "ganache":
        # `ganache --wallet.deterministic` always seeds the same accounts, so
        # the local node needs no credentials of its own. An explicit
        # PRIVATE_KEY still wins if one is set.
        return w3.eth.account.from_key(
            os.environ.get("PRIVATE_KEY") or GANACHE_DETERMINISTIC_KEY
        )

    private_key = os.environ.get("PRIVATE_KEY")
    if not private_key:
        try:
            from dotenv import load_dotenv

            load_dotenv()
        except ImportError:
            pass
        private_key = os.environ.get("PRIVATE_KEY")

    if not private_key:
        raise ChainError(
            f"No PRIVATE_KEY set for network {network!r}. Export the PRIVATE_KEY "
            "environment variable (or put it in a .env file) with a funded "
            "account's private key before transacting on this network."
        )

    return w3.eth.account.from_key(private_key)


def _sender_address(account) -> str:
    return account if isinstance(account, str) else account.address


def _tx_hex(tx_hash) -> str:
    """Normalize a transaction hash to a 0x-prefixed hex string.

    web3.py's HexBytes.hex() dropped the 0x prefix in v7+, which would
    otherwise produce a malformed block-explorer URL.
    """
    h = tx_hash.hex() if hasattr(tx_hash, "hex") else str(tx_hash)
    return h if h.startswith("0x") else "0x" + h

def _send_transaction(w3: Web3, network: str, account, tx: dict):
    if network == "local":
        return w3.eth.send_transaction(tx)

    signed = w3.eth.account.sign_transaction(tx, private_key=account.key)
    raw = getattr(signed, "raw_transaction", None) or getattr(signed, "rawTransaction", None)
    return w3.eth.send_raw_transaction(raw)


def _base_tx_params(w3: Web3, network: str, from_address: str) -> dict:
    params = {
        "from": from_address,
        "nonce": w3.eth.get_transaction_count(from_address),
        "chainId": w3.eth.chain_id,
    }

    base_fee = None
    if network != "local":
        try:
            base_fee = w3.eth.get_block("latest").get("baseFeePerGas")
        except Exception:
            base_fee = None

    if base_fee is not None:
        priority_fee = w3.to_wei(1, "gwei")
        params["maxPriorityFeePerGas"] = priority_fee
        params["maxFeePerGas"] = base_fee * 2 + priority_fee
    else:
        params["gasPrice"] = w3.eth.gas_price

    return params


def _send_and_wait(w3: Web3, network: str, account, tx: dict, action: str):
    try:
        tx_hash = _send_transaction(w3, network, account, tx)
    except Exception as exc:
        raise ChainError(f"{action} failed to send on {network!r}: {exc}") from exc

    try:
        receipt = w3.eth.wait_for_transaction_receipt(tx_hash)
    except Exception as exc:
        raise ChainError(f"{action} timed out waiting for a receipt on {network!r}: {exc}") from exc

    if receipt.get("status") != 1:
        raise ChainError(f"{action} reverted on-chain (tx {_tx_hex(tx_hash)}) on {network!r}")

    return tx_hash, receipt


# --------------------------------------------------------------------------
# Deployment
# --------------------------------------------------------------------------

def _load_deployment_address(network: str) -> str | None:
    if network == "local":
        return _LOCAL_DEPLOYMENT_ADDRESS

    if not DEPLOYMENT_PATH.exists():
        return None
    try:
        data = json.loads(DEPLOYMENT_PATH.read_text())
    except (json.JSONDecodeError, OSError):
        return None
    if data.get("network") != network:
        return None
    return data.get("address")


def deploy(network: str = "local") -> str:
    global _LOCAL_DEPLOYMENT_ADDRESS

    abi, bytecode = compile_contract()
    w3 = get_web3(network)
    account = get_account(w3, network)
    from_address = _sender_address(account)

    try:
        factory = w3.eth.contract(abi=abi, bytecode=bytecode)
        tx = factory.constructor().build_transaction(_base_tx_params(w3, network, from_address))
        tx_hash, receipt = _send_and_wait(w3, network, account, tx, "Deployment")
    except ChainError:
        raise
    except Exception as exc:
        raise ChainError(f"Deployment to {network!r} failed: {exc}") from exc

    address = Web3.to_checksum_address(receipt["contractAddress"])

    if network == "local":
        _LOCAL_DEPLOYMENT_ADDRESS = address

    DEPLOYMENT_PATH.parent.mkdir(parents=True, exist_ok=True)
    DEPLOYMENT_PATH.write_text(
        json.dumps({"network": network, "address": address, "abi": abi}, indent=2)
    )

    return address


# --------------------------------------------------------------------------
# Hash helpers
# --------------------------------------------------------------------------

def _to_bytes32(value) -> bytes:
    if isinstance(value, (bytes, bytearray)):
        b = bytes(value)
    elif isinstance(value, int):
        b = value.to_bytes(32, "big")
    elif isinstance(value, str):
        s = value[2:] if value.lower().startswith("0x") else value
        b = bytes.fromhex(s)
    else:
        raise ChainError(f"Cannot convert {value!r} to a bytes32 value")

    if len(b) != 32:
        raise ChainError(f"Expected a 32-byte hash, got {len(b)} bytes from {value!r}")
    return b


def _bytes32_to_hex(b: bytes) -> str:
    return "0x" + bytes(b).hex()


def _first_attr(obj, *names):
    for name in names:
        if hasattr(obj, name):
            return getattr(obj, name)
    raise ChainError(f"Match object has none of the expected fields {names!r}")


# --------------------------------------------------------------------------
# Recording / verification
# --------------------------------------------------------------------------

def record_match(
    match: Match,
    embedding: list[float],
    network: str = "local",
    contract_address: str | None = None,
) -> Receipt:
    if contract_address is None:
        contract_address = _load_deployment_address(network)
        if contract_address is None:
            contract_address = deploy(network)

    abi, _ = compile_contract()
    w3 = get_web3(network)
    account = get_account(w3, network)
    from_address = _sender_address(account)

    contract = w3.eth.contract(address=Web3.to_checksum_address(contract_address), abi=abi)

    p_hash_bytes = _to_bytes32(payload_hash(dataclasses.asdict(match)))
    f_hash_bytes = _to_bytes32(face_hash(embedding))

    post_url = _first_attr(match, "post_url", "postUrl", "url")
    raw_similarity = _first_attr(match, "face_similarity", "similarity", "similarity_score", "score")
    sim_bps = int(similarity_bps(raw_similarity))

    try:
        tx = contract.functions.recordMatch(
            p_hash_bytes, f_hash_bytes, post_url, sim_bps
        ).build_transaction(_base_tx_params(w3, network, from_address))
        tx_hash, receipt = _send_and_wait(w3, network, account, tx, "record_match")
    except ChainError:
        raise
    except Exception as exc:
        raise ChainError(f"record_match failed on {network!r}: {exc}") from exc

    cfg = NETWORKS[network]
    explorer_url = cfg["explorer_tx"].format(tx=_tx_hex(tx_hash)) if cfg["explorer_tx"] else ""

    return Receipt(
        network=network,
        chain_id=w3.eth.chain_id,
        contract_address=Web3.to_checksum_address(contract_address),
        tx_hash=_tx_hex(tx_hash),
        block_number=receipt["blockNumber"],
        explorer_url=explorer_url,
        payload_hash=_bytes32_to_hex(p_hash_bytes),
        face_hash=_bytes32_to_hex(f_hash_bytes),
    )


def verify_record(
    payload_hash_hex: str,
    network: str = "local",
    contract_address: str | None = None,
) -> tuple[bool, dict]:
    if contract_address is None:
        contract_address = _load_deployment_address(network)
        if contract_address is None:
            raise ChainError(f"No deployment found for network {network!r}; deploy first")

    abi, _ = compile_contract()
    w3 = get_web3(network)
    contract = w3.eth.contract(address=Web3.to_checksum_address(contract_address), abi=abi)

    p_hash_bytes = _to_bytes32(payload_hash_hex)

    try:
        exists, record = contract.functions.verifyRecord(p_hash_bytes).call()
    except Exception as exc:
        raise ChainError(f"verify_record failed on {network!r}: {exc}") from exc

    record_dict = {
        "payload_hash": _bytes32_to_hex(record[0]),
        "face_hash": _bytes32_to_hex(record[1]),
        "post_url": record[2],
        "similarity_bps": record[3],
        "timestamp": record[4],
        "submitter": record[5],
    }
    return exists, record_dict


# --------------------------------------------------------------------------
# Zero-credential demo
# --------------------------------------------------------------------------

def _dummy_field_value(field: dataclasses.Field):
    name = field.name.lower()
    if "url" in name:
        return "https://example.com/post/demo-12345"
    if "similar" in name or name == "score":
        return 0.91
    if "hash" in name:
        return "0x" + ("11" * 32)

    ftype = field.type
    if ftype in (str, "str"):
        return "demo"
    if ftype in (float, "float"):
        return 0.5
    if ftype in (int, "int"):
        return 1
    if ftype in (bool, "bool"):
        return True
    return "demo"


def _dummy_match() -> Match:
    """Build a throwaway Match for the demo without hard-coding the field
    layout of faceproof.types (owned by a different agent)."""
    values = {f.name: _dummy_field_value(f) for f in dataclasses.fields(Match)}
    return Match(**values)


if __name__ == "__main__":
    print("[faceproof.chain] Deploying FaceMatchRegistry to 'local'...")
    address = deploy("local")
    print(f"[faceproof.chain] Deployed at {address}")

    dummy_match = _dummy_match()
    dummy_embedding = [round(0.01 * i, 4) for i in range(128)]

    print("[faceproof.chain] Recording a dummy match on-chain...")
    receipt = record_match(dummy_match, dummy_embedding, network="local")
    print(f"[faceproof.chain] Receipt: {receipt}")

    print("[faceproof.chain] Re-verifying the record...")
    exists, record = verify_record(receipt.payload_hash, network="local")
    print(f"[faceproof.chain] exists={exists}")
    print(f"[faceproof.chain] record={record}")

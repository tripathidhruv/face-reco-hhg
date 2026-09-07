"""End-to-end contract test for faceproof.chain against the `local` (in-process
EVM, eth-tester) network.

This intentionally exercises the whole write/read loop rather than mocking
web3: deploy FaceMatchRegistry, record a real Match, and confirm
verify_record's on-chain view matches what a caller can recompute locally
from the same Match via faceproof.canonical.payload_hash - the same
"recompute + compare" flow the CLI's `verify` command uses. It also confirms
the append-only guarantee: two writes of the same payloadHash must revert.

Skipped (not failed) whenever the chain stack (faceproof.chain/faceproof.types,
or their web3 / py-solc-x / eth-tester dependencies) isn't importable, so the
rest of the suite still runs in an environment where those heavier deps aren't
installed.
"""

import dataclasses

import pytest

try:
    from faceproof.canonical import payload_hash
    from faceproof.chain import deploy, record_match, verify_record
    from faceproof.types import Match

    CHAIN_DEPS_AVAILABLE = True
except ImportError:
    CHAIN_DEPS_AVAILABLE = False

pytestmark = pytest.mark.skipif(
    not CHAIN_DEPS_AVAILABLE,
    reason="faceproof.chain/faceproof.types (or web3/py-solc-x/eth-tester) are not available",
)

NETWORK = "local"

# record_match commits keccak(canonical_json(embedding)) as the faceHash. The
# values are irrelevant to the contract - it only ever sees the digest.
EMBEDDING = [0.01 * i for i in range(512)]


def _hash_of(match) -> str:
    """The same canonical-JSON keccak hash record_match commits on-chain."""
    return payload_hash(dataclasses.asdict(match))


def _make_match(**overrides) -> Match:
    fields = dict(
        post_url="https://example.com/post/contract-test",
        source_domain="example.com",
        title="contract test candidate",
        image_url="https://example.com/img.jpg",
        image_sha256="0" * 64,
        face_similarity=0.75,
        verified_at="2026-09-07T00:00:00+00:00",
    )
    fields.update(overrides)
    return Match(**fields)


@pytest.fixture(scope="module")
def contract_address() -> str:
    """Deploy FaceMatchRegistry once to an in-process EVM for this module's tests."""
    return deploy(network=NETWORK)


def _record(match: Match, contract_address: str):
    return record_match(
        match,
        EMBEDDING,
        network=NETWORK,
        contract_address=contract_address,
    )


def test_deploy_returns_a_contract_address(contract_address):
    assert isinstance(contract_address, str)
    assert contract_address.startswith("0x")
    assert len(contract_address) == 42


def test_record_returns_a_receipt_carrying_the_local_hash(contract_address):
    match = _make_match(post_url="https://example.com/post/receipt-shape")
    receipt = _record(match, contract_address)

    assert receipt.payload_hash == _hash_of(match)
    assert receipt.contract_address == contract_address
    assert receipt.network == NETWORK
    assert receipt.block_number > 0
    assert receipt.tx_hash


def test_record_then_verify_returns_exists_true_with_matching_hash(contract_address):
    match = _make_match()
    expected_hash = _hash_of(match)

    _record(match, contract_address)

    exists, record = verify_record(
        expected_hash, network=NETWORK, contract_address=contract_address
    )
    assert exists is True
    assert record["payload_hash"] == expected_hash
    assert record["post_url"] == match.post_url
    # 0.75 similarity -> 7500 basis points
    assert record["similarity_bps"] == 7500


def test_verify_record_with_tampered_hash_does_not_exist(contract_address):
    # Record one match for real...
    recorded = _make_match(post_url="https://example.com/post/tamper-source")
    _record(recorded, contract_address)

    # ...then ask about a *different* payload that was never written. This is
    # the tamper case: one changed field means a different hash, and the chain
    # has never heard of it.
    never_recorded = _make_match(post_url="https://example.com/post/never-recorded")
    tampered_hash = _hash_of(never_recorded)
    assert tampered_hash != _hash_of(recorded)

    exists, _ = verify_record(
        tampered_hash, network=NETWORK, contract_address=contract_address
    )
    assert exists is False


def test_recording_duplicate_payload_hash_reverts(contract_address):
    """Records are append-only: the same payloadHash can never be overwritten."""
    match = _make_match(post_url="https://example.com/post/duplicate-test")
    _record(match, contract_address)

    with pytest.raises(Exception):
        _record(match, contract_address)

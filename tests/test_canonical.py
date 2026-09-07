"""Unit tests for faceproof.canonical — the tamper-evidence foundation.

These tests don't touch torch/web3/facenet at all, so they should run in any
environment that has `eth_utils` installed (a lightweight dependency), even
before the rest of the pipeline exists.
"""

import copy

import pytest

from faceproof.canonical import canonical_json, payload_hash, similarity_bps


# ---------------------------------------------------------------------------
# canonical_json
# ---------------------------------------------------------------------------


def test_canonical_json_is_key_order_independent():
    a = {"z": 1, "a": 2, "m": {"y": 1, "x": 2}}
    b = {"a": 2, "m": {"x": 2, "y": 1}, "z": 1}
    assert canonical_json(a) == canonical_json(b)


def test_canonical_json_key_order_independent_nested_lists_of_dicts():
    a = {"items": [{"b": 1, "a": 2}, {"d": 3, "c": 4}]}
    b = {"items": [{"a": 2, "b": 1}, {"c": 4, "d": 3}]}
    assert canonical_json(a) == canonical_json(b)


def test_canonical_json_does_not_mutate_input():
    original = {"score": 0.6042, "nested": {"x": 1.0}}
    snapshot = copy.deepcopy(original)
    canonical_json(original)
    assert original == snapshot


def test_canonical_json_float_formatting_is_stable():
    # Two floats that are numerically identical but could arrive via
    # different code paths must serialize identically.
    a = {"score": 0.1 + 0.2}
    b = {"score": 0.30000000000000004}
    assert canonical_json(a) == canonical_json(b)


def test_canonical_json_float_formatting_fixed_precision():
    out = canonical_json({"score": 0.6})
    assert '"0.600000"' in out


def test_canonical_json_output_is_deterministic_across_calls():
    record = {"b": 2, "a": [1, 2, 3], "c": {"nested": True, "score": 0.123456789}}
    first = canonical_json(record)
    for _ in range(5):
        assert canonical_json(record) == first


def test_canonical_json_bool_not_coerced_to_float_string():
    out = canonical_json({"verified": True, "flagged": False})
    assert '"verified":true' in out
    assert '"flagged":false' in out


# ---------------------------------------------------------------------------
# payload_hash
# ---------------------------------------------------------------------------


def _sample_match():
    return {
        "queryImageHash": "0xabc123",
        "postUrl": "https://example.com/post/1",
        "platform": "twitter",
        "similarityBps": 6042,
        "timestamp": 1700000000,
        "faceHash": "0xdeadbeef",
    }


def test_payload_hash_deterministic_across_calls():
    match = _sample_match()
    first = payload_hash(match)
    for _ in range(5):
        assert payload_hash(match) == first


def test_payload_hash_key_order_independent():
    match = _sample_match()
    reordered = {k: match[k] for k in reversed(list(match.keys()))}
    assert payload_hash(match) == payload_hash(reordered)


def test_payload_hash_is_0x_prefixed_hex():
    h = payload_hash(_sample_match())
    assert h.startswith("0x")
    assert len(h) == 66  # 0x + 32 bytes hex
    int(h, 16)  # must parse as hex


@pytest.mark.parametrize(
    "field,new_value",
    [
        ("postUrl", "https://example.com/post/2"),
        ("platform", "instagram"),
        ("similarityBps", 6043),
        ("timestamp", 1700000001),
        ("faceHash", "0xdeadbeef0"),
        ("queryImageHash", "0xabc124"),
    ],
)
def test_payload_hash_changes_on_single_field_change(field, new_value):
    original = _sample_match()
    tampered = dict(original)
    tampered[field] = new_value
    assert payload_hash(original) != payload_hash(tampered)


def test_payload_hash_single_character_change_in_string_field_changes_hash():
    original = _sample_match()
    tampered = dict(original)
    # Flip a single character in the URL.
    tampered["postUrl"] = original["postUrl"][:-1] + (
        "2" if original["postUrl"][-1] != "2" else "3"
    )
    assert payload_hash(original) != payload_hash(tampered)


# ---------------------------------------------------------------------------
# similarity_bps
# ---------------------------------------------------------------------------


def test_similarity_bps_known_value():
    assert similarity_bps(0.6042) == 6042


def test_similarity_bps_clamps_low():
    assert similarity_bps(-0.5) == 0
    assert similarity_bps(0.0) == 0


def test_similarity_bps_clamps_high():
    assert similarity_bps(1.5) == 10000
    assert similarity_bps(1.0) == 10000


def test_similarity_bps_returns_int():
    assert isinstance(similarity_bps(0.5), int)

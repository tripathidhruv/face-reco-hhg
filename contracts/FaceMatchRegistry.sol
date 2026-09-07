// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

/// @title FaceMatchRegistry
/// @notice Append-only, tamper-evident registry linking a face-match search
///         result to a public post URL. Anyone may add a record; nobody —
///         including the deployer — can modify or remove one once it exists.
/// @dev Only opaque hashes (payloadHash, faceHash) and a public post URL are
///      ever written on-chain. The biometric embedding itself never leaves
///      the caller's machine and never reaches this contract or the chain.
///      There is no owner and no admin function by design.
contract FaceMatchRegistry {
    struct Record {
        bytes32 payloadHash;
        bytes32 faceHash;
        string postUrl;
        uint32 similarityBps;
        uint64 timestamp;
        address submitter;
    }

    /// @notice Thrown when payloadHash is the zero hash.
    error ZeroPayloadHash();

    /// @notice Thrown when similarityBps exceeds 10000 (100.00%).
    error SimilarityTooHigh(uint32 similarityBps);

    /// @notice Thrown when a record already exists for payloadHash — records
    ///         are immutable, so re-recording the same payload is rejected
    ///         rather than silently overwriting it.
    error RecordAlreadyExists(bytes32 payloadHash);

    mapping(bytes32 => Record) private _records;
    bytes32[] private _keys;

    event MatchRecorded(
        bytes32 indexed payloadHash,
        bytes32 indexed faceHash,
        string postUrl,
        uint32 similarityBps,
        address submitter
    );

    /// @notice Permanently record a face-match result.
    /// @dev Reverts if payloadHash is zero, if similarityBps > 10000, or if
    ///      a record already exists for payloadHash. Records can never be
    ///      updated or deleted after this call succeeds.
    function recordMatch(
        bytes32 payloadHash,
        bytes32 faceHash,
        string calldata postUrl,
        uint32 similarityBps
    ) external {
        if (payloadHash == bytes32(0)) revert ZeroPayloadHash();
        if (similarityBps > 10000) revert SimilarityTooHigh(similarityBps);
        if (_records[payloadHash].timestamp != 0) revert RecordAlreadyExists(payloadHash);

        _records[payloadHash] = Record({
            payloadHash: payloadHash,
            faceHash: faceHash,
            postUrl: postUrl,
            similarityBps: similarityBps,
            timestamp: uint64(block.timestamp),
            submitter: msg.sender
        });
        _keys.push(payloadHash);

        emit MatchRecorded(payloadHash, faceHash, postUrl, similarityBps, msg.sender);
    }

    /// @notice Look up a record by payload hash.
    /// @return exists Whether a record has been stored for payloadHash.
    /// @return record The stored record (zero-valued if it does not exist).
    function verifyRecord(bytes32 payloadHash) external view returns (bool exists, Record memory record) {
        record = _records[payloadHash];
        exists = record.timestamp != 0;
    }

    /// @notice Total number of records ever stored.
    function recordCount() external view returns (uint256) {
        return _keys.length;
    }

    /// @notice The payloadHash key at index i, for enumerating all records.
    function keyAt(uint256 i) external view returns (bytes32) {
        return _keys[i];
    }
}

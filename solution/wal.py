from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
import json
import struct
import zlib

FILE_MAGIC = b"SEWAL1\0\0"
FRAME_MAGIC = b"FRM1"
HEADER = struct.Struct("<4sBBHHHQQIIII")


@dataclass(frozen=True)
class WalRecord:
    lsn: int
    prev_lsn: int
    payload: bytes

    def document(self) -> dict:
        value = json.loads(self.payload.decode("utf-8"))
        if not isinstance(value, dict):
            raise ValueError("WAL payload must decode to an object")
        return value


def _read_replica(path: Path) -> list[WalRecord]:
    data = path.read_bytes()
    if not data.startswith(FILE_MAGIC):
        raise ValueError(f"bad WAL file header: {path}")

    groups: dict[tuple[int, int, int, int, int], dict[int, bytes]] = defaultdict(dict)
    offset = len(FILE_MAGIC)

    while offset < len(data):
        if offset + HEADER.size > len(data):
            break

        raw_header = data[offset : offset + HEADER.size]
        (
            magic,
            _flags,
            _reserved,
            fragment_index,
            fragment_count,
            _reserved2,
            lsn,
            prev_lsn,
            total_len,
            fragment_len,
            payload_crc,
            frame_crc,
        ) = HEADER.unpack(raw_header)
        offset += HEADER.size

        if magic != FRAME_MAGIC:
            raise ValueError(f"bad frame magic in {path} at {offset - HEADER.size}")
        if fragment_count == 0 or fragment_index >= fragment_count:
            raise ValueError(f"bad fragment index in {path}")
        expected_flags = (1 if fragment_index == 0 else 0) | (2 if fragment_index == fragment_count - 1 else 0)
        if (_flags & 0x03) != expected_flags:
            continue
        if offset + fragment_len > len(data):
            break

        fragment = data[offset : offset + fragment_len]
        offset += fragment_len

        zero_crc_header = HEADER.pack(
            magic,
            _flags,
            _reserved,
            fragment_index,
            fragment_count,
            _reserved2,
            lsn,
            prev_lsn,
            total_len,
            fragment_len,
            payload_crc,
            0,
        )
        computed_frame_crc = zlib.crc32(zero_crc_header + fragment) & 0xFFFFFFFF
        if computed_frame_crc != frame_crc:
            continue

        key = (lsn, prev_lsn, total_len, payload_crc, fragment_count)
        existing = groups[key].get(fragment_index)
        if existing is None:
            groups[key][fragment_index] = fragment
        elif existing != fragment:
            # Conflicting duplicate fragments on one replica cannot establish a record.
            groups[key][fragment_index] = b""

    records: list[WalRecord] = []
    for (lsn, prev_lsn, total_len, payload_crc, fragment_count), fragments in groups.items():
        if len(fragments) != fragment_count:
            continue
        ordered = [fragments.get(index) for index in range(fragment_count)]
        if any(fragment is None or fragment == b"" for fragment in ordered):
            continue
        payload = b"".join(ordered)
        if len(payload) != total_len:
            continue
        if (zlib.crc32(payload) & 0xFFFFFFFF) != payload_crc:
            continue
        records.append(WalRecord(lsn=lsn, prev_lsn=prev_lsn, payload=payload))

    return records


def _majority(voters: list[str]) -> int:
    return len(voters) // 2 + 1


def recover_generation(data_dir: Path, generation: str) -> list[dict]:
    replication = json.loads((data_dir / "replication.json").read_text(encoding="utf-8"))
    initial_voters = list(replication["generations"][generation]["initial_voters"])
    replicas = list(replication["replicas"])

    replica_records: dict[str, list[WalRecord]] = {}
    for replica in replicas:
        replica_records[replica] = _read_replica(data_dir / "wal" / generation / f"{replica}.wal")

    support: dict[tuple[int, int, bytes], set[str]] = defaultdict(set)
    for replica, records in replica_records.items():
        for record in records:
            support[(record.lsn, record.prev_lsn, record.payload)].add(replica)

    by_prev: dict[int, list[tuple[WalRecord, set[str]]]] = defaultdict(list)
    for (lsn, prev_lsn, payload), holders in support.items():
        by_prev[prev_lsn].append((WalRecord(lsn, prev_lsn, payload), holders))

    current_voters = initial_voters
    previous_lsn = 0
    recovered: list[dict] = []

    while True:
        eligible: list[tuple[WalRecord, dict]] = []
        for record, holders in by_prev.get(previous_lsn, []):
            if len(holders.intersection(current_voters)) < _majority(current_voters):
                continue

            document = record.document()
            if document.get("generation") not in (None, generation):
                continue
            if document.get("lsn") not in (None, record.lsn):
                continue

            if document.get("kind") == "membership":
                new_voters = list(document.get("new_voters", []))
                if not new_voters or len(set(new_voters)) != len(new_voters):
                    continue
                if any(replica not in replicas for replica in new_voters):
                    continue
                if len(holders.intersection(new_voters)) < _majority(new_voters):
                    continue

            eligible.append((record, document))

        if not eligible:
            break
        if len(eligible) != 1:
            summary = [(record.lsn, sorted(record.document().keys())) for record, _ in eligible]
            raise ValueError(f"ambiguous quorum successor after {previous_lsn} in {generation}: {summary}")

        record, document = eligible[0]
        recovered.append(document)
        previous_lsn = record.lsn
        if document.get("kind") == "membership":
            current_voters = list(document["new_voters"])

    if not recovered:
        raise ValueError(f"no durable WAL chain recovered for {generation}")
    return recovered


def load_logical_capture(data_dir: Path) -> dict[str, list[dict]]:
    replication = json.loads((data_dir / "replication.json").read_text(encoding="utf-8"))

    capture: dict[str, list[dict]] = {
        "catalog": [],
        "sessions": [],
        "transactions": [],
        "executions": [],
        "controls": [],
    }
    transaction_parts: dict[str, dict] = {}

    for generation in replication["generations"]:
        for document in recover_generation(data_dir, generation):
            kind = document.get("kind")
            if kind == "membership":
                continue
            if kind == "catalog":
                capture["catalog"].append(document["event"])
            elif kind == "session":
                capture["sessions"].append(document["event"])
            elif kind == "execution":
                capture["executions"].append(document["event"])
            elif kind == "control":
                capture["controls"].append(document["event"])
            elif kind == "tx_begin":
                txid = document["txid"]
                part = transaction_parts.setdefault(txid, {})
                part.update(
                    txid=txid,
                    generation=generation,
                    begin_lsn=int(document["lsn"]),
                )
            elif kind == "tx_finish":
                txid = document["txid"]
                part = transaction_parts.setdefault(txid, {})
                part.update(
                    txid=txid,
                    generation=generation,
                    finish_lsn=int(document["lsn"]),
                    requested_outcome=document["requested_outcome"],
                )
            else:
                raise ValueError(f"unknown durable WAL record kind: {kind}")

    required = {"txid", "generation", "begin_lsn", "finish_lsn", "requested_outcome"}
    for txid, part in transaction_parts.items():
        if set(part) != required:
            raise ValueError(f"incomplete durable transaction record: {txid}")
        capture["transactions"].append(part)

    for name in capture:
        if name == "transactions":
            capture[name].sort(key=lambda row: (row["generation"], row["begin_lsn"], row["txid"]))
        elif capture[name] and "lsn" in capture[name][0]:
            capture[name].sort(key=lambda row: (row["generation"], row["lsn"]))

    return capture

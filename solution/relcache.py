from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import json
import struct
import zlib

PAGE_SIZE = 4096
PAGE_HEADER = struct.Struct('<4sHHII')
TUPLE_HEADER = struct.Struct('<HHIIHH')
STATUS_HEADER = struct.Struct('<8sI')
STATUS_RECORD = struct.Struct('<IB3x')
SUBTRANS_HEADER = struct.Struct('<8sI')
SUBTRANS_RECORD = struct.Struct('<II')

IN_PROGRESS = 0
COMMITTED = 1
ABORTED = 2
SUB_COMMITTED = 3


def xid_precedes(left: int, right: int) -> bool:
    delta = (left - right) & 0xFFFFFFFF
    if delta & 0x80000000:
        delta -= 0x100000000
    return delta < 0


@dataclass(frozen=True)
class DescriptorTuple:
    column_id: str
    descriptor_id: str
    xmin: int
    xmax: int


class RelcacheMVCC:
    def __init__(self, data_dir: Path):
        self.base = data_dir / 'relcache'
        self._tuples: dict[str, list[DescriptorTuple]] = {}
        self._status: dict[str, dict[int, int]] = {}
        self._parents: dict[str, dict[int, int]] = {}
        self._snapshots: dict[str, list[dict]] = {}

    def _ensure_generation(self, generation: str) -> None:
        if generation in self._tuples:
            return
        self._tuples[generation] = self._read_heap(self.base / f'{generation}.heap')
        self._status[generation] = self._read_status(self.base / f'{generation}.xact')
        self._parents[generation] = self._read_subtrans(self.base / f'{generation}.subtrans')
        snapshots = []
        with (self.base / f'{generation}.snapshots.jsonl').open('r', encoding='utf-8') as handle:
            for line in handle:
                if line.strip():
                    snapshots.append(json.loads(line))
        snapshots.sort(key=lambda row: int(row['valid_from_lsn']))
        if not snapshots or int(snapshots[0]['valid_from_lsn']) != 0:
            raise ValueError(f'missing initial relcache snapshot for {generation}')
        self._snapshots[generation] = snapshots

    @staticmethod
    def _read_heap(path: Path) -> list[DescriptorTuple]:
        data = path.read_bytes()
        if len(data) % PAGE_SIZE:
            raise ValueError(f'relcache heap size is not page aligned: {path}')
        result: list[DescriptorTuple] = []
        for page_offset in range(0, len(data), PAGE_SIZE):
            page = bytearray(data[page_offset:page_offset + PAGE_SIZE])
            magic, _page_no, slot_count, free_start, stored_crc = PAGE_HEADER.unpack_from(page, 0)
            if magic != b'RCHP':
                continue
            if free_start < PAGE_HEADER.size or free_start > PAGE_SIZE:
                continue
            if PAGE_HEADER.size + 2 * slot_count > free_start:
                continue
            PAGE_HEADER.pack_into(page, 0, magic, _page_no, slot_count, free_start, 0)
            if (zlib.crc32(page) & 0xFFFFFFFF) != stored_crc:
                continue
            for slot in range(slot_count):
                tuple_offset = struct.unpack_from('<H', page, PAGE_HEADER.size + 2 * slot)[0]
                if tuple_offset < free_start or tuple_offset + TUPLE_HEADER.size > PAGE_SIZE:
                    continue
                total_len, _flags, xmin, xmax, column_len, descriptor_len = TUPLE_HEADER.unpack_from(page, tuple_offset)
                if total_len < TUPLE_HEADER.size:
                    continue
                end = tuple_offset + total_len
                if end > PAGE_SIZE or TUPLE_HEADER.size + column_len + descriptor_len != total_len:
                    continue
                cursor = tuple_offset + TUPLE_HEADER.size
                try:
                    column_id = bytes(page[cursor:cursor + column_len]).decode('utf-8')
                    cursor += column_len
                    descriptor_id = bytes(page[cursor:cursor + descriptor_len]).decode('utf-8')
                except UnicodeDecodeError:
                    continue
                result.append(DescriptorTuple(column_id, descriptor_id, xmin, xmax))
        if not result:
            raise ValueError(f'no valid relcache tuples recovered from {path}')
        return result

    @staticmethod
    def _read_status(path: Path) -> dict[int, int]:
        data = path.read_bytes()
        if len(data) < STATUS_HEADER.size:
            raise ValueError(f'truncated transaction status capture: {path}')
        magic, count = STATUS_HEADER.unpack_from(data, 0)
        if magic != b'RCXS1\0\0\0':
            raise ValueError(f'bad transaction status header: {path}')
        expected = STATUS_HEADER.size + count * STATUS_RECORD.size
        if len(data) != expected:
            raise ValueError(f'bad transaction status length: {path}')
        result = {}
        offset = STATUS_HEADER.size
        for _ in range(count):
            xid, status = STATUS_RECORD.unpack_from(data, offset)
            offset += STATUS_RECORD.size
            if status not in (IN_PROGRESS, COMMITTED, ABORTED, SUB_COMMITTED):
                raise ValueError(f'unknown relcache transaction status {status}')
            result[xid] = status
        return result

    @staticmethod
    def _read_subtrans(path: Path) -> dict[int, int]:
        data = path.read_bytes()
        if len(data) < SUBTRANS_HEADER.size:
            raise ValueError(f'truncated subtransaction capture: {path}')
        magic, count = SUBTRANS_HEADER.unpack_from(data, 0)
        if magic != b'RCST1\0\0\0':
            raise ValueError(f'bad subtransaction header: {path}')
        expected = SUBTRANS_HEADER.size + count * SUBTRANS_RECORD.size
        if len(data) != expected:
            raise ValueError(f'bad subtransaction length: {path}')
        result = {}
        offset = SUBTRANS_HEADER.size
        for _ in range(count):
            child, parent = SUBTRANS_RECORD.unpack_from(data, offset)
            offset += SUBTRANS_RECORD.size
            result[child] = parent
        return result

    def _top_xid(self, generation: str, xid: int) -> int:
        status = self._status[generation]
        parents = self._parents[generation]
        seen = set()
        current = xid
        while status.get(current) == SUB_COMMITTED:
            if current in seen or current not in parents:
                raise ValueError(f'broken subtransaction ancestry for xid {xid} in {generation}')
            seen.add(current)
            current = parents[current]
        return current

    def _effective_status(self, generation: str, xid: int) -> int:
        top = self._top_xid(generation, xid)
        return self._status[generation].get(top, IN_PROGRESS)

    def _snapshot_for(self, generation: str, lsn: int) -> dict:
        snapshots = self._snapshots[generation]
        candidates = [row for row in snapshots if int(row['valid_from_lsn']) <= lsn]
        if not candidates:
            raise ValueError(f'no relcache snapshot for {generation} at LSN {lsn}')
        return candidates[-1]

    def _xid_in_snapshot(self, generation: str, xid: int, snapshot: dict) -> bool:
        xmin = int(snapshot['xmin']) & 0xFFFFFFFF
        xmax = int(snapshot['xmax']) & 0xFFFFFFFF
        raw = xid & 0xFFFFFFFF

        if xid_precedes(raw, xmin):
            return False
        if not xid_precedes(raw, xmax):
            return True

        if bool(snapshot['suboverflowed']):
            top = self._top_xid(generation, raw)
            if xid_precedes(top, xmin):
                return False
            if not xid_precedes(top, xmax):
                return True
            return top in {int(value) & 0xFFFFFFFF for value in snapshot['xip']}

        active = {int(value) & 0xFFFFFFFF for value in snapshot['xip']}
        active.update(int(value) & 0xFFFFFFFF for value in snapshot['subxip'])
        return raw in active

    def _visible(self, generation: str, row: DescriptorTuple, snapshot: dict) -> bool:
        if self._effective_status(generation, row.xmin) != COMMITTED:
            return False
        if self._xid_in_snapshot(generation, row.xmin, snapshot):
            return False

        if row.xmax == 0:
            return True
        xmax_status = self._effective_status(generation, row.xmax)
        if xmax_status != COMMITTED:
            return True
        return self._xid_in_snapshot(generation, row.xmax, snapshot)

    def descriptor(self, generation: str, lsn: int, column_id: str) -> str:
        self._ensure_generation(generation)
        snapshot = self._snapshot_for(generation, lsn)
        visible = [
            row.descriptor_id
            for row in self._tuples[generation]
            if row.column_id == column_id and self._visible(generation, row, snapshot)
        ]
        if len(visible) != 1:
            raise ValueError(
                f'expected one visible descriptor for {generation}/{column_id} at LSN {lsn}, got {visible}'
            )
        return visible[0]

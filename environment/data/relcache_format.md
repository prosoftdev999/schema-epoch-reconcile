# Relcache descriptor capture

The files under `/app/data/relcache/` are a crash-time capture of the physical catalog descriptor state used by prepared statements. They are independent of the logical DDL stream: the logical catalog determines column names, types, defaults, and whether a column is active, while this capture determines the physical descriptor token a prepared statement was bound against.

Each generation has four files:

- `<generation>.heap` — fixed-size descriptor heap pages;
- `<generation>.xact` — transaction-status records for tuple `xmin`/`xmax` values;
- `<generation>.subtrans` — parent links for subcommitted XIDs;
- `<generation>.snapshots.jsonl` — relcache snapshots valid from specified generation-local LSNs.

All integers are little-endian. XIDs are unsigned 32-bit values and use wraparound ordering. For XIDs `a` and `b`, `a` precedes `b` when the signed 32-bit value of `(a - b)` is negative. The capture never spans half the 32-bit XID space, so this ordering is unambiguous.

## Heap pages

`<generation>.heap` is a multiple of 4096 bytes. Every page has this 16-byte header:

| Offset | Size | Field |
| --- | ---: | --- |
| 0 | 4 | ASCII `RCHP` |
| 4 | 2 | page number |
| 6 | 2 | slot count |
| 8 | 4 | `free_start` |
| 12 | 4 | IEEE CRC-32 of the complete 4096-byte page with this CRC field zeroed |

The slot array begins immediately after the header and contains `slot_count` little-endian 16-bit tuple offsets. A page whose magic, bounds, or CRC are invalid is not evidence and must be ignored as a whole.

Each tuple begins at a slot offset and has this 16-byte header:

| Offset | Size | Field |
| --- | ---: | --- |
| 0 | 2 | total tuple length |
| 2 | 2 | reserved flags |
| 4 | 4 | `xmin` |
| 8 | 4 | `xmax`, or zero when there is no deleting/replacing XID |
| 12 | 2 | UTF-8 `column_id` length |
| 14 | 2 | UTF-8 `descriptor_id` length |

The two strings follow immediately, first `column_id` then `descriptor_id`. The tuple length must exactly cover the header and both strings. Malformed tuples are ignored.

Different tuple versions can have the same `column_id`. A bad-checksum page can also contain plausible-looking descriptor strings; those bytes are not authoritative.

## Transaction status

`<generation>.xact` starts with an 8-byte magic `RCXS1\0\0\0`, then a 32-bit record count. Each following 8-byte record is:

- 32-bit XID;
- 8-bit status;
- three reserved bytes.

Status codes are:

- `0`: in progress;
- `1`: committed;
- `2`: aborted;
- `3`: subcommitted.

An XID absent from this file is treated as in progress. For a subcommitted XID, follow `<generation>.subtrans` recursively until reaching a non-subcommitted top-level XID; that top-level status determines whether the subtransaction ultimately committed or aborted. A missing parent or a cycle is an invalid capture.

`<generation>.subtrans` starts with the 8-byte magic `RCST1\0\0\0`, followed by a 32-bit count and then `count` pairs of 32-bit `(child_xid, parent_xid)` values.

## Snapshot schedule

Each line of `<generation>.snapshots.jsonl` is a JSON object containing:

`valid_from_lsn`, `snapshot_id`, `xmin`, `xmax`, `xip`, `subxip`, and `suboverflowed`.

For a prepare or execution at LSN `L`, use the snapshot with the greatest `valid_from_lsn` not greater than `L`.

Snapshot membership follows these rules for a tuple XID `x`:

1. If `x` precedes `xmin`, it is not in the snapshot's in-progress set.
2. If `x` does not precede `xmax`, it is in progress for that snapshot.
3. Otherwise, if `suboverflowed` is false, `x` is in progress exactly when the raw XID appears in `xip` or `subxip`.
4. Otherwise, resolve `x` through the subtransaction parent map to its top-level XID and test that top-level XID against `xip`. The same `xmin`/`xmax` boundary rules apply to the resolved top-level XID.

## Descriptor tuple visibility

A descriptor tuple is visible in a snapshot only when all of the following hold:

- its insertion XID ultimately committed;
- its insertion XID is not in progress for the selected snapshot;
- and either `xmax` is zero, or the deleting/replacing XID did not both ultimately commit and become visible before that snapshot.

Equivalently, a committed `xmax` hides the tuple only when that `xmax` is not in progress for the selected snapshot. An aborted or still-in-progress `xmax` does not hide it.

The shipped capture is constructed so that every column that can be bound at a prepare or checked at an execution has exactly one visible descriptor tuple at that event's snapshot.

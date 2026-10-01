# Replicated WAL capture format

The files under `/app/data/wal/<generation>/` are byte-for-byte captures from five WAL receivers. A receiver file can be incomplete, can contain records that never became durable, and can contain a locally valid record from a losing replication fork. No single receiver is authoritative.

`replication.json` lists the available replica IDs and the initial voting set for each generation. Voting-set changes are themselves WAL records.

## File and frame layout

Each receiver file starts with the eight bytes:

`53 45 57 41 4c 31 00 00` (`SEWAL1\0\0`)

The rest of the file is a sequence of frames. Integers are little-endian. Every frame has this 44-byte header followed immediately by `fragment_len` payload bytes:

| Offset | Size | Field |
| --- | ---: | --- |
| 0 | 4 | ASCII `FRM1` |
| 4 | 1 | flags: bit 0 = first fragment, bit 1 = last fragment |
| 5 | 1 | reserved, must be ignored |
| 6 | 2 | `fragment_index` |
| 8 | 2 | `fragment_count` |
| 10 | 2 | reserved, must be ignored |
| 12 | 8 | `lsn` |
| 20 | 8 | `prev_lsn` |
| 28 | 4 | total reconstructed payload length |
| 32 | 4 | `fragment_len` |
| 36 | 4 | CRC-32 of the complete reconstructed payload |
| 40 | 4 | CRC-32 of this frame |

The frame CRC is the ordinary IEEE CRC-32 used by `zlib.crc32`. Compute it over the 44-byte header with the frame-CRC field set to zero, followed by the fragment bytes. A frame with a bad frame CRC is not evidence of a record.

Fragments belonging to one candidate record have the same `lsn`, `prev_lsn`, total length, payload CRC, and fragment count. Fragment order in a receiver file is not significant. A candidate exists on that receiver only when every fragment index from zero through `fragment_count - 1` is present, the reconstructed length matches, and the reconstructed payload CRC matches.

The reconstructed payload is UTF-8 JSON. Replica agreement is on the complete reconstructed payload bytes; the capture uses a canonical JSON serialization for matching copies.

## Durable-record rule

LSNs are generation-local. A generation's first durable record has `prev_lsn = 0`. Every later durable record names the immediately preceding durable LSN in `prev_lsn`. Records not on that linked durable chain are speculative capture and do not affect database state.

For the current voting set, a normal record is durable when the identical complete candidate record is present on a strict majority of current voters. Presence on non-voters does not contribute to that majority.

A `membership` record contains `new_voters`. It becomes durable only when that same complete record is present on a strict majority of the current voters and on a strict majority of `new_voters`. Once such a record is durable, `new_voters` is the voting set for later records in that generation.

At any point in this capture there is at most one successor candidate satisfying the applicable quorum rule. Receiver-local records that fail integrity checks, minority variants at the same LSN, and minority tails after the durable head are not part of the recovered history.

## Logical record payloads

Durable payload objects use these `kind` values:

- `membership`: replication membership only; it is not a database event.
- `catalog`: `event` is a catalog change with its generation-local LSN.
- `session`: `event` is a connection or prepare event.
- `tx_begin`: contains `generation`, `txid`, and begin `lsn`.
- `tx_finish`: contains `generation`, `txid`, finish `lsn`, and `requested_outcome`.
- `execution`: `event` is one statement execution.
- `control`: `event` is a savepoint control record.

A durable `tx_begin` and durable `tx_finish` with the same `txid` form one captured transaction record. The capture contains one complete durable pair for every transaction that must appear in the transaction audit, including transactions on generations that are later discarded by promotion ancestry.

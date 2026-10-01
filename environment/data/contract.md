# Catalog epoch recovery contract

The capture describes one logical `accounts` table as it moved through several server generations. The data is synthetic, but the failure model mirrors a real failover: an old primary continued accepting work after a successor forked, catalog names were reused across schema changes, long-lived sessions kept prepared plans that were bound before later DDL, and the surviving logical history had to be reconstructed from replicated WAL receiver captures.

Read `wal_format.md` and `relcache_format.md` together with this contract. The WAL format defines receiver framing, integrity, quorum, membership changes, and the durable logical chain. The relcache format defines the physical descriptor snapshots used by prepared statements. Only durable logical WAL records participate in the database replay described below, but plan binding and plan validity also depend on the relcache evidence.

All `lsn` values are local to their generation. Within a generation, every durable catalog, session, transaction boundary, execution, control, and membership record on the recovered chain has a distinct LSN, so there is no same-LSN tie to resolve.

## 1. Recovered generation history

`promotions.json` names the final generation and the parent of every generation. Recover only the ancestry of `final_generation`.

For an ancestor generation, records survive only through the fork point recorded on the next child in that ancestry. For example, if child `g1` says `parent = g0` and `fork_parent_lsn = 4200`, only `g0` history at LSN 4200 or earlier survives into `g1`. The final generation has no upper cutoff.

A transaction survives only when its generation is on the recovered ancestry and both its begin and finish LSN are within that generation's surviving range. A transaction that does not survive is reported as `discarded_branch`, and none of its executions consume sequence values or affect table state.

Catalog and session events outside the surviving range are ignored. At the start of each child generation, table/catalog state is inherited from the parent at the fork point, but client session state is not: all connections and prepared statements start empty.

## 2. Catalog identity and DDL

Column identity is `column_id`, not the displayed name. Names can be changed and later reused by a different column ID.

The bootstrap catalog in `bootstrap.json` is the starting catalog. Apply surviving durable `catalog` WAL events in recovered-history order.

The operations mean:

- `rename`: change only the displayed name of the existing column ID.
- `add`: activate the new column ID. Its current default is materialized into every committed row at that event. If the default is `null`, existing rows receive `null`.
- `drop`: deactivate the column ID and remove that column's value from every committed row.
- `alter_default`: change the default used by later inserts. Existing values do not change.
- `alter_type`: change the displayed type. If `binary_compatible` is false, the column's prepared-plan binding epoch advances by one. If it is true, the binding epoch is unchanged. Existing stored values do not change.

Renames, added unrelated columns, and default changes do not invalidate a prepared plan by themselves.

## 3. Sessions and prepared plans

Durable `session` WAL events are ordered by generation-local LSN.

`connect` creates a fresh connection for that session and clears every older prepared statement with the same session name. `disconnect` clears the connection and its plans. `prepare` is valid only on a connected session and replaces any plan with the same `plan_name` on that connection.

At `prepare`, resolve every named column against the logical catalog visible at that exact LSN. The plan captures each resolved `column_id`, that column's logical binding epoch, and the visible physical `descriptor_id` for that column from the generation's relcache snapshot at the same LSN. The plan does not later re-resolve displayed names.

At execution, a prepared plan is valid only if every bound column ID is still active, each bound column still has the captured logical binding epoch, and the relcache descriptor visible for that column at the execution LSN is the same descriptor the plan captured at prepare time. A rename therefore preserves the logical binding, while a drop/re-add under the same displayed name does not. An incompatible type change advances the logical binding epoch. A binary-compatible type change leaves the logical epoch unchanged, but a separately captured descriptor replacement can still make an older prepared plan stale.

The descriptor heap can contain stale, aborted, and bad-page candidates. Their visibility is determined only by `relcache_format.md`; raw string matches or the physically latest-looking tuple are not sufficient.

If an execution has no live prepared plan or its plan is invalid, report that execution as `invalid_plan`. The transaction enters a failed state. Later executions in the same transaction are reported as `skipped_failed_transaction`. A failed transaction cannot commit.

## 4. Statement behavior

Parameters are positional.

For an `insert` plan, the parameter list corresponds to the plan's bound `columns` in order. Start the new row from the catalog visible at execution time: each active column receives its current default, or `null` if its default is null. Then write the supplied parameter values into the bound column IDs.

For an `update` plan, parameters for `set_columns` come first in the declared order and the final parameter is the value of the bound `key_column`. Update every matching row in the transaction's own view. The capture is constructed so the key is unique whenever the plan is valid.

For a `delete` plan, the single parameter is the value of the bound `key_column`. Delete the matching row from the transaction's own view.

An update or delete that finds no row is a successful no-op.

A transaction's own view is the committed table snapshot taken at its `begin_lsn`, plus that transaction's earlier successful statements. It does not see commits from concurrent transactions that occurred after its begin.

## 5. Savepoints and transaction control

Durable `control` WAL events contain savepoint records that interleave with executions by generation-local LSN.

- `savepoint` captures the transaction's current row view and touched-row set under the supplied name. Savepoints may be nested.
- `rollback_to` restores the row view and touched-row set captured by the most recent savepoint with that name. Savepoints created after the target are discarded, while the target savepoint remains available. The transaction's failed-plan state is restored to the state captured at that savepoint, so rolling back to a savepoint created before an `invalid_plan` clears that failure and later statements can execute.
- `release` removes the named savepoint and any savepoints nested inside it. It does not undo row changes.

A failed transaction does not execute ordinary statements: they are reported as `skipped_failed_transaction` until a `rollback_to` restores a non-failed savepoint. The capture does not issue `savepoint` or `release` while the transaction is failed.

Savepoint rollback restores transactional row state only. Sequence values already allocated by executed inserts are never restored or reused.

## 6. Generated primary keys

The primary-key column is the active column with `primary_key = true`. Its identity is stable across renames.

If a valid insert plan does not bind the primary-key column, evaluate the generation's sequence leases from `sequence_leases.jsonl`. The active lease is the eligible lease with the greatest `valid_from_lsn` not greater than the execution LSN. Allocate the next unused integer from that lease.

Sequence allocation happens after prepared-plan validation and when the insert executes. It is non-transactional: an allocated value remains consumed even if the transaction later aborts because of a requested rollback, a write conflict, or a commit-time constraint failure. An `invalid_plan`, a skipped execution, or a discarded-branch execution consumes no value.

## 7. Transaction completion and conflicts

Process surviving transaction boundaries in recovered-history order.

At `begin_lsn`, capture the current committed row state for that transaction.

At `finish_lsn`, use this outcome precedence:

1. If `requested_outcome` is `abort`, report `aborted_requested`.
2. Otherwise, if any execution made the transaction failed because of an invalid plan, report `aborted_plan`.
3. Otherwise, apply first-committer-wins conflict detection. If any logical row touched by this transaction was modified by another committed transaction after this transaction began, report `aborted_conflict`.
4. Otherwise, validate the candidate committed table against the active catalog constraints. If any constraint fails, report `aborted_constraint`.
5. Otherwise report `committed` and publish only this transaction's touched rows/deletions over the then-current committed state.

A row is considered touched only when an insert creates it, an update actually finds and changes it, or a delete actually finds and deletes it. A no-op update/delete does not create a conflict.

## 8. Commit-time constraints

For every active column:

- `nullable = false` rejects `null`.
- `unique = true` requires all non-null values to be distinct.
- `check = "email"` requires a string containing `@` and at most 120 characters.
- `check = "score_0_100"` requires an integer from 0 through 100 inclusive.
- `check = "state"` permits only `active`, `hold`, or `closed`.
- `check = "nonnegative"` requires an integer greater than or equal to zero.
- `check = "segment"` permits only `retail`, `smb`, or `enterprise`.
- `check = "region"` permits only `us`, `eu`, or `apac`.
- a null or missing `check` adds no extra constraint beyond nullability.

Boolean values are not integers for these checks.

Constraints are checked against the current committed table plus the transaction's touched rows at finish time, so a concurrent commit can cause a uniqueness failure even when the transaction's begin snapshot was valid.

## 9. Execution and transaction audit rows

Every durable `execution` WAL record must have one execution audit row, including records whose generation is later discarded by promotion ancestry.

`status` is one of:

- `discarded_branch`
- `executed`
- `invalid_plan`
- `skipped_failed_transaction`

`generated_id` is the allocated primary-key integer for an executed insert that omitted the primary key; it is `null` for every other execution.

Every durable transaction pair (`tx_begin` plus matching `tx_finish`) must have one transaction audit row, including pairs whose generation is later discarded by promotion ancestry.

`outcome` is one of:

- `discarded_branch`
- `aborted_requested`
- `aborted_plan`
- `aborted_conflict`
- `aborted_constraint`
- `committed`

## 10. Operational observations

`observations.jsonl` contains coarse row-count and numeric-sum observations captured at promotion boundaries. They are diagnostic witnesses, not stage-by-stage expected answers or a second definition of correctness. A correct reconstruction is consistent with them. If a witness disagrees with the rules above, treat the contract and raw capture as authoritative and investigate the implementation rather than changing the rules.

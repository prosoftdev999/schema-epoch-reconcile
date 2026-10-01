# Recover the account catalog after failover

The control-plane snapshot for the `accounts` database was lost during a failover. The bootstrap state, replicated WAL receiver captures, and a crash-time relcache descriptor capture under `/app/data` survived. Reconstruct the state that belongs to the promoted history and write `/app/output/schema_epoch_recovery.json`.

Read `/app/data/wal_format.md`, `/app/data/relcache_format.md`, and `/app/data/contract.md` first. They are authoritative for WAL integrity and quorum, replication membership changes, promotion ancestry, catalog identity, relcache descriptor visibility, prepared-plan binding, savepoint rollback, sequence leases, transaction snapshots, conflicts, constraints, and audit outcomes. The remaining files under `/app/data` are captured evidence.

The output is one JSON object containing these arrays:

- `schema`: every active final column, with `column_id`, `name`, `type`, `nullable`, and `ordinal`.
- `rows`: every live row in the final committed table, using the final active column names as keys. Include explicit JSON `null` values where the recovered value is null.
- `transactions`: one row for every durable transaction pair recovered from the WAL capture, with `txid` and its recovered `outcome`.
- `executions`: one row for every durable execution record recovered from the WAL capture, with `exec_id`, `status`, and `generated_id`.

Array order is not significant. Do not omit transactions or executions merely because their generation is later discarded by promotion ancestry; their audit rows are part of the recovery record.

You have 7200 seconds to complete this task. Do not cheat by using online solutions or hints specific to this task.

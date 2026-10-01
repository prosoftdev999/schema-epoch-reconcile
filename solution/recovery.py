from __future__ import annotations

import copy
from collections import defaultdict
from pathlib import Path
import json

from wal import load_logical_capture
from relcache import RelcacheMVCC


def read_json(path: Path):
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def read_jsonl(path: Path) -> list[dict]:
    rows: list[dict] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                rows.append(json.loads(line))
    return rows


def recovered_lineage(promotions: dict) -> tuple[list[str], dict[str, int]]:
    generations = {row["generation"]: row for row in promotions["generations"]}
    chain: list[str] = []

    generation = promotions["final_generation"]
    while generation is not None:
        chain.append(generation)
        generation = generations[generation]["parent"]
    chain.reverse()

    cutoffs: dict[str, int] = {}
    for index, generation in enumerate(chain):
        if index == len(chain) - 1:
            cutoffs[generation] = 10**18
        else:
            child = generations[chain[index + 1]]
            cutoffs[generation] = int(child["fork_parent_lsn"])
    return chain, cutoffs


def column_name_map(columns: dict[str, dict]) -> dict[str, str]:
    return {
        meta["name"]: column_id
        for column_id, meta in columns.items()
        if meta["active"]
    }


def active_columns(columns: dict[str, dict]) -> dict[str, dict]:
    return {
        column_id: meta
        for column_id, meta in columns.items()
        if meta["active"]
    }


def value_satisfies_check(meta: dict, value) -> bool:
    if value is None:
        return bool(meta["nullable"])

    check = meta.get("check")
    if check == "email":
        return isinstance(value, str) and "@" in value and len(value) <= 120
    if check == "score_0_100":
        return isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 100
    if check == "state":
        return value in {"active", "hold", "closed"}
    if check == "nonnegative":
        return isinstance(value, int) and not isinstance(value, bool) and value >= 0
    if check == "segment":
        return value in {"retail", "smb", "enterprise"}
    if check == "region":
        return value in {"us", "eu", "apac"}
    return True


def table_satisfies_constraints(rows: dict[int, dict], columns: dict[str, dict]) -> bool:
    live_columns = active_columns(columns)

    for row in rows.values():
        for column_id, meta in live_columns.items():
            value = row.get(column_id)
            if value is None and not meta["nullable"]:
                return False
            if not value_satisfies_check(meta, value):
                return False

    for column_id, meta in live_columns.items():
        if not meta.get("unique"):
            continue
        seen = set()
        for row in rows.values():
            value = row.get(column_id)
            if value is None:
                continue
            if value in seen:
                return False
            seen.add(value)

    return True


def apply_catalog_event(event: dict, columns: dict[str, dict], rows: dict[int, dict]) -> None:
    operation = event["op"]

    if operation == "rename":
        column_id = event["column_id"]
        columns[column_id]["name"] = event["new_name"]
        return

    if operation == "add":
        meta = copy.deepcopy(event["column"])
        meta["active"] = True
        column_id = meta["column_id"]
        columns[column_id] = meta
        for row in rows.values():
            row[column_id] = copy.deepcopy(meta.get("default"))
        return

    if operation == "drop":
        column_id = event["column_id"]
        columns[column_id]["active"] = False
        for row in rows.values():
            row.pop(column_id, None)
        return

    if operation == "alter_default":
        columns[event["column_id"]]["default"] = copy.deepcopy(event["new_default"])
        return

    if operation == "alter_type":
        meta = columns[event["column_id"]]
        meta["type"] = event["new_type"]
        if not event["binary_compatible"]:
            meta["binding_epoch"] += 1
        return

    raise ValueError(f"unknown catalog operation: {operation}")


def bind_plan(event: dict, columns: dict[str, dict], relcache: RelcacheMVCC) -> dict:
    names = column_name_map(columns)
    kind = event["kind"]
    plan: dict = {"kind": kind}
    bound_ids: list[str] = []

    if kind == "insert":
        ids = [names[name] for name in event["columns"]]
        plan["columns"] = ids
        bound_ids.extend(ids)
    elif kind == "update":
        set_ids = [names[name] for name in event["set_columns"]]
        key_id = names[event["key_column"]]
        plan["set_columns"] = set_ids
        plan["key_column"] = key_id
        bound_ids.extend(set_ids)
        bound_ids.append(key_id)
    elif kind == "delete":
        key_id = names[event["key_column"]]
        plan["key_column"] = key_id
        bound_ids.append(key_id)
    else:
        raise ValueError(f"unknown plan kind: {kind}")

    plan["binding_epochs"] = {
        column_id: columns[column_id]["binding_epoch"]
        for column_id in bound_ids
    }
    plan["descriptor_ids"] = {
        column_id: relcache.descriptor(event["generation"], int(event["lsn"]), column_id)
        for column_id in bound_ids
    }
    return plan


def plan_is_valid(
    plan: dict,
    columns: dict[str, dict],
    relcache: RelcacheMVCC,
    generation: str,
    lsn: int,
) -> bool:
    for column_id, captured_epoch in plan["binding_epochs"].items():
        meta = columns.get(column_id)
        if meta is None or not meta["active"]:
            return False
        if meta["binding_epoch"] != captured_epoch:
            return False
        if relcache.descriptor(generation, lsn, column_id) != plan["descriptor_ids"][column_id]:
            return False
    return True


class Recovery:
    def __init__(self, data_dir: Path):
        self.promotions = read_json(data_dir / "promotions.json")
        self.bootstrap = read_json(data_dir / "bootstrap.json")
        logical = load_logical_capture(data_dir)
        self.catalog = logical["catalog"]
        self.session_events = logical["sessions"]
        self.transactions = logical["transactions"]
        self.executions = logical["executions"]
        self.tx_control = logical["controls"]
        self.leases = read_jsonl(data_dir / "sequence_leases.jsonl")
        self.relcache = RelcacheMVCC(data_dir)

        self.catalog_by_generation = defaultdict(list)
        self.sessions_by_generation = defaultdict(list)
        self.transactions_by_generation = defaultdict(list)
        self.executions_by_generation = defaultdict(list)
        self.control_by_generation = defaultdict(list)
        self.leases_by_generation = defaultdict(list)

        for row in self.catalog:
            self.catalog_by_generation[row["generation"]].append(row)
        for row in self.session_events:
            self.sessions_by_generation[row["generation"]].append(row)
        for row in self.transactions:
            self.transactions_by_generation[row["generation"]].append(row)
        for row in self.executions:
            self.executions_by_generation[row["generation"]].append(row)
        for row in self.tx_control:
            self.control_by_generation[row["generation"]].append(row)
        for row in self.leases:
            self.leases_by_generation[row["generation"]].append(row)

    def run(self) -> dict:
        lineage, cutoffs = recovered_lineage(self.promotions)

        columns: dict[str, dict] = {}
        for original in self.bootstrap["columns"]:
            meta = copy.deepcopy(original)
            meta["active"] = True
            columns[meta["column_id"]] = meta

        rows = {
            row["c1"]: copy.deepcopy(row)
            for row in self.bootstrap["rows"]
        }

        transaction_outcomes = {
            row["txid"]: "discarded_branch"
            for row in self.transactions
        }
        execution_outcomes = {
            row["exec_id"]: {"status": "discarded_branch", "generated_id": None}
            for row in self.executions
        }

        commit_clock = 0
        last_modified = {key: 0 for key in rows}

        for generation in lineage:
            rows, commit_clock = self._replay_generation(
                generation=generation,
                cutoff=cutoffs[generation],
                columns=columns,
                rows=rows,
                transaction_outcomes=transaction_outcomes,
                execution_outcomes=execution_outcomes,
                commit_clock=commit_clock,
                last_modified=last_modified,
            )

        return self._format_result(
            columns,
            rows,
            transaction_outcomes,
            execution_outcomes,
        )

    def _replay_generation(
        self,
        generation: str,
        cutoff: int,
        columns: dict[str, dict],
        rows: dict[int, dict],
        transaction_outcomes: dict[str, str | None],
        execution_outcomes: dict[str, dict],
        commit_clock: int,
        last_modified: dict[int, int],
    ) -> tuple[dict[int, dict], int]:
        sessions: dict[str, dict] = {}
        transactions: dict[str, dict] = {}

        lease_next = {
            lease["lease_id"]: lease["start"]
            for lease in self.leases_by_generation[generation]
        }

        surviving_txids = {
            tx["txid"]
            for tx in self.transactions_by_generation[generation]
            if tx["begin_lsn"] <= cutoff and tx["finish_lsn"] <= cutoff
        }

        events: list[tuple[int, str, dict]] = []
        for event in self.catalog_by_generation[generation]:
            if event["lsn"] <= cutoff:
                events.append((event["lsn"], "catalog", event))

        for event in self.sessions_by_generation[generation]:
            if event["lsn"] <= cutoff:
                events.append((event["lsn"], "session", event))

        for tx in self.transactions_by_generation[generation]:
            if tx["txid"] not in surviving_txids:
                continue
            transaction_outcomes[tx["txid"]] = None
            events.append((tx["begin_lsn"], "begin", tx))
            events.append((tx["finish_lsn"], "finish", tx))

        for execution in self.executions_by_generation[generation]:
            if execution["txid"] not in surviving_txids or execution["lsn"] > cutoff:
                continue
            execution_outcomes[execution["exec_id"]] = {
                "status": None,
                "generated_id": None,
            }
            events.append((execution["lsn"], "execution", execution))

        for control in self.control_by_generation[generation]:
            if control["txid"] in surviving_txids and control["lsn"] <= cutoff:
                events.append((control["lsn"], "control", control))

        events.sort(key=lambda item: item[0])

        for _, event_kind, event in events:
            if event_kind == "catalog":
                apply_catalog_event(event, columns, rows)
                continue

            if event_kind == "session":
                self._apply_session_event(event, sessions, columns)
                continue

            if event_kind == "begin":
                transactions[event["txid"]] = {
                    "begin_commit_clock": commit_clock,
                    "working_rows": copy.deepcopy(rows),
                    "touched": set(),
                    "plan_failed": False,
                    "savepoints": [],
                }
                continue

            if event_kind == "control":
                self._apply_tx_control(event, transactions[event["txid"]])
                continue

            if event_kind == "execution":
                self._execute_statement(
                    event,
                    generation,
                    columns,
                    sessions,
                    transactions[event["txid"]],
                    execution_outcomes[event["exec_id"]],
                    lease_next,
                )
                continue

            if event_kind == "finish":
                context = transactions.pop(event["txid"])
                outcome, rows, commit_clock = self._finish_transaction(
                    event,
                    context,
                    columns,
                    rows,
                    commit_clock,
                    last_modified,
                )
                transaction_outcomes[event["txid"]] = outcome
                continue

            raise ValueError(f"unknown event kind: {event_kind}")

        return rows, commit_clock

    def _apply_session_event(self, event: dict, sessions: dict[str, dict], columns: dict[str, dict]) -> None:
        session_name = event["session"]
        action = event["event"]

        if action == "connect":
            sessions[session_name] = {"connected": True, "plans": {}}
            return

        if action == "disconnect":
            sessions[session_name] = {"connected": False, "plans": {}}
            return

        if action == "prepare":
            session = sessions.get(session_name)
            if session is None or not session["connected"]:
                raise ValueError(f"prepare on disconnected session {session_name}")
            session["plans"][event["plan_name"]] = bind_plan(event, columns, self.relcache)
            return

        raise ValueError(f"unknown session event: {action}")

    @staticmethod
    def _apply_tx_control(event: dict, transaction: dict) -> None:
        action = event["action"]
        name = event["name"]

        if action == "savepoint":
            if transaction["plan_failed"]:
                raise ValueError("capture creates a savepoint while transaction is failed")
            transaction["savepoints"].append(
                {
                    "name": name,
                    "working_rows": copy.deepcopy(transaction["working_rows"]),
                    "touched": set(transaction["touched"]),
                    "plan_failed": transaction["plan_failed"],
                }
            )
            return

        matching = [
            index
            for index, savepoint in enumerate(transaction["savepoints"])
            if savepoint["name"] == name
        ]
        if not matching:
            raise ValueError(f"missing savepoint {name}")
        index = matching[-1]

        if action == "rollback_to":
            savepoint = transaction["savepoints"][index]
            transaction["working_rows"] = copy.deepcopy(savepoint["working_rows"])
            transaction["touched"] = set(savepoint["touched"])
            transaction["plan_failed"] = savepoint["plan_failed"]
            transaction["savepoints"] = transaction["savepoints"][: index + 1]
            return

        if action == "release":
            if transaction["plan_failed"]:
                raise ValueError("capture releases a savepoint while transaction is failed")
            transaction["savepoints"] = transaction["savepoints"][:index]
            return

        raise ValueError(f"unknown transaction control action: {action}")

    def _execute_statement(
        self,
        execution: dict,
        generation: str,
        columns: dict[str, dict],
        sessions: dict[str, dict],
        transaction: dict,
        result: dict,
        lease_next: dict[str, int],
    ) -> None:
        if transaction["plan_failed"]:
            result["status"] = "skipped_failed_transaction"
            return

        session = sessions.get(execution["session"])
        plan = None
        if session is not None and session["connected"]:
            plan = session["plans"].get(execution["plan_name"])

        if plan is None or not plan_is_valid(
            plan, columns, self.relcache, generation, int(execution["lsn"])
        ):
            result["status"] = "invalid_plan"
            transaction["plan_failed"] = True
            return

        params = execution["params"]
        kind = plan["kind"]
        generated_id = None

        if kind == "insert":
            generated_id = self._execute_insert(
                execution,
                generation,
                plan,
                params,
                columns,
                transaction,
                lease_next,
            )
        elif kind == "update":
            self._execute_update(plan, params, transaction)
        elif kind == "delete":
            self._execute_delete(plan, params, transaction)
        else:
            raise ValueError(f"unknown plan kind: {kind}")

        result["status"] = "executed"
        result["generated_id"] = generated_id

    def _execute_insert(
        self,
        execution: dict,
        generation: str,
        plan: dict,
        params: list,
        columns: dict[str, dict],
        transaction: dict,
        lease_next: dict[str, int],
    ) -> int | None:
        if len(params) != len(plan["columns"]):
            raise ValueError("insert parameter count does not match plan")

        row = {
            column_id: copy.deepcopy(meta.get("default"))
            for column_id, meta in active_columns(columns).items()
        }
        for column_id, value in zip(plan["columns"], params):
            row[column_id] = value

        primary_keys = [
            column_id
            for column_id, meta in active_columns(columns).items()
            if meta.get("primary_key")
        ]
        if len(primary_keys) != 1:
            raise ValueError("capture must have exactly one active primary key")
        primary_key = primary_keys[0]

        generated_id = None
        if primary_key not in plan["columns"]:
            generated_id = self._allocate_identity(
                generation,
                execution["lsn"],
                lease_next,
            )
            row[primary_key] = generated_id

        key = row[primary_key]
        transaction["working_rows"][key] = row
        transaction["touched"].add(key)
        return generated_id

    def _allocate_identity(
        self,
        generation: str,
        lsn: int,
        lease_next: dict[str, int],
    ) -> int:
        eligible = [
            lease
            for lease in self.leases_by_generation[generation]
            if lease["valid_from_lsn"] <= lsn
        ]
        if not eligible:
            raise ValueError(f"no sequence lease for {generation} at LSN {lsn}")

        lease = max(eligible, key=lambda row: row["valid_from_lsn"])
        value = lease_next[lease["lease_id"]]
        if value > lease["end"]:
            raise ValueError(f"sequence lease exhausted: {lease['lease_id']}")
        lease_next[lease["lease_id"]] = value + 1
        return value

    @staticmethod
    def _execute_update(plan: dict, params: list, transaction: dict) -> None:
        if len(params) != len(plan["set_columns"]) + 1:
            raise ValueError("update parameter count does not match plan")

        key_value = params[-1]
        key_column = plan["key_column"]
        matching_key = next(
            (
                row_key
                for row_key, row in transaction["working_rows"].items()
                if row.get(key_column) == key_value
            ),
            None,
        )
        if matching_key is None:
            return

        for column_id, value in zip(plan["set_columns"], params[:-1]):
            transaction["working_rows"][matching_key][column_id] = value
        transaction["touched"].add(matching_key)

    @staticmethod
    def _execute_delete(plan: dict, params: list, transaction: dict) -> None:
        if len(params) != 1:
            raise ValueError("delete parameter count does not match plan")

        key_column = plan["key_column"]
        key_value = params[0]
        matching_key = next(
            (
                row_key
                for row_key, row in transaction["working_rows"].items()
                if row.get(key_column) == key_value
            ),
            None,
        )
        if matching_key is None:
            return

        del transaction["working_rows"][matching_key]
        transaction["touched"].add(matching_key)

    @staticmethod
    def _finish_transaction(
        tx: dict,
        context: dict,
        columns: dict[str, dict],
        rows: dict[int, dict],
        commit_clock: int,
        last_modified: dict[int, int],
    ) -> tuple[str, dict[int, dict], int]:
        if tx["requested_outcome"] == "abort":
            return "aborted_requested", rows, commit_clock

        if context["plan_failed"]:
            return "aborted_plan", rows, commit_clock

        if any(
            last_modified.get(key, 0) > context["begin_commit_clock"]
            for key in context["touched"]
        ):
            return "aborted_conflict", rows, commit_clock

        candidate = copy.deepcopy(rows)
        for key in context["touched"]:
            if key in context["working_rows"]:
                candidate[key] = copy.deepcopy(context["working_rows"][key])
            else:
                candidate.pop(key, None)

        if not table_satisfies_constraints(candidate, columns):
            return "aborted_constraint", rows, commit_clock

        commit_clock += 1
        for key in context["touched"]:
            last_modified[key] = commit_clock
        return "committed", candidate, commit_clock

    def _format_result(
        self,
        columns: dict[str, dict],
        rows: dict[int, dict],
        transaction_outcomes: dict[str, str],
        execution_outcomes: dict[str, dict],
    ) -> dict:
        live_columns = active_columns(columns)
        ordered_columns = sorted(
            live_columns.items(),
            key=lambda item: (item[1]["ordinal"], item[0]),
        )

        schema = [
            {
                "column_id": column_id,
                "name": meta["name"],
                "type": meta["type"],
                "nullable": meta["nullable"],
                "ordinal": meta["ordinal"],
            }
            for column_id, meta in ordered_columns
        ]

        final_rows = []
        for _, row in sorted(rows.items()):
            final_rows.append(
                {
                    meta["name"]: row.get(column_id)
                    for column_id, meta in ordered_columns
                }
            )

        transaction_audit = [
            {
                "txid": tx["txid"],
                "outcome": transaction_outcomes[tx["txid"]],
            }
            for tx in sorted(self.transactions, key=lambda row: row["txid"])
        ]

        execution_audit = [
            {
                "exec_id": execution["exec_id"],
                "status": execution_outcomes[execution["exec_id"]]["status"],
                "generated_id": execution_outcomes[execution["exec_id"]]["generated_id"],
            }
            for execution in sorted(self.executions, key=lambda row: row["exec_id"])
        ]

        return {
            "schema": schema,
            "rows": final_rows,
            "transactions": transaction_audit,
            "executions": execution_audit,
        }

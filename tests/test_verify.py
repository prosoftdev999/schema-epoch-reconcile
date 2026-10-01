import json
from pathlib import Path

ARTIFACT = Path("/app/output/schema_epoch_recovery.json")
EXPECTED = Path("/tests/expected.json")


def load_json(path: Path):
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def required_array(document: dict, name: str) -> list:
    assert isinstance(document, dict), "artifact root must be a JSON object"
    assert name in document, f"missing top-level array: {name}"
    value = document[name]
    assert isinstance(value, list), f"{name} must be an array"
    return value


def index_unique(items: list, key: str, fields: tuple[str, ...]) -> dict:
    indexed = {}
    for item in items:
        assert isinstance(item, dict), f"{key} collection contains a non-object"
        for field in fields:
            assert field in item, f"missing {field} in {key} collection"
        identity = item[key]
        assert identity not in indexed, f"duplicate {key}: {identity}"
        indexed[identity] = {field: item[field] for field in fields}
    return indexed


def test_artifact_is_valid_json():
    assert ARTIFACT.is_file(), f"missing artifact: {ARTIFACT}"
    document = load_json(ARTIFACT)
    for name in ("schema", "rows", "transactions", "executions"):
        required_array(document, name)


def test_recovered_schema_and_rows():
    actual = load_json(ARTIFACT)
    expected = load_json(EXPECTED)

    schema_fields = ("column_id", "name", "type", "nullable", "ordinal")
    actual_schema = index_unique(
        required_array(actual, "schema"),
        "column_id",
        schema_fields,
    )
    expected_schema = index_unique(
        required_array(expected, "schema"),
        "column_id",
        schema_fields,
    )
    assert actual_schema == expected_schema

    actual_rows = required_array(actual, "rows")
    expected_rows = required_array(expected, "rows")
    assert all(isinstance(row, dict) for row in actual_rows)

    expected_columns = {entry["name"] for entry in expected["schema"]}
    for row in actual_rows:
        assert set(row) == expected_columns, "row fields do not match final active schema"

    def by_primary_key(rows):
        indexed = {}
        for row in rows:
            key = row["customer_id"]
            assert key not in indexed, f"duplicate customer_id: {key}"
            indexed[key] = row
        return indexed

    assert by_primary_key(actual_rows) == by_primary_key(expected_rows)


def test_transaction_audit():
    actual = load_json(ARTIFACT)
    expected = load_json(EXPECTED)
    fields = ("txid", "outcome")
    assert index_unique(
        required_array(actual, "transactions"),
        "txid",
        fields,
    ) == index_unique(
        required_array(expected, "transactions"),
        "txid",
        fields,
    )


def test_execution_audit():
    actual = load_json(ARTIFACT)
    expected = load_json(EXPECTED)
    fields = ("exec_id", "status", "generated_id")
    assert index_unique(
        required_array(actual, "executions"),
        "exec_id",
        fields,
    ) == index_unique(
        required_array(expected, "executions"),
        "exec_id",
        fields,
    )

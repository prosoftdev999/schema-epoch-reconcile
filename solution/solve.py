from pathlib import Path
import json
import os

from recovery import Recovery


def main() -> None:
    data_dir = Path(os.environ.get("DATA_DIR", "/app/data"))
    output_path = Path(
        os.environ.get(
            "OUTPUT_PATH",
            "/app/output/schema_epoch_recovery.json",
        )
    )

    result = Recovery(data_dir).run()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as handle:
        json.dump(result, handle, indent=2, sort_keys=True)
        handle.write("\n")


if __name__ == "__main__":
    main()

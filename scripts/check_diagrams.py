"""Check published bytes against the recorded Archify browser-checked revision.

This portable CI check does not run Archify or repeat a browser inspection.
After editing a candidate, regenerate and browser-check it, then update the
sanitized verification manifest from the new local receipts.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
GATES = {"validate", "deliver", "check", "browser-check"}


def checked_path(relative: str) -> Path:
    path = (ROOT / relative).resolve()
    if Path(relative).is_absolute() or not path.is_relative_to(ROOT):
        raise ValueError("diagram path must stay inside the repository")
    if not path.is_file():
        raise ValueError(f"missing diagram file: {relative}")
    return path


def main() -> None:
    manifest = json.loads((ROOT / "docs/diagram-verification.json").read_text())
    if manifest["schema_version"] != 1 or len(manifest["diagrams"]) != 2:
        raise ValueError("expected the two verified architecture diagrams")
    repair = manifest["generator"]["project_format_repair"]
    if hashlib.sha256(checked_path(repair["script"]).read_bytes()).hexdigest() != repair["script_sha256"]:
        raise ValueError("generator format repair script changed; review and regenerate the diagrams")
    for item in manifest["diagrams"]:
        if item["type"] != "architecture" or item["quality"] != "showcase":
            raise ValueError("unexpected diagram profile")
        if set(item["gates"]) != GATES or any(v != "pass" for v in item["gates"].values()):
            raise ValueError("recorded Archify gates must all pass")
        for field in ("specification", "artifact"):
            entry = item[field]
            content = checked_path(entry["path"]).read_bytes()
            if len(content) != entry["bytes"] or hashlib.sha256(content).hexdigest() != entry["sha256"]:
                raise ValueError(f"changed {field}: regenerate and verify {entry['path']}")
        candidate = json.loads(checked_path(item["specification"]["path"]).read_text())
        if candidate["diagram_type"] != item["type"] or candidate["meta"]["output"] != item["artifact"]["path"]:
            raise ValueError("candidate output disagrees with the verification manifest")
        print(f"PASS {item['id']}: source/output bytes match the recorded browser-checked revision")
    print("Static consistency only; CI did not repeat browser or visual review.")


if __name__ == "__main__":
    main()

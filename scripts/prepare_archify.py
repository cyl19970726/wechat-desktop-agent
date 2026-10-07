"""Prepare an ignored project-local Archify 3.0.1 runtime with a format repair.

The installed runtime stays untouched. Clear spaces on otherwise empty lines
at renderer generation time, before its artifact hash and all four gates.
No validation logic, delivery state, or browser checks are changed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import tempfile


ROOT = Path(__file__).resolve().parents[1]
DIRECTORIES = ("assets", "bin", "brand-marks", "delta", "migrations", "renderers", "schemas", "scripts")
FILES = ("LICENSE", "THIRD_PARTY_NOTICES.md", "package.json", "package-lock.json", "skill-release.json")
ANCHOR = "    sourceEvidence,\n  });\n  let candidatePath;"
REPAIR = "    sourceEvidence,\n  }).replace(/^[\\t ]+$/gm, '');\n  let candidatePath;"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cli", type=Path, required=True, help="existing Archify 3.0.1 bin/archify.mjs")
    args = parser.parse_args()
    cli = args.cli.resolve(strict=True)
    if cli.name != "archify.mjs" or cli.parent.name != "bin":
        raise ValueError("expected an installed bin/archify.mjs entry")
    source = cli.parent.parent
    if json.loads((source / "package.json").read_text())["version"] != "3.0.1":
        raise ValueError("format repair is only reviewed for Archify 3.0.1")
    original = (source / "renderers/shared/cli.mjs").read_text()
    if original.count(ANCHOR) != 1:
        raise ValueError("renderer differs from the reviewed repair location")
    private = ROOT / ".local"
    private.mkdir(exist_ok=True)
    destination = private / "archify-runtime"
    if destination.exists() or destination.is_symlink():
        raise ValueError("local runtime already exists; review it before preparing a replacement")
    with tempfile.TemporaryDirectory(dir=private, prefix="archify-build-") as temporary:
        staged = Path(temporary) / "runtime"
        staged.mkdir()
        for directory in DIRECTORIES:
            shutil.copytree(source / directory, staged / directory)
        for filename in FILES:
            shutil.copy2(source / filename, staged / filename)
        repaired = original.replace(ANCHOR, REPAIR)
        (staged / "renderers/shared/cli.mjs").write_text(repaired)
        (staged / "project-formatting-patch.json").write_text(json.dumps({
            "upstream_version": "3.0.1",
            "repair": "clear spaces/tabs on empty output lines before staging and hashing HTML",
            "file": "renderers/shared/cli.mjs",
            "original_sha256": hashlib.sha256(original.encode()).hexdigest(),
            "repaired_sha256": hashlib.sha256(repaired.encode()).hexdigest(),
        }, indent=2) + "\n")
        staged.rename(destination)
    print("Prepared .local/archify-runtime/bin/archify.mjs; installed runtime unchanged")
    print("Use this local CLI for finalize and visual-check; no global skill was installed or registered.")


if __name__ == "__main__":
    main()

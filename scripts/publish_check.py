"""Reject common private artifacts and credential patterns before publication.

An additional guard, not a complete secret detector or a substitute for review.
Only inspect this repository's non-ignored files, or its Git index with --tracked.
Never print matched secret content.
"""

from __future__ import annotations

import argparse
from pathlib import Path
import re
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
PRIVATE_DIRS = {".local", "data", "evidence", "screenshots", "recordings", "logs", "visual-check"}
PRIVATE_SUFFIXES = {".db", ".sqlite", ".sqlite3", ".log", ".png", ".jpg", ".jpeg", ".mp4", ".pem", ".key", ".p12", ".p8"}
PATTERNS = {
    "credential-like token": re.compile(r"\b(?:sk-(?:proj-)?[A-Za-z0-9_-]{24,}|gh[pousr]_[A-Za-z0-9]{24,}|github_pat_[A-Za-z0-9_]{24,})\b"),
    "private key material": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "bot secret value": re.compile(r"(?i)\bbot(?:sec|secret)\s*[\"']?\s*[:=]\s*[\"']?[A-Za-z0-9_-]{16,}"),
    "machine-specific home path": re.compile(r"/(?:Users|home)/[A-Za-z0-9_.-]+/"),
    "private temporary path": re.compile(r"/var/" + r"folders/"),
    "private historical message marker": re.compile(r"\b(?:WXBOT-TEST-OK|WXOUT|WXTEST)-20\d{6}-?"),
    "phone-like personal identifier": re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)"),
}


def inspect_file(relative: str) -> list[str]:
    path = ROOT / relative
    reasons = []
    if PRIVATE_DIRS.intersection(path.relative_to(ROOT).parts):
        reasons.append("private artifact directory")
    if path.suffix.lower() in PRIVATE_SUFFIXES or path.name.endswith(".lock"):
        reasons.append("private runtime or capture file")
    if path.name.startswith(".env") and path.name != ".env.example":
        reasons.append("environment credentials file")
    if ".archify" in path.parts and path.suffix == ".json" and path.name != "candidate.json":
        reasons.append("raw local diagram receipt")
    if path.is_symlink():
        return reasons + ["symlink requires explicit publication review"]
    if not path.is_file():
        return reasons + ["missing indexed file"]
    try:
        content = path.read_bytes().decode("utf-8")
    except UnicodeDecodeError:
        return reasons + ["unreviewed binary file"]
    if "\x00" in content:
        reasons.append("binary content")
    reasons.extend(name for name, pattern in PATTERNS.items() if pattern.search(content))
    return reasons


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tracked", action="store_true", help="inspect only Git-index files")
    args = parser.parse_args()
    command = ["git", "ls-files", "-z", "--cached"]
    if not args.tracked:
        command.extend(["--others", "--exclude-standard"])
    result = subprocess.run(command, cwd=ROOT, check=True, capture_output=True)
    files = sorted(set(result.stdout.decode().rstrip("\0").split("\0")) - {""})
    if not files:
        print("FAIL no publishable files selected")
        return 1
    failures = [(name, inspect_file(name)) for name in files]
    failures = [(name, reasons) for name, reasons in failures if reasons]
    for name, reasons in failures:
        print(f"FAIL {name}: {', '.join(reasons)}")
    if failures:
        return 1
    print(f"PASS {len(files)} files: no prohibited artifacts or configured sensitive patterns found")
    print("Pattern checks supplement manual review; they cannot prove the absence of every secret.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

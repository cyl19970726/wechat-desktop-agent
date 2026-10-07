"""Strict, offline parser for text copied from a selected chat region.

The caller must verify the exact source conversation, selection count, and
clipboard change before calling. This module never reads the OS clipboard.
Display labels are not stable identities and copied text has no native IDs.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Sequence

from .transcript import Bubble


_DATE = re.compile(r"([0-9]{4})年([0-9]{2})月([0-9]{2})日 ([0-9]{2}):([0-9]{2})\Z")
MAX_CHARS = 20000
MAX_RECORDS = 50
MAX_LABEL_CHARS = 160


class Refused(ValueError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class CopiedRecord:
    sender_label: str
    time_label: str
    text: str
    direction: str = "unknown"
    provenance: str = "clipboard_text"
    native_message_id_available: bool = False


def _valid_label(value: str) -> bool:
    return bool(value and value.strip() and len(value) <= MAX_LABEL_CHARS
                and value == value.strip())


def _valid_date(value: str) -> bool:
    match = _DATE.fullmatch(value)
    if match is None:
        return False
    try:
        datetime(*(int(part) for part in match.groups()))
    except ValueError:
        return False
    return True


def parse_copied_records(text: str, expected_count: int, *, max_chars: int = MAX_CHARS
                         ) -> tuple[CopiedRecord, ...]:
    """Parse selected records; reject uncertain separators and count mismatches.

    Format: sender label, displayed minute timestamp, body; one blank line
    separates records. Body newlines and blank lines remain intact. No sender
    identity, direction, timezone, seconds, or message ID is inferred.
    """
    if (type(text) is not str or type(expected_count) is not int
            or not 1 <= expected_count <= MAX_RECORDS
            or type(max_chars) is not int or not 1 <= max_chars <= MAX_CHARS):
        raise Refused("invalid_input")
    if not text or len(text) > max_chars:
        raise Refused("size_limit")
    if "\r" in text.replace("\r\n", ""):
        raise Refused("invalid_newline")
    normalized = text.replace("\r\n", "\n")
    if any(ord(char) < 32 and char not in "\n\t" or ord(char) == 127
           for char in normalized):
        raise Refused("invalid_character")
    lines = normalized.split("\n")
    if len(lines) < 3 or not _valid_label(lines[0]) or not _valid_date(lines[1]):
        raise Refused("invalid_header")

    # Every blank line followed by a valid label/date pair could be a record
    # boundary. Do not choose an arbitrary subset to satisfy expected_count.
    boundaries = [i for i in range(3, len(lines) - 1)
                  if lines[i - 1] == "" and _valid_label(lines[i])
                  and _valid_date(lines[i + 1])]
    if len(boundaries) + 1 != expected_count:
        raise Refused("count_or_separator_ambiguous")

    starts = [0, *boundaries]
    records = []
    for index, start in enumerate(starts):
        end = starts[index + 1] - 1 if index + 1 < len(starts) else len(lines)
        body_lines = lines[start + 2:end]
        if not body_lines or not any(line.strip() for line in body_lines):
            raise Refused("missing_body")
        records.append(CopiedRecord(lines[start], lines[start + 1], "\n".join(body_lines)))
    return tuple(records)


def to_bubbles(records: Sequence[CopiedRecord], directions: Sequence[str], *,
               visually_verified: bool = False) -> tuple[Bubble, ...]:
    """Map copied records after caller visually verifies each message direction.

    Confidence 1 means exact extraction from supplied text only; it does not
    certify the sender's identity or completeness of the source selection.
    """
    items, labels = tuple(records), tuple(directions)
    if (visually_verified is not True or not items or len(items) != len(labels)
            or any(not isinstance(item, CopiedRecord)
                   or item.direction != "unknown"
                   or item.provenance != "clipboard_text"
                   or item.native_message_id_available is not False for item in items)
            or any(direction not in ("incoming", "outgoing") for direction in labels)):
        raise Refused("direction_unverified")
    return tuple(Bubble(item.text, direction, sender=item.sender_label,
                        time_candidate=item.time_label, confidence=1.0)
                 for item, direction in zip(items, labels))

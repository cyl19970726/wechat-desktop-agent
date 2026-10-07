"""Conservative offline transcript candidates for one locally bound group.

A trusted caller supplies already segmented, ordered bubbles and a human verifies
that the displayed baseline is the intended starting point. This module does not
extract OCR bubbles, operate WeChat, or prove message API level completeness.
"""

from __future__ import annotations

import json
import math
import os
import sqlite3
import stat
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Sequence


MIN_CONFIDENCE = 0.90


class TranscriptError(ValueError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class Bubble:
    text: str
    direction: str
    sender: str | None = None
    time_candidate: str | None = None
    confidence: float = 1.0
    edge_truncated: bool = False
    unsupported: bool = False

    def __post_init__(self) -> None:
        if (not isinstance(self.text, str) or not self.text
                or self.direction not in ("incoming", "outgoing", "unknown")
                or any(x is not None and (not isinstance(x, str) or not x)
                       for x in (self.sender, self.time_candidate))
                or isinstance(self.confidence, bool)
                or not isinstance(self.confidence, (int, float))
                or not math.isfinite(self.confidence)
                or not 0 <= self.confidence <= 1
                or type(self.edge_truncated) is not bool
                or type(self.unsupported) is not bool):
            raise TranscriptError("invalid_candidate")


@dataclass(frozen=True)
class StitchResult:
    coverage: str  # from_confirmed_start or partial
    reason: str
    bubbles: tuple[Bubble, ...]
    overlap: int = 0
    # Local positions in the supplied pages, never WeChat message IDs.
    older_start: int | None = None
    newer_start: int | None = None


def _normalized(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _eligible(bubble: Bubble) -> bool:
    return (bubble.direction != "unknown" and bubble.confidence >= MIN_CONFIDENCE
            and not bubble.edge_truncated and not bubble.unsupported)


def _compatible(left: Bubble, right: Bubble) -> bool:
    return (_normalized(left.text) == _normalized(right.text)
            and left.direction == right.direction
            and (left.sender is None or right.sender is None or left.sender == right.sender)
            and (left.time_candidate is None or right.time_candidate is None
                 or left.time_candidate == right.time_candidate))


def stitch_pages(older: Sequence[Bubble], newer: Sequence[Bubble]) -> StitchResult:
    """Join chronological pages only across one unique, strong suffix/prefix anchor.

    Repeated text can be separate messages. No content hash or fuzzy OCR matching
    is used. The caller must supply the actual page order and human checked bubbles.
    """
    left, right = tuple(older), tuple(newer)
    if not all(isinstance(b, Bubble) for b in left + right):
        raise TranscriptError("invalid_candidate")
    if not left or not right:
        return StitchResult("partial", "empty_page", left)
    if any(not _eligible(b) for b in left + right):
        return StitchResult("partial", "unsafe_candidate", left)
    matches = []
    for size in range(3, min(len(left), len(right)) + 1):
        a, b = left[-size:], right[:size]
        if (len({_normalized(item.text) for item in a}) >= 2
                and all(_compatible(x, y) for x, y in zip(a, b))):
            matches.append(size)
    if not matches:
        return StitchResult("partial", "anchor_missing", left)
    if len(matches) != 1:
        return StitchResult("partial", "ambiguous_anchor", left)
    size = matches[0]
    return StitchResult("from_confirmed_start", "matched", left + right[size:],
                        size, len(left) - size, 0)


def append_after_checkpoint(confirmed: Sequence[Bubble], new_page: Sequence[Bubble]) -> StitchResult:
    """Check that the latest confirmed tail appears at the next page's head."""
    return stitch_pages(confirmed, new_page)


@dataclass(frozen=True)
class TranscriptEvent:
    seq: int
    event_id: str
    bubble: Bubble


class TranscriptStore:
    """Private bounded SQLite journal for one group and one agent session.

    A user-confirmed baseline starts local coverage. Earlier history is unknown.
    OCR extraction and human validation happen outside this store. No raw pixels
    or screenshots are accepted or persisted.
    """

    def __init__(self, path: str | Path, *, group_binding: str,
                 agent_session_id: str, max_events: int = 10000):
        if (not isinstance(group_binding, str) or not group_binding.strip()
                or not isinstance(agent_session_id, str) or not agent_session_id.strip()
                or type(max_events) is not int or max_events < 3):
            raise TranscriptError("invalid_binding")
        self.path = Path(path)
        self.max_events = max_events
        parent = self.path.parent
        if (not parent.is_dir() or parent.is_symlink()
                or stat.S_IMODE(parent.stat().st_mode) & 0o077):
            raise TranscriptError("unsafe_directory")
        try:
            fd = os.open(self.path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        except OSError as exc:
            raise TranscriptError("unsafe_database") from exc
        try:
            mode = os.fstat(fd).st_mode
            if not stat.S_ISREG(mode) or stat.S_IMODE(mode) & 0o077:
                raise TranscriptError("unsafe_database")
        finally:
            os.close(fd)
        self.db = sqlite3.connect(self.path, isolation_level="DEFERRED", timeout=10)
        self.db.execute("PRAGMA journal_mode=DELETE")
        self.db.execute("CREATE TABLE IF NOT EXISTS binding ("
                        "singleton INTEGER PRIMARY KEY CHECK(singleton=1), "
                        "group_binding TEXT NOT NULL, agent_session_id TEXT NOT NULL, "
                        "baseline_confirmed INTEGER NOT NULL)")
        self.db.execute("CREATE TABLE IF NOT EXISTS events ("
                        "seq INTEGER PRIMARY KEY, event_id TEXT NOT NULL UNIQUE, "
                        "bubble_json TEXT NOT NULL)")
        row = self.db.execute("SELECT group_binding, agent_session_id FROM binding WHERE singleton=1").fetchone()
        if row is None:
            self.db.execute("INSERT INTO binding VALUES (1, ?, ?, 0)",
                            (group_binding, agent_session_id))
        elif row != (group_binding, agent_session_id):
            self.db.close()
            raise TranscriptError("binding_conflict")
        self.db.commit()

    def close(self) -> None:
        self.db.close()

    @property
    def cursor(self) -> int:
        return self.db.execute("SELECT COALESCE(MAX(seq), 0) FROM events").fetchone()[0]

    def _insert(self, bubbles: Sequence[Bubble]) -> None:
        items = tuple(bubbles)
        if any(not isinstance(b, Bubble) or not _eligible(b) for b in items):
            raise TranscriptError("unsafe_candidate")
        current = self.cursor
        if current + len(items) > self.max_events:
            raise TranscriptError("retention_limit")
        for i, bubble in enumerate(items, current + 1):
            self.db.execute("INSERT INTO events VALUES (?, ?, ?)",
                            (i, str(uuid.uuid4()), json.dumps(asdict(bubble), ensure_ascii=False)))

    def confirm_baseline(self, bubbles: Sequence[Bubble], *, user_confirmed: bool) -> int:
        if user_confirmed is not True:
            raise TranscriptError("human_confirmation_required")
        items = tuple(bubbles)
        if not items:
            raise TranscriptError("empty_baseline")
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            if self.db.execute("SELECT baseline_confirmed FROM binding").fetchone()[0]:
                raise TranscriptError("baseline_exists")
            self._insert(items)
            self.db.execute("UPDATE binding SET baseline_confirmed=1")
        return self.cursor

    def append_page(self, page: Sequence[Bubble], *, checkpoint_cursor: int) -> StitchResult:
        if type(checkpoint_cursor) is not int or checkpoint_cursor < 0:
            raise TranscriptError("invalid_cursor")
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            if not self.db.execute("SELECT baseline_confirmed FROM binding").fetchone()[0]:
                raise TranscriptError("baseline_required")
            current = self.cursor
            if checkpoint_cursor != current:
                return StitchResult("partial", "checkpoint_mismatch", self._bubbles())
            confirmed = self._bubbles()
            result = append_after_checkpoint(confirmed, page)
            if result.coverage != "from_confirmed_start":
                return result
            suffix = result.bubbles[len(confirmed):]
            self._insert(suffix)
            return result

    def _bubbles(self) -> tuple[Bubble, ...]:
        return tuple(Bubble(**json.loads(row[0])) for row in
                     self.db.execute("SELECT bubble_json FROM events ORDER BY seq"))

    def read_since(self, cursor: int) -> tuple[TranscriptEvent, ...]:
        if type(cursor) is not int or cursor < 0 or cursor > self.cursor:
            raise TranscriptError("invalid_cursor")
        return tuple(TranscriptEvent(seq, event_id, Bubble(**json.loads(payload)))
                     for seq, event_id, payload in self.db.execute(
                         "SELECT seq, event_id, bubble_json FROM events WHERE seq>? ORDER BY seq",
                         (cursor,)))

"""SQLite delivery journal; desktop and recipient evidence come from callers.

No model, network, credentials, WeChat API, or GUI implementation is supplied.
The caller must bind observations to a trusted desktop driver. Local UI success
is not recipient delivery, and no click can promise exactly-once delivery.
"""

from __future__ import annotations

import hashlib
import fcntl
import json
import math
import os
import sqlite3
import stat
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, Protocol


class Refused(Exception):
    """A safety or state precondition failed. Do not infer delivery."""


@dataclass(frozen=True)
class FullTarget:
    """Exact caller-configured binding; conversation_id is not a WeChat API ID."""

    application: str
    account_id: str
    conversation_id: str
    displayed_title: str


@dataclass(frozen=True)
class WindowIdentity:
    pid: int
    window_id: int
    bounds: tuple[int, int, int, int]


@dataclass(frozen=True)
class Observation:
    target: FullTarget
    window: WindowIdentity
    capture_healthy: bool
    observed_at: float
    draft_text: str | None = None
    visible_outgoing_text: str | None = None
    failure_marker: str | None = None


@dataclass(frozen=True)
class Session:
    account_id: str
    owner_id: str
    epoch: int


@dataclass(frozen=True)
class ReceiptEvidence:
    target: FullTarget
    intent_id: str
    payload_sha256: str
    recipient_account_id: str
    proof_id: str
    observed_at: float
    source: str = "independent_recipient"


class DesktopDriver(Protocol):
    def click_send(self, target: FullTarget, window: WindowIdentity) -> None: ...


class ReceiptVerifier(Protocol):
    def verify(self, evidence: ReceiptEvidence) -> bool: ...


def _key(value: str) -> bool:
    return (isinstance(value, str) and 0 < len(value) <= 160
            and value == value.strip() and all(32 <= ord(char) < 127 or ord(char) > 159
                                            for char in value))


def _target(target: FullTarget) -> str:
    if not isinstance(target, FullTarget) or not all(_key(value) for value in asdict(target).values()):
        raise Refused("full target identity required")
    return json.dumps(asdict(target), ensure_ascii=False, sort_keys=True)


def _window(window: WindowIdentity) -> str:
    if (not isinstance(window, WindowIdentity) or type(window.pid) is not int or window.pid <= 0
            or type(window.window_id) is not int or window.window_id <= 0
            or len(window.bounds) != 4 or any(type(value) is not int for value in window.bounds)
            or window.bounds[2] <= 0 or window.bounds[3] <= 0):
        raise Refused("exact window identity required")
    return json.dumps(asdict(window), sort_keys=True)


def _hash(text: str) -> str:
    if not isinstance(text, str) or not text or len(text) > 1000 or any(ord(c) < 32 for c in text):
        raise Refused("invalid fixed reply")
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class Outbox:
    def __init__(self, path: str | Path, *, clock: Callable[[], float] = time.time,
                 lease_seconds: float = 10.0, observation_seconds: float = 2.0):
        if (not 0 < lease_seconds <= 60 or not 0 < observation_seconds <= 10):
            raise ValueError("invalid time limits")
        self.path = str(path)
        self.clock = clock
        self.lease_seconds = lease_seconds
        self.observation_seconds = observation_seconds
        self._ensure_private_database()
        with self._connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS accounts (
                  account_id TEXT PRIMARY KEY, mode TEXT NOT NULL DEFAULT 'human',
                  epoch INTEGER NOT NULL DEFAULT 0, owner_id TEXT,
                  lease_until REAL NOT NULL DEFAULT 0,
                  invalidated_at REAL NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS intents (
                  account_id TEXT NOT NULL, intent_id TEXT NOT NULL,
                  source_message_id TEXT NOT NULL, identity_kind TEXT NOT NULL,
                  target_json TEXT NOT NULL, payload_sha256 TEXT NOT NULL,
                  marker TEXT NOT NULL, state TEXT NOT NULL,
                  window_json TEXT, draft_epoch INTEGER,
                  prepared_at REAL NOT NULL, attempted_at REAL,
                  ui_at REAL, confirmed_at REAL,
                  PRIMARY KEY(account_id, intent_id),
                  UNIQUE(account_id, source_message_id));
                CREATE TABLE IF NOT EXISTS receipt_proofs (
                  proof_id TEXT PRIMARY KEY, account_id TEXT NOT NULL,
                  intent_id TEXT NOT NULL);
            """)

    def _ensure_private_database(self) -> None:
        if not self.path or self.path == ":memory:":
            raise Refused("durable database path required")
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        except FileExistsError:
            pass
        else:
            os.close(fd)
        metadata = os.stat(self.path, follow_symlinks=False)
        if not stat.S_ISREG(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) & 0o077:
            raise Refused("private SQLite file permissions required")

    @contextmanager
    def _connect(self):
        db = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        db.row_factory = sqlite3.Row
        try:
            yield db
        finally:
            db.close()

    @contextmanager
    def _account_lock(self, account_id: str):
        # A second process cannot acquire a replacement lease while a click
        # is in flight, even if the lease deadline passes during that click.
        lock_name = hashlib.sha256(account_id.encode("utf-8")).hexdigest()[:24]
        fd = os.open(self.path + "." + lock_name + ".lock", os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def _now(self) -> float:
        value = self.clock()
        if not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
            raise Refused("clock unavailable")
        return float(value)

    def _account(self, db, account_id: str):
        row = db.execute("SELECT * FROM accounts WHERE account_id=?", (account_id,)).fetchone()
        if row is None:
            db.execute("INSERT INTO accounts(account_id) VALUES (?)", (account_id,))
            row = db.execute("SELECT * FROM accounts WHERE account_id=?", (account_id,)).fetchone()
        return row

    @staticmethod
    def _intent(db, session: Session, intent_id: str):
        row = db.execute("SELECT * FROM intents WHERE account_id=? AND intent_id=?",
                         (session.account_id, intent_id)).fetchone()
        if row is None:
            raise Refused("intent unavailable")
        return row

    def _session(self, db, session: Session, now: float):
        if not isinstance(session, Session):
            raise Refused("account lease required")
        row = self._account(db, session.account_id)
        if (row["mode"] != "automatic" or row["owner_id"] != session.owner_id
                or row["epoch"] != session.epoch or row["lease_until"] <= now):
            raise Refused("account lease or authorization stale")
        return row

    def _observation(self, observation: Observation, target_json: str, window_json: str | None,
                     account, now: float, expected_text: str | None = None) -> str:
        if (not isinstance(observation, Observation) or observation.capture_healthy is not True
                or _target(observation.target) != target_json
                or observation.target.account_id != account["account_id"]
                or not isinstance(observation.observed_at, (int, float))
                or not math.isfinite(observation.observed_at)
                or observation.observed_at <= account["invalidated_at"]
                or not 0 <= now - observation.observed_at <= self.observation_seconds):
            raise Refused("target, capture, or fresh observation not verified")
        actual_window = _window(observation.window)
        if window_json is not None and actual_window != window_json:
            raise Refused("window identity changed")
        if expected_text is not None and observation.draft_text != expected_text:
            raise Refused("draft text not exact")
        return actual_window

    def handoff_to_human(self, account_id: str) -> None:
        if not _key(account_id):
            raise Refused("account identity required")
        with self._account_lock(account_id):
            self._handoff_to_human_locked(account_id)

    def _handoff_to_human_locked(self, account_id: str) -> None:
        now = self._now()
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            account = self._account(db, account_id)
            db.execute("UPDATE accounts SET mode='human', epoch=?, owner_id=NULL, "
                       "lease_until=0, invalidated_at=? WHERE account_id=?",
                       (account["epoch"] + 1, now, account_id))
            db.commit()

    def authorize_automatic(self, account_id: str, *, human_approved: bool) -> None:
        if not _key(account_id) or human_approved is not True:
            raise Refused("fresh human authorization required")
        with self._account_lock(account_id):
            self._authorize_automatic_locked(account_id)

    def _authorize_automatic_locked(self, account_id: str) -> None:
        now = self._now()
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            account = self._account(db, account_id)
            db.execute("UPDATE accounts SET mode='automatic', epoch=?, owner_id=NULL, "
                       "lease_until=0, invalidated_at=? WHERE account_id=?",
                       (account["epoch"] + 1, now, account_id))
            db.commit()

    def acquire(self, account_id: str, owner_id: str) -> Session:
        if not _key(account_id) or not _key(owner_id):
            raise Refused("account and owner identity required")
        with self._account_lock(account_id):
            return self._acquire_locked(account_id, owner_id)

    def _acquire_locked(self, account_id: str, owner_id: str) -> Session:
        now = self._now()
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            account = self._account(db, account_id)
            if account["mode"] != "automatic":
                raise Refused("human takeover active")
            if account["lease_until"] > now and account["owner_id"] not in (None, owner_id):
                raise Refused("account owned by another writer")
            new_owner = account["owner_id"] != owner_id or account["lease_until"] <= now
            epoch = account["epoch"] + int(new_owner)
            invalidated = now if new_owner else account["invalidated_at"]
            db.execute("UPDATE accounts SET owner_id=?, lease_until=?, epoch=?, invalidated_at=? "
                       "WHERE account_id=?", (owner_id, now + self.lease_seconds,
                                             epoch, invalidated, account_id))
            db.commit()
        return Session(account_id, owner_id, epoch)

    def prepare(self, session: Session, *, intent_id: str, source_message_id: str,
                identity_kind: str, target: FullTarget, marker: str, text: str) -> str:
        if (not _key(intent_id) or not _key(source_message_id) or not _key(marker)
                or identity_kind not in ("explicit_stable", "synthetic_fixture")
                or not text.startswith(marker + " ")):
            raise Refused("explicit stable message and intent identity required")
        target_json, digest, now = _target(target), _hash(text), self._now()
        if target.account_id != session.account_id:
            raise Refused("target account mismatch")
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._session(db, session, now)
            existing = db.execute("SELECT * FROM intents WHERE account_id=? AND "
                                  "(intent_id=? OR source_message_id=?)",
                                  (session.account_id, intent_id, source_message_id)).fetchone()
            if existing is not None:
                if (existing["intent_id"] == intent_id and existing["source_message_id"] == source_message_id
                        and existing["target_json"] == target_json and existing["payload_sha256"] == digest
                        and existing["marker"] == marker and existing["identity_kind"] == identity_kind):
                    return existing["state"]
                raise Refused("message or intent identity reused with different content")
            db.execute("INSERT INTO intents(account_id,intent_id,source_message_id,identity_kind,"
                       "target_json,payload_sha256,marker,state,prepared_at) "
                       "VALUES(?,?,?,?,?,?,?,?,?)",
                       (session.account_id, intent_id, source_message_id, identity_kind,
                        target_json, digest, marker, "prepared", now))
            db.commit()
        return "prepared"

    def verify_draft(self, session: Session, intent_id: str, text: str,
                     observation: Observation) -> None:
        now, digest = self._now(), _hash(text)
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            account = self._session(db, session, now)
            intent = self._intent(db, session, intent_id)
            if intent["state"] not in ("prepared", "draft_verified") or intent["payload_sha256"] != digest:
                raise Refused("draft already handled or content mismatch")
            window_json = self._observation(observation, intent["target_json"], None,
                                            account, now, text)
            db.execute("UPDATE intents SET state='draft_verified', window_json=?, draft_epoch=? "
                       "WHERE account_id=? AND intent_id=?",
                       (window_json, session.epoch, session.account_id, intent_id))
            db.commit()

    def submit_once(self, session: Session, intent_id: str, text: str,
                    observation: Observation, driver: DesktopDriver) -> str:
        """Commit submit_attempted first; then make exactly one driver call."""
        if not isinstance(session, Session):
            raise Refused("account lease required")
        with self._account_lock(session.account_id):
            return self._submit_once_locked(session, intent_id, text, observation, driver)

    def _submit_once_locked(self, session: Session, intent_id: str, text: str,
                            observation: Observation, driver: DesktopDriver) -> str:
        now, digest = self._now(), _hash(text)
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            account = self._session(db, session, now)
            intent = self._intent(db, session, intent_id)
            if (intent["state"] != "draft_verified" or intent["draft_epoch"] != session.epoch
                    or intent["payload_sha256"] != digest):
                raise Refused("submit already attempted or draft authorization stale")
            self._observation(observation, intent["target_json"], intent["window_json"],
                              account, now, text)
            db.execute("UPDATE intents SET state='submit_attempted', attempted_at=? "
                       "WHERE account_id=? AND intent_id=?", (now, session.account_id, intent_id))
            db.commit()
        try:
            driver.click_send(observation.target, observation.window)
        except Exception:
            self._mark_indeterminate(session.account_id, intent_id)
            return "indeterminate"
        return "submit_attempted"

    def _mark_indeterminate(self, account_id: str, intent_id: str) -> None:
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("UPDATE intents SET state='indeterminate' WHERE account_id=? "
                       "AND intent_id=? AND state='submit_attempted'", (account_id, intent_id))
            db.commit()

    def record_ui_outcome(self, account_id: str, intent_id: str,
                          observation: Observation, outcome: str) -> str:
        if outcome not in ("ui_observed", "failed", "indeterminate"):
            raise Refused("unsupported UI outcome")
        now = self._now()
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            account = self._account(db, account_id)
            intent = self._intent(db, Session(account_id, "", 0), intent_id)
            if intent["state"] != "submit_attempted":
                raise Refused("no pending submit attempt")
            if outcome != "indeterminate":
                self._observation(observation, intent["target_json"], intent["window_json"],
                                  account, now)
                if observation.observed_at <= intent["attempted_at"]:
                    raise Refused("UI evidence predates submit attempt")
                if outcome == "ui_observed" and (
                        observation.visible_outgoing_text is None
                        or _hash(observation.visible_outgoing_text) != intent["payload_sha256"]):
                    raise Refused("outgoing UI text not exact")
                if outcome == "failed" and observation.failure_marker != intent["marker"]:
                    raise Refused("failed UI marker not exact")
            db.execute("UPDATE intents SET state=?, ui_at=? WHERE account_id=? AND intent_id=?",
                       (outcome, now, account_id, intent_id))
            db.commit()
        return outcome

    def confirm_recipient(self, account_id: str, intent_id: str,
                          evidence: ReceiptEvidence, verifier: ReceiptVerifier) -> None:
        """Require independent recipient evidence; local UI/OCR cannot confirm."""
        now = self._now()
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            intent = self._intent(db, Session(account_id, "", 0), intent_id)
            if (intent["state"] not in ("submit_attempted", "ui_observed", "failed", "indeterminate")
                    or not isinstance(evidence, ReceiptEvidence)
                    or evidence.source != "independent_recipient"
                    or not _key(evidence.proof_id) or not _key(evidence.recipient_account_id)
                    or evidence.recipient_account_id == account_id
                    or evidence.intent_id != intent_id
                    or _target(evidence.target) != intent["target_json"]
                    or evidence.payload_sha256 != intent["payload_sha256"]
                    or not isinstance(evidence.observed_at, (int, float))
                    or not intent["attempted_at"] <= evidence.observed_at <= now
                    or not verifier.verify(evidence)):
                raise Refused("independent recipient receipt not verified")
            try:
                db.execute("INSERT INTO receipt_proofs(proof_id,account_id,intent_id) VALUES(?,?,?)",
                           (evidence.proof_id, account_id, intent_id))
            except sqlite3.IntegrityError:
                raise Refused("recipient proof already used") from None
            db.execute("UPDATE intents SET state='recipient_confirmed', confirmed_at=? "
                       "WHERE account_id=? AND intent_id=?", (now, account_id, intent_id))
            db.commit()

    def state(self, account_id: str, intent_id: str) -> str:
        with self._connect() as db:
            row = db.execute("SELECT state FROM intents WHERE account_id=? AND intent_id=?",
                             (account_id, intent_id)).fetchone()
        if row is None:
            raise Refused("intent unavailable")
        return row["state"]

"""One locally bound group channel for an existing agent session.

The binding is a private local configuration, not a WeChat chat ID or account
verification. Native reads and writes are delegated to a separately audited
desktop backend. No synthetic fallback is used at runtime.
"""

from __future__ import annotations

import fcntl
import json
import math
import os
import stat
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, Protocol

from .clipboard_records import Refused as CopyRefused, parse_copied_records
from .outbox import FullTarget, Outbox, Refused


class ChannelError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _label(value: str) -> bool:
    return (isinstance(value, str) and 0 < len(value) <= 160 and value == value.strip()
            and all(32 <= ord(char) < 127 or ord(char) > 159 for char in value))


def _uuid(value: str) -> bool:
    try:
        return isinstance(value, str) and str(uuid.UUID(value)) == value
    except (ValueError, AttributeError):
        return False


@dataclass(frozen=True)
class Binding:
    version: int
    channel_id: str
    account_binding_id: str
    group_title: str
    agent_session_id: str
    paused: bool

    @property
    def target(self) -> FullTarget:
        return FullTarget("com.tencent.xinWeChat", self.account_binding_id,
                          self.channel_id, self.group_title)


class NativeBackend(Protocol):
    def diagnose_header(self, *, activate_existing: bool = False) -> dict: ...
    def read(self) -> dict: ...
    def page(self, scroll_delta: int) -> dict: ...
    def dispatch(self, outbox: Outbox, session, request_id: str, text: str) -> dict: ...


def _default_backend(target: FullTarget, state_dir: Path) -> NativeBackend:
    # No import at module load: a missing or unsupported Mac integration must
    # fail closed, never fall back to a fake result.
    from .native import NativeChannelBackend
    return NativeChannelBackend(target, state_dir)


class Channel:
    def __init__(self, state_dir: str | Path | None = None, *,
                 backend_factory: Callable[[FullTarget, Path], NativeBackend] | None = None,
                 clock: Callable[[], float] = time.time):
        self.state_dir = Path(state_dir) if state_dir is not None else Path.cwd() / ".local" / "wechat-desktop-agent"
        self.backend_factory = backend_factory or _default_backend
        self.clock = clock
        self.binding_file = self.state_dir / "channel.json"
        self.db_file = self.state_dir / "outbox.sqlite3"
        self.lock_file = self.state_dir / "desktop.lock"

    def _directory(self, *, create: bool) -> None:
        if create:
            self.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            metadata = self.state_dir.lstat()
        except FileNotFoundError:
            raise ChannelError("not_initialized", "local channel is not initialized") from None
        if not stat.S_ISDIR(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) & 0o077:
            raise ChannelError("unsafe_state_dir", "private local state directory required")

    def _lock(self):
        self._directory(create=True)
        fd = os.open(self.lock_file, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        metadata = os.fstat(fd)
        if not stat.S_ISREG(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) & 0o077:
            os.close(fd)
            raise ChannelError("unsafe_state_dir", "private local lock required")
        return fd

    def _load(self) -> Binding:
        try:
            self._directory(create=False)
        except OSError:
            raise ChannelError("unsafe_state_dir", "private local state directory required") from None
        try:
            fd = os.open(self.binding_file, os.O_RDONLY | os.O_NOFOLLOW |
                         getattr(os, "O_CLOEXEC", 0))
        except FileNotFoundError:
            raise ChannelError("not_initialized", "local channel is not initialized") from None
        except OSError:
            raise ChannelError("unsafe_binding", "private local binding required") from None
        try:
            metadata = os.fstat(fd)
            if not stat.S_ISREG(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) & 0o077:
                raise ChannelError("unsafe_binding", "private local binding required")
            handle = os.fdopen(fd, "r", encoding="utf-8")
            fd = None
            with handle:
                raw = json.load(handle)
            binding = Binding(**raw)
        except ChannelError:
            raise
        except OSError:
            raise ChannelError("unsafe_binding", "private local binding required") from None
        except (ValueError, TypeError):
            raise ChannelError("invalid_binding", "local binding is malformed") from None
        finally:
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    raise ChannelError("unsafe_binding", "private local binding required") from None
        if (set(raw) != set(asdict(binding)) or binding.version != 1
                or not _uuid(binding.channel_id)
                or type(binding.paused) is not bool
                or not all(_label(value) for value in (binding.account_binding_id,
                                                       binding.group_title, binding.agent_session_id))):
            raise ChannelError("invalid_binding", "local binding is malformed")
        return binding

    def _replace_binding(self, binding: Binding) -> None:
        temporary = self.state_dir / (".channel-" + str(uuid.uuid4()) + ".tmp")
        fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
        try:
            payload = json.dumps(asdict(binding), ensure_ascii=False, sort_keys=True).encode("utf-8")
            os.write(fd, payload)
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(temporary, self.binding_file)
        directory_fd = os.open(self.state_dir, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)

    @staticmethod
    def _session(binding: Binding, session_id: str) -> None:
        if session_id != binding.agent_session_id:
            raise ChannelError("session_mismatch", "agent session does not match local binding")

    def _outbox(self) -> Outbox:
        try:
            return Outbox(self.db_file, clock=self.clock)
        except Refused:
            raise ChannelError("unsafe_state", "private outbox unavailable") from None

    def init(self, *, group_title: str, account_binding_id: str,
             agent_session_id: str) -> dict:
        if not all(_label(value) for value in (group_title, account_binding_id, agent_session_id)):
            raise ChannelError("invalid_binding", "exact local binding fields required")
        fd = self._lock()
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            if self.binding_file.exists() or self.binding_file.is_symlink():
                existing = self._load()
                if (existing.group_title, existing.account_binding_id, existing.agent_session_id) != (
                        group_title, account_binding_id, agent_session_id):
                    raise ChannelError("binding_exists", "only one group binding is supported")
                return {"status": "already_initialized", "channel_id": existing.channel_id}
            binding = Binding(1, str(uuid.uuid4()), account_binding_id, group_title,
                              agent_session_id, True)
            config_fd = os.open(self.binding_file,
                                os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
            try:
                payload = json.dumps(asdict(binding), ensure_ascii=False, sort_keys=True).encode("utf-8")
                os.write(config_fd, payload)
                os.fsync(config_fd)
            finally:
                os.close(config_fd)
            self._outbox()  # Create a private durable journal in human mode.
            return {"status": "initialized", "channel_id": binding.channel_id}
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def status(self) -> dict:
        binding = self._load()
        mode = self._outbox().account_mode(binding.account_binding_id)
        return {"status": "configured", "channel_id": binding.channel_id,
                "agent_session_id": binding.agent_session_id,
                "mode": mode, "paused": binding.paused,
                "account_binding_verified": False,
                "native_capability_verified": False}

    def pause(self) -> dict:
        fd = self._lock()
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            binding = self._load()
            if not binding.paused:
                self._replace_binding(Binding(**{**asdict(binding), "paused": True}))
            self._outbox().handoff_to_human(binding.account_binding_id)
            return {"status": "paused", "channel_id": binding.channel_id}
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def resume(self, *, session_id: str) -> dict:
        fd = self._lock()
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            binding = self._load()
            self._session(binding, session_id)
            self._outbox().authorize_automatic(binding.account_binding_id,
                                                human_approved=True)
            self._replace_binding(Binding(**{**asdict(binding), "paused": False}))
            return {"status": "automatic_enabled", "channel_id": binding.channel_id,
                    "account_binding_verified": False}
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    @staticmethod
    def _snapshot(binding: Binding, snapshot: dict, *, scroll_delta: int | None = None) -> dict:
        if (not isinstance(snapshot, dict) or snapshot.get("target_verified") is not True
                or snapshot.get("title_verified") is not True
                or snapshot.get("capture_healthy") is not True
                or snapshot.get("coverage") != "viewport_only"
                or snapshot.get("channel_id") != binding.channel_id
                or snapshot.get("session_id") != binding.agent_session_id
                or snapshot.get("message_id_available") is not False
                or snapshot.get("deduplication") != "unavailable"
                or (scroll_delta is not None and snapshot.get("scroll_delta") != scroll_delta)
                or not isinstance(snapshot.get("lines"), list)):
            raise ChannelError("native_blocked", "native read not verifiable")
        window = snapshot.get("window")
        if (not isinstance(window, dict) or set(window) != {"pid", "window_id", "bounds"}
                or type(window["pid"]) is not int or window["pid"] <= 0
                or type(window["window_id"]) is not int or window["window_id"] <= 0
                or not isinstance(window["bounds"], (list, tuple)) or len(window["bounds"]) != 4
                or any(type(value) is not int for value in window["bounds"])
                or window["bounds"][2] <= 0 or window["bounds"][3] <= 0
                or type(snapshot.get("observed_at")) not in (int, float)
                or not math.isfinite(snapshot["observed_at"]) or snapshot["observed_at"] <= 0
                or not isinstance(snapshot.get("viewport_hash"), str)
                or len(snapshot["viewport_hash"]) != 64
                or any(char not in "0123456789abcdef" for char in snapshot["viewport_hash"])):
            raise ChannelError("native_blocked", "native viewport evidence malformed")
        lines = []
        for item in snapshot["lines"]:
            if (not isinstance(item, dict) or not isinstance(item.get("text"), str)
                    or item.get("direction") != "unknown"
                    or not isinstance(item.get("confidence"), (float, int))
                    or not math.isfinite(item["confidence"])
                    or not 0 <= item["confidence"] <= 1
                    or not isinstance(item.get("box"), (list, tuple))
                    or len(item["box"]) != 4
                    or any(not isinstance(value, (float, int)) or not math.isfinite(value)
                           or not 0 <= value <= 1 for value in item["box"])):
                raise ChannelError("native_blocked", "native OCR observation malformed")
            # Raw OCR lines are not messages, sender claims, or stable IDs.
            lines.append({"text": item["text"], "confidence": item["confidence"],
                          "box": list(item["box"]), "direction": "unknown"})
        return {"status": "snapshot", "channel_id": binding.channel_id,
                "agent_session_id": binding.agent_session_id,
                "lines": lines, "coverage": "viewport_only",
                "observed_at": snapshot["observed_at"],
                "window": {"pid": window["pid"], "window_id": window["window_id"],
                           "bounds": list(window["bounds"])},
                "viewport_hash": snapshot["viewport_hash"],
                "complete_context": False, "marks_all_read": False,
                "message_id_available": False, "deduplication": "unavailable",
                "cursor_available": False, "subscription": False}

    def diagnose_header(self, *, session_id: str, activate_existing: bool = False) -> dict:
        """Return bounded header diagnostics, without granting read or send."""
        if type(activate_existing) is not bool:
            raise ChannelError("invalid_arguments", "explicit activation flag required")
        binding = self._load()
        self._session(binding, session_id)
        try:
            report = self.backend_factory(binding.target, self.state_dir).diagnose_header(
                activate_existing=activate_existing)
        except Exception:
            raise ChannelError("native_blocked", "native header diagnostic unavailable") from None
        phases = {"binding", "layout", "session", "activate", "window", "capture", "ocr", "recheck", "complete"}
        reasons = {"none", "precondition_unavailable", "title_mismatch", "automatic_title_gate_not_passed"}
        fields = {"status", "observed_at", "window", "capture_healthy", "title_exact",
                  "matching_title_confidences", "automatic_title_gate_passed", "identity_verified",
                  "body_read", "activation_requested", "activated_existing", "phase", "reason"}
        if (not isinstance(report, dict) or not fields <= report.keys()
                or report["status"] not in ("header_observed", "blocked")
                or not isinstance(report["phase"], str) or not isinstance(report["reason"], str)
                or report["phase"] not in phases or report["reason"] not in reasons
                or type(report["observed_at"]) not in (int, float)
                or not math.isfinite(report["observed_at"]) or report["observed_at"] <= 0
                or any(type(report[k]) is not bool for k in (
                    "capture_healthy", "title_exact", "automatic_title_gate_passed", "activation_requested"))
                or report["identity_verified"] is not False or report["body_read"] is not False
                or report["activation_requested"] != activate_existing
                or (report["activated_existing"] is not None
                    and type(report["activated_existing"]) is not bool)
                or not isinstance(report["matching_title_confidences"], list)
                or any(type(c) not in (int, float) or not math.isfinite(c) or not 0 <= c <= 1
                       for c in report["matching_title_confidences"])):
            raise ChannelError("native_blocked", "native header diagnostic malformed")
        window = report["window"]
        if window is not None and (not isinstance(window, dict)
                or set(window) != {"pid", "window_id", "bounds"}
                or any(type(window[k]) is not int or window[k] <= 0 for k in ("pid", "window_id"))
                or not isinstance(window["bounds"], (list, tuple)) or len(window["bounds"]) != 4
                or any(type(v) is not int for v in window["bounds"])
                or any(v <= 0 for v in window["bounds"][2:])):
            raise ChannelError("native_blocked", "native header window malformed")
        if report["status"] == "header_observed" and (
                window is None or not report["capture_healthy"] or not report["title_exact"]
                or not report["matching_title_confidences"] or report["phase"] != "complete"):
            raise ChannelError("native_blocked", "native header diagnostic inconsistent")
        # Whitelist fields so OCR text or unexpected backend details stay out.
        return {**{k: report[k] for k in fields}, "channel_id": binding.channel_id,
                "agent_session_id": binding.agent_session_id,
                "account_binding_verified": False, "read_authorized": False,
                "send_authorized": False}

    def read(self, *, session_id: str) -> dict:
        binding = self._load()
        self._session(binding, session_id)
        try:
            snapshot = self.backend_factory(binding.target, self.state_dir).read()
        except Exception:
            raise ChannelError("native_blocked", "native read unavailable") from None
        return self._snapshot(binding, snapshot)

    def page(self, *, session_id: str, delta: int) -> dict:
        if type(delta) is not int or delta not in (-3, -2, -1, 1, 2, 3):
            raise ChannelError("invalid_page_delta", "page delta must be -3, -2, -1, 1, 2, or 3")
        binding = self._load()
        self._session(binding, session_id)
        try:
            snapshot = self.backend_factory(binding.target, self.state_dir).page(delta)
        except Exception:
            raise ChannelError("native_blocked", "native page unavailable") from None
        result = self._snapshot(binding, snapshot, scroll_delta=delta)
        result["scroll_delta"] = delta
        return result

    def parse_copy(self, *, session_id: str, expected_count: int, text: str) -> dict:
        """Parse caller-supplied text; this does not inspect its clipboard source."""
        binding = self._load()
        self._session(binding, session_id)
        try:
            records = parse_copied_records(text, expected_count)
        except CopyRefused as error:
            raise ChannelError(error.code, "supplied copied text rejected") from None
        return {"status": "parsed_copy", "channel_id": binding.channel_id,
                "agent_session_id": binding.agent_session_id,
                "records": [asdict(record) for record in records],
                "source_verified": False, "coverage": "provided_copy_only",
                "history_complete": False, "cursor_available": False,
                "event_ids_available": False}

    def send(self, *, session_id: str, request_id: str, text: str,
             synthetic_test: bool = False) -> dict:
        if not _uuid(request_id):
            raise ChannelError("invalid_request_id", "canonical UUID request_id required")
        if synthetic_test is not True:
            raise ChannelError("test_mode_required", "only explicitly marked synthetic test sends are enabled")
        binding = self._load()
        self._session(binding, session_id)
        if binding.paused:
            raise ChannelError("paused", "human takeover is active")
        full_text = request_id + " " + text
        try:
            backend = self.backend_factory(binding.target, self.state_dir)
        except Exception:
            raise ChannelError("native_blocked", "native dispatch unavailable") from None
        try:
            outbox = self._outbox()
            try:
                session = outbox.acquire(binding.account_binding_id, "channel:" + binding.channel_id)
                result = outbox.prepare(session, intent_id=request_id,
                        source_message_id="agent-request:" + request_id,
                        identity_kind="agent_request", target=binding.target,
                        marker=request_id, text=full_text,
                        tag_existing=True)
            except Refused:
                raise ChannelError("request_refused", "request identity, content, or lease refused") from None
            if result.startswith("existing:"):
                state = result.split(":", 1)[1]
                return {"status": "existing", "channel_id": binding.channel_id,
                        "journal_state": state, "request_id": request_id,
                        "delivery": "unverified", "requires_manual_review": state == "prepared"}
            try:
                dispatch_result = backend.dispatch(outbox, session, request_id, full_text)
            except Exception:
                raise ChannelError("native_blocked", "native dispatch unavailable; inspect before any new request") from None
            journal_state = outbox.state(binding.account_binding_id, request_id)
            if journal_state == "prepared" and isinstance(dispatch_result, dict) and dispatch_result.get("status") == "indeterminate":
                return {"status": "indeterminate", "channel_id": binding.channel_id,
                        "journal_state": "prepared", "request_id": request_id,
                        "delivery": "unverified", "requires_manual_review": True}
            if journal_state not in ("submit_attempted", "ui_observed", "failed",
                                     "indeterminate", "recipient_confirmed"):
                raise ChannelError("native_blocked", "native dispatch did not reach a durable attempt")
            return {"status": "attempt_recorded", "channel_id": binding.channel_id,
                    "journal_state": journal_state,
                    "request_id": request_id, "delivery": "unverified"}
        except ChannelError:
            raise

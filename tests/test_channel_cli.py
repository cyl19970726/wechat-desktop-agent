import io
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from wechat_desktop_agent import Observation, WindowIdentity
from wechat_desktop_agent import channel as channel_module
from wechat_desktop_agent.channel import Channel, ChannelError
from wechat_desktop_agent.cli import main


REQUEST = "5d814faa-e0fe-4fe6-a6bc-e882b527da48"
COPIED = ("Alex\n2026年10月07日 12:30\nFirst synthetic request\n\n"
          "Blair\n2026年10月07日 12:31\nSecond synthetic request")


class Clock:
    def __init__(self):
        self.value = 1000.0

    def __call__(self):
        return self.value

    def advance(self):
        self.value += 0.1
        return self.value


class FakeDesktop:
    def __init__(self, target, state_dir, clock, counts):
        self.target = target
        self.clock = clock
        self.counts = counts

    def diagnose_header(self, *, activate_existing=False):
        self.counts["doctor"] += 1
        self.counts["activation_requested"] = activate_existing
        return {"status": "header_observed", "observed_at": self.clock(),
                "window": {"pid": 101, "window_id": 202, "bounds": [10, 20, 900, 800]},
                "capture_healthy": True, "title_exact": True,
                "matching_title_confidences": [0.5], "automatic_title_gate_passed": False,
                "identity_verified": False, "body_read": False,
                "activation_requested": activate_existing, "activated_existing": None,
                "phase": "complete", "reason": "automatic_title_gate_not_passed",
                "recognized_text": "private header must not be echoed"}

    def read(self):
        self.counts["read"] += 1
        return {"target_verified": True, "title_verified": True,
                "capture_healthy": True, "coverage": "viewport_only",
                "channel_id": self.target.conversation_id,
                "session_id": "existing-agent-session",
                "message_id_available": False, "deduplication": "unavailable",
                "observed_at": self.clock(), "viewport_hash": "a" * 64,
                "window": {"pid": 101, "window_id": 202,
                           "bounds": [10, 20, 900, 800]},
                "lines": [{"text": "synthetic request", "confidence": 0.9,
                           "box": [0.1, 0.2, 0.3, 0.4], "direction": "unknown",
                           "message_id": "weak-ocr-fingerprint"}]}

    def page(self, scroll_delta):
        self.counts["page"] += 1
        return {**self.read(), "scroll_delta": scroll_delta}

    def dispatch(self, outbox, session, request_id, text):
        self.counts["dispatch"] += 1
        window = WindowIdentity(101, 202, (10, 20, 900, 800))
        outbox.verify_draft(session, request_id, text,
            Observation(self.target, window, True, self.clock.advance(), draft_text=text))

        class Click:
            def click_send(self, target, window):
                pass  # Synthetic test driver; no GUI action.

        outbox.submit_once(session, request_id, text,
            Observation(self.target, window, True, self.clock.advance(), draft_text=text),
            Click())
        return {"status": "synthetic_driver_return_is_not_delivery"}


class ChannelTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state_dir = Path(self.temp.name) / "private"
        self.clock = Clock()
        self.counts = {"read": 0, "page": 0, "dispatch": 0, "constructed": 0, "doctor": 0}

        def factory(target, state_dir):
            self.counts["constructed"] += 1
            return FakeDesktop(target, state_dir, self.clock, self.counts)

        self.factory = factory
        self.channel = Channel(self.state_dir, backend_factory=factory, clock=self.clock)

    def init(self):
        return self.channel.init(group_title="Synthetic Team", account_binding_id="local-test-account",
                                 agent_session_id="existing-agent-session")

    def test_single_private_binding_and_stable_ids(self):
        first = self.init()
        self.assertEqual(first["status"], "initialized")
        self.assertEqual(self.init(), {"status": "already_initialized",
                                        "channel_id": first["channel_id"]})
        with self.assertRaisesRegex(ChannelError, "only one group"):
            self.channel.init(group_title="Other Team", account_binding_id="local-test-account",
                              agent_session_id="existing-agent-session")
        self.assertEqual(stat.S_IMODE(self.state_dir.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE((self.state_dir / "channel.json").stat().st_mode), 0o600)
        self.assertEqual(self.channel.status()["agent_session_id"], "existing-agent-session")
        self.assertTrue(self.channel.status()["paused"])
        self.assertFalse(self.channel.status()["account_binding_verified"])

    def test_binding_symlink_is_refused_and_path_swap_keeps_original_fd(self):
        original = self.init()["channel_id"]
        binding_path = self.state_dir / "channel.json"
        replacement_path = self.state_dir / "replacement.json"
        replacement = json.loads(binding_path.read_text(encoding="utf-8"))
        replacement["channel_id"] = REQUEST
        replacement_path.write_text(json.dumps(replacement), encoding="utf-8")
        replacement_path.chmod(0o600)

        binding_path.rename(self.state_dir / "saved.json")
        binding_path.symlink_to(replacement_path)
        with self.assertRaises(ChannelError) as linked:
            self.channel._load()
        self.assertEqual(linked.exception.code, "unsafe_binding")

        binding_path.unlink()
        (self.state_dir / "saved.json").rename(binding_path)
        moved_path = self.state_dir / "moved.json"
        swapped = False
        real_lstat = Path.lstat
        real_fstat = os.fstat

        def replace_path():
            nonlocal swapped
            if not swapped:
                swapped = True
                binding_path.rename(moved_path)
                binding_path.symlink_to(replacement_path)

        def lstat_after_check(path):
            result = real_lstat(path)
            if path == binding_path:
                replace_path()
            return result

        def fstat_after_open(fd):
            result = real_fstat(fd)
            replace_path()
            return result

        # Old code checked Path.lstat then reopened the path; new code checks
        # the descriptor it already opened. Both triggers model the same swap.
        with mock.patch.object(Path, "lstat", lstat_after_check), \
                mock.patch.object(channel_module.os, "fstat", fstat_after_open):
            loaded = self.channel._load()
        self.assertTrue(swapped)
        self.assertEqual(loaded.channel_id, original)
        self.assertNotEqual(loaded.channel_id, replacement["channel_id"])

    def test_pause_resume_and_session_mismatch_block_dispatch(self):
        self.init()
        with self.assertRaises(ChannelError) as wrong:
            self.channel.read(session_id="other-session")
        self.assertEqual(wrong.exception.code, "session_mismatch")
        with self.assertRaises(ChannelError) as paused:
            self.channel.send(session_id="existing-agent-session", request_id=REQUEST,
                              text="fixed reply", synthetic_test=True)
        self.assertEqual(paused.exception.code, "paused")
        self.assertEqual(self.counts["constructed"], 0)
        self.channel.resume(session_id="existing-agent-session")
        self.assertFalse(self.channel.status()["paused"])
        self.channel.pause()
        self.assertEqual(self.channel.status()["mode"], "human")
        self.assertTrue(self.channel.status()["paused"])

    def test_read_is_one_viewport_without_official_ids_or_cursor(self):
        self.init()
        result = self.channel.read(session_id="existing-agent-session")
        self.assertEqual(result["coverage"], "viewport_only")
        self.assertFalse(result["complete_context"])
        self.assertFalse(result["marks_all_read"])
        self.assertFalse(result["message_id_available"])
        self.assertFalse(result["cursor_available"])
        self.assertFalse(result["subscription"])
        self.assertEqual(result["window"], {"pid": 101, "window_id": 202,
                                            "bounds": [10, 20, 900, 800]})
        self.assertEqual(result["observed_at"], self.clock())
        self.assertEqual(result["viewport_hash"], "a" * 64)
        self.assertEqual(result["lines"], [{"text": "synthetic request", "confidence": 0.9,
                                             "box": [0.1, 0.2, 0.3, 0.4],
                                             "direction": "unknown"}])
        self.assertNotIn("messages", result)
        self.assertNotIn("weak-ocr-fingerprint", str(result))

    def test_doctor_low_confidence_does_not_authorize_or_echo_text(self):
        self.init()
        report = self.channel.diagnose_header(session_id="existing-agent-session")
        self.assertTrue(report["title_exact"])
        self.assertEqual(report["matching_title_confidences"], [0.5])
        self.assertFalse(report["automatic_title_gate_passed"])
        self.assertFalse(report["read_authorized"])
        self.assertFalse(report["send_authorized"])
        self.assertFalse(report["identity_verified"])
        self.assertNotIn("recognized_text", report)
        self.assertNotIn("private header", json.dumps(report))
        self.assertFalse(self.counts["activation_requested"])
        self.assertTrue(self.channel.status()["paused"])
        self.assertEqual([self.counts[k] for k in ("read", "page", "dispatch")], [0, 0, 0])
        before = self.counts["constructed"]
        with self.assertRaises(ChannelError):
            self.channel.diagnose_header(session_id="wrong-session", activate_existing=True)
        self.assertEqual(self.counts["constructed"], before)

    def test_doctor_rejects_fabricated_identity_or_body_read(self):
        self.init()
        for claim in ("identity_verified", "body_read"):
            class InvalidDiagnosis(FakeDesktop):
                def diagnose_header(inner, *, activate_existing=False):
                    report = super().diagnose_header(activate_existing=activate_existing)
                    report[claim] = True
                    return report

            channel = Channel(self.state_dir, backend_factory=lambda *args: InvalidDiagnosis(
                args[0], args[1], self.clock, self.counts))
            with self.assertRaises(ChannelError) as error:
                channel.diagnose_header(session_id="existing-agent-session")
            self.assertEqual(error.exception.code, "native_blocked")

    def test_cli_doctor_preserves_blocked_phase_and_nonzero_exit(self):
        self.init()

        class CaptureBlocked(FakeDesktop):
            def diagnose_header(inner, *, activate_existing=False):
                report = super().diagnose_header(activate_existing=activate_existing)
                report.update(status="blocked", phase="capture", capture_healthy=False,
                              title_exact=False, matching_title_confidences=[],
                              reason="precondition_unavailable")
                return report

        output = io.StringIO()
        code = main(["--state-dir", str(self.state_dir), "doctor",
                     "--session-id", "existing-agent-session", "--activate-existing"],
                    backend_factory=lambda *args: CaptureBlocked(args[0], args[1], self.clock, self.counts),
                    clock=self.clock, stdout=output)
        report = json.loads(output.getvalue())
        self.assertEqual(code, 2)
        self.assertFalse(report["ok"])
        self.assertEqual(report["phase"], "capture")
        self.assertFalse(report["body_read"])
        self.assertTrue(self.counts["activation_requested"])
        self.assertEqual([self.counts[k] for k in ("read", "page", "dispatch")], [0, 0, 0])

    def test_page_bounded_and_evidence_required_for_each_viewport(self):
        self.init()
        page = self.channel.page(session_id="existing-agent-session", delta=-2)
        self.assertEqual(page["scroll_delta"], -2)
        self.assertEqual(page["coverage"], "viewport_only")
        self.assertFalse(page["complete_context"])
        self.assertEqual(page["window"]["window_id"], 202)
        self.assertEqual(page["viewport_hash"], "a" * 64)
        self.assertEqual(self.counts["page"], 1)
        with self.assertRaises(ChannelError):
            self.channel.page(session_id="existing-agent-session", delta=0)
        with self.assertRaises(ChannelError):
            self.channel.page(session_id="other-session", delta=1)
        self.assertEqual(self.counts["page"], 1)

        class MissingEvidence(FakeDesktop):
            def page(self, scroll_delta):
                snapshot = super().page(scroll_delta)
                snapshot["viewport_hash"] = "invalid"
                return snapshot

        blocked = Channel(self.state_dir,
                          backend_factory=lambda target, state: MissingEvidence(
                              target, state, self.clock, self.counts), clock=self.clock)
        with self.assertRaises(ChannelError) as error:
            blocked.page(session_id="existing-agent-session", delta=1)
        self.assertEqual(error.exception.code, "native_blocked")

    def test_request_idempotency_same_text_returns_old_state_no_second_dispatch(self):
        self.init()
        self.channel.resume(session_id="existing-agent-session")
        first = self.channel.send(session_id="existing-agent-session", request_id=REQUEST,
                                  text="fixed reply", synthetic_test=True)
        self.assertEqual(first["journal_state"], "submit_attempted")
        again = self.channel.send(session_id="existing-agent-session", request_id=REQUEST,
                                  text="fixed reply", synthetic_test=True)
        self.assertEqual(again["status"], "existing")
        self.assertEqual(again["journal_state"], "submit_attempted")
        self.assertEqual(self.counts["dispatch"], 1)
        with self.assertRaises(ChannelError) as mismatched:
            self.channel.send(session_id="existing-agent-session", request_id=REQUEST,
                              text="different reply", synthetic_test=True)
        self.assertEqual(mismatched.exception.code, "request_refused")
        self.assertEqual(self.counts["dispatch"], 1)

    def test_native_missing_or_fake_success_cannot_report_delivery(self):
        self.init()
        self.channel.resume(session_id="existing-agent-session")

        def missing(target, state_dir):
            raise ImportError("native unavailable")

        blocked = Channel(self.state_dir, backend_factory=missing, clock=self.clock)
        with self.assertRaises(ChannelError) as read_error:
            blocked.read(session_id="existing-agent-session")
        self.assertEqual(read_error.exception.code, "native_blocked")
        with self.assertRaises(ChannelError) as send_error:
            blocked.send(session_id="existing-agent-session", request_id=REQUEST,
                         text="fixed reply", synthetic_test=True)
        self.assertEqual(send_error.exception.code, "native_blocked")

        class FakeSuccess:
            def read(self):
                return {"messages": []}

            def dispatch(self, outbox, session, request_id, text):
                return {"status": "delivered"}

        fake_success = Channel(self.state_dir, backend_factory=lambda *args: FakeSuccess(),
                               clock=self.clock)
        with self.assertRaises(ChannelError) as error:
            fake_success.send(session_id="existing-agent-session", request_id=REQUEST,
                              text="fixed reply", synthetic_test=True)
        self.assertEqual(error.exception.code, "native_blocked")

    def test_typed_attempt_without_click_remains_manual_review_after_restart(self):
        self.init()
        self.channel.resume(session_id="existing-agent-session")

        class UncertainDesktop:
            def dispatch(self, outbox, session, request_id, text):
                return {"status": "indeterminate"}

        uncertain = Channel(self.state_dir, backend_factory=lambda *args: UncertainDesktop(),
                            clock=self.clock)
        first = uncertain.send(session_id="existing-agent-session", request_id=REQUEST,
                               text="fixed reply", synthetic_test=True)
        self.assertEqual(first["status"], "indeterminate")
        self.assertEqual(first["journal_state"], "prepared")
        restarted = Channel(self.state_dir, backend_factory=self.factory, clock=self.clock)
        second = restarted.send(session_id="existing-agent-session", request_id=REQUEST,
                                text="fixed reply", synthetic_test=True)
        self.assertEqual(second["status"], "existing")
        self.assertTrue(second["requires_manual_review"])
        self.assertEqual(self.counts["dispatch"], 0)

    def test_parse_copy_is_unverified_local_input_without_ui_or_cursor(self):
        initialized = self.init()
        files_before = {path.name for path in self.state_dir.iterdir()}
        result = self.channel.parse_copy(session_id="existing-agent-session",
                                         expected_count=2, text=COPIED)
        self.assertEqual(result["channel_id"], initialized["channel_id"])
        self.assertEqual(result["agent_session_id"], "existing-agent-session")
        self.assertEqual([item["text"] for item in result["records"]],
                         ["First synthetic request", "Second synthetic request"])
        self.assertEqual([item["sender_label"] for item in result["records"]],
                         ["Alex", "Blair"])
        self.assertTrue(all(item["direction"] == "unknown" and
                            item["native_message_id_available"] is False and
                            item["provenance"] == "clipboard_text"
                            for item in result["records"]))
        self.assertFalse(result["source_verified"])
        self.assertFalse(result["history_complete"])
        self.assertFalse(result["cursor_available"])
        self.assertFalse(result["event_ids_available"])
        self.assertEqual(result["coverage"], "provided_copy_only")
        self.assertTrue(all("event_id" not in item for item in result["records"]))
        self.assertEqual(self.channel.parse_copy(session_id="existing-agent-session",
                                                  expected_count=2, text=COPIED), result)
        self.assertEqual(files_before, {path.name for path in self.state_dir.iterdir()})
        self.assertEqual(self.counts["constructed"], 0)

    def test_parse_copy_rejects_wrong_session_count_and_missing_body_without_leak(self):
        with self.assertRaises(ChannelError) as missing:
            self.channel.parse_copy(session_id="existing-agent-session", expected_count=2,
                                    text=COPIED)
        self.assertEqual(missing.exception.code, "not_initialized")
        self.init()
        with self.assertRaises(ChannelError) as wrong_session:
            self.channel.parse_copy(session_id="other-session", expected_count=2,
                                    text=COPIED)
        self.assertEqual(wrong_session.exception.code, "session_mismatch")
        with self.assertRaises(ChannelError) as wrong_count:
            self.channel.parse_copy(session_id="existing-agent-session", expected_count=1,
                                    text=COPIED)
        self.assertEqual(wrong_count.exception.code, "count_or_separator_ambiguous")
        with self.assertRaises(ChannelError) as missing_body:
            self.channel.parse_copy(session_id="existing-agent-session", expected_count=1,
                                    text="Alex\n2026年10月07日 12:30\n")
        self.assertEqual(missing_body.exception.code, "missing_body")
        self.assertNotIn("First synthetic request", str(wrong_count.exception))
        self.assertEqual(self.counts["constructed"], 0)

    def test_parse_copy_cli_json_and_size_gate(self):
        self.init()
        args = ["--state-dir", str(self.state_dir), "parse-copy", "--session-id",
                "existing-agent-session", "--expected-count", "2"]
        output = io.StringIO()
        self.assertEqual(main(args, stdin=io.StringIO(COPIED), stdout=output,
                              backend_factory=self.factory, clock=self.clock), 0)
        response = json.loads(output.getvalue())
        self.assertTrue(response["ok"])
        self.assertEqual(len(response["records"]), 2)
        self.assertFalse(response["source_verified"])
        self.assertEqual(self.counts["constructed"], 0)

        output = io.StringIO()
        self.assertEqual(main(args, stdin=io.StringIO(COPIED + "x" * 20001), stdout=output,
                              backend_factory=self.factory, clock=self.clock), 2)
        refused = json.loads(output.getvalue())
        self.assertEqual(refused["code"], "size_limit")
        self.assertNotIn("First synthetic request", output.getvalue())
        self.assertEqual(len(output.getvalue().splitlines()), 1)
        self.assertEqual(self.counts["constructed"], 0)

    def test_cli_json_errors_and_synthetic_send_using_stdin(self):
        output = io.StringIO()
        args = ["--state-dir", str(self.state_dir)]
        self.assertEqual(main(args + ["init", "--group-title", "Synthetic Team",
                                      "--account-binding-id", "local-test-account",
                                      "--session-id", "existing-agent-session"],
                              backend_factory=self.factory, clock=self.clock, stdout=output), 0)
        self.assertTrue(json.loads(output.getvalue())["ok"])
        output = io.StringIO()
        self.assertEqual(main(args + ["send", "--session-id", "existing-agent-session",
                                      "--request-id", REQUEST], stdin=io.StringIO("fixed reply"),
                              backend_factory=self.factory, clock=self.clock, stdout=output), 2)
        self.assertEqual(json.loads(output.getvalue())["code"], "invalid_arguments")
        output = io.StringIO()
        self.assertEqual(main(args + ["resume", "--session-id", "existing-agent-session"],
                              backend_factory=self.factory, clock=self.clock, stdout=output), 0)
        output = io.StringIO()
        self.assertEqual(main(args + ["page", "--session-id", "existing-agent-session",
                                      "--delta", "-1"],
                              backend_factory=self.factory, clock=self.clock, stdout=output), 0)
        self.assertEqual(json.loads(output.getvalue())["scroll_delta"], -1)
        output = io.StringIO()
        self.assertEqual(main(args + ["page", "--session-id", "existing-agent-session",
                                      "--delta", "4"],
                              backend_factory=self.factory, clock=self.clock, stdout=output), 2)
        self.assertEqual(json.loads(output.getvalue())["code"], "invalid_arguments")
        output = io.StringIO()
        self.assertEqual(main(args + ["send", "--session-id", "existing-agent-session",
                                      "--request-id", REQUEST, "--synthetic-test"],
                              stdin=io.StringIO("fixed reply"), backend_factory=self.factory,
                              clock=self.clock,
                              stdout=output), 0)
        result = json.loads(output.getvalue())
        self.assertTrue(result["ok"])
        self.assertEqual(result["journal_state"], "submit_attempted")


if __name__ == "__main__":
    unittest.main()

import hashlib
import os
import stat
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from wechat_desktop_agent import (FullTarget, Observation, Outbox, ReceiptEvidence,
                                  Refused, WindowIdentity)


class Clock:
    def __init__(self):
        self.value = 1000.0

    def __call__(self):
        return self.value

    def advance(self, seconds=0.1):
        self.value += seconds


class Driver:
    def __init__(self, callback=None, fail=False):
        self.calls = []
        self.callback = callback
        self.fail = fail

    def click_send(self, target, window):
        self.calls.append((target, window))
        if self.callback:
            self.callback()
        if self.fail:
            raise RuntimeError("unknown GUI result")


class Verifier:
    def __init__(self, accepted=True):
        self.accepted = accepted
        self.calls = 0

    def verify(self, evidence):
        self.calls += 1
        return self.accepted


class OutboxTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "journal.sqlite3"
        self.clock = Clock()
        self.box = Outbox(self.path, clock=self.clock)
        self.target = FullTarget("generic.desktop", "synthetic-account", "synthetic-team",
                                 "Synthetic Team")
        self.window = WindowIdentity(20, 31, (10, 20, 900, 800))
        self.text = "OUT-01 fixed synthetic reply"

    def automatic(self):
        self.box.authorize_automatic(self.target.account_id, human_approved=True)
        return self.box.acquire(self.target.account_id, "writer-A")

    def observation(self, text=None, **overrides):
        self.clock.advance()
        values = {"target": self.target, "window": self.window,
                  "capture_healthy": True, "observed_at": self.clock(),
                  "draft_text": text}
        values.update(overrides)
        return Observation(**values)

    def prepared(self, session):
        self.assertEqual(self.box.prepare(session, intent_id="intent-01",
            source_message_id="message-01", identity_kind="synthetic_fixture",
            target=self.target, marker="OUT-01", text=self.text), "prepared")
        with self.assertRaises(Refused):
            self.box.prepare(session, intent_id="intent-01", source_message_id="message-01",
                             identity_kind="explicit_stable", target=self.target,
                             marker="OUT-01", text=self.text)

    def draft_verified(self, session):
        self.prepared(session)
        self.box.verify_draft(session, "intent-01", self.text, self.observation(self.text))
        self.assertEqual(self.box.state(self.target.account_id, "intent-01"), "draft_verified")

    def test_initial_human_mode_refuses_automatic_lease(self):
        with self.assertRaisesRegex(Refused, "human takeover"):
            self.box.acquire(self.target.account_id, "writer-A")
        with self.assertRaisesRegex(Refused, "human authorization"):
            self.box.authorize_automatic(self.target.account_id, human_approved=False)

    def test_control_characters_in_identifiers_or_target_refuse(self):
        for account in ("synthetic\x00account", "synthetic\raccount", "synthetic\taccount"):
            with self.subTest(account=repr(account)), self.assertRaises(Refused):
                self.box.handoff_to_human(account)
        session = self.automatic()
        bad_target = FullTarget("generic.desktop", self.target.account_id, "synthetic-team",
                                "Synthetic\tTeam")
        with self.assertRaises(Refused):
            self.box.prepare(session, intent_id="intent-01", source_message_id="message-01",
                             identity_kind="synthetic_fixture", target=bad_target,
                             marker="OUT-01", text=self.text)

    def test_database_is_private_and_unsafe_existing_file_is_not_changed(self):
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        unsafe = Path(self.temp.name) / "unsafe.sqlite3"
        unsafe.write_bytes(b"")
        os.chmod(unsafe, 0o644)
        with self.assertRaisesRegex(Refused, "private SQLite"):
            Outbox(unsafe, clock=self.clock)
        self.assertEqual(stat.S_IMODE(unsafe.stat().st_mode), 0o644)
        self.assertEqual(unsafe.read_bytes(), b"")

    def test_submit_is_durable_before_click_and_restart_never_reclicks(self):
        session = self.automatic()
        self.draft_verified(session)
        driver = Driver(callback=lambda: self.assertEqual(
            Outbox(self.path, clock=self.clock).state(self.target.account_id, "intent-01"),
            "submit_attempted"))
        self.assertEqual(self.box.submit_once(session, "intent-01", self.text,
                         self.observation(self.text), driver), "submit_attempted")
        restarted = Outbox(self.path, clock=self.clock)
        with self.assertRaisesRegex(Refused, "already attempted"):
            restarted.submit_once(session, "intent-01", self.text,
                                  self.observation(self.text), driver)
        self.assertEqual(len(driver.calls), 1)

    def test_unknown_click_result_is_indeterminate_and_never_retried(self):
        session = self.automatic()
        self.draft_verified(session)
        driver = Driver(fail=True)
        self.assertEqual(self.box.submit_once(session, "intent-01", self.text,
                         self.observation(self.text), driver), "indeterminate")
        with self.assertRaises(Refused):
            Outbox(self.path, clock=self.clock).submit_once(
                session, "intent-01", self.text, self.observation(self.text), driver)
        self.assertEqual(len(driver.calls), 1)

    def test_target_window_capture_and_freshness_mismatch_block_side_effect(self):
        session = self.automatic()
        self.draft_verified(session)
        driver = Driver()
        wrong_target = FullTarget("generic.desktop", "synthetic-account", "other-team", "Other Team")
        wrong_window = WindowIdentity(20, 32, (10, 20, 900, 800))
        for changes in ({"target": wrong_target}, {"window": wrong_window},
                        {"capture_healthy": False}, {"draft_text": "wrong"}):
            with self.subTest(changes=changes), self.assertRaises(Refused):
                self.box.submit_once(session, "intent-01", self.text,
                                     self.observation(self.text, **changes), driver)
        old = self.observation(self.text)
        self.clock.advance(3.0)
        with self.assertRaises(Refused):
            self.box.submit_once(session, "intent-01", self.text, old, driver)
        self.assertEqual(driver.calls, [])
        self.assertEqual(self.box.state(self.target.account_id, "intent-01"), "draft_verified")

    def test_explicit_identity_not_content_fingerprint_controls_dedup(self):
        session = self.automatic()
        self.prepared(session)
        self.assertEqual(self.box.prepare(session, intent_id="intent-01",
            source_message_id="message-01", identity_kind="synthetic_fixture",
            target=self.target, marker="OUT-01", text=self.text), "prepared")
        self.assertEqual(self.box.prepare(session, intent_id="intent-02",
            source_message_id="message-02", identity_kind="synthetic_fixture",
            target=self.target, marker="OUT-01", text=self.text), "prepared")
        with self.assertRaises(Refused):
            self.box.prepare(session, intent_id="intent-03", source_message_id="message-01",
                             identity_kind="synthetic_fixture", target=self.target,
                             marker="OUT-01", text=self.text)
        with self.assertRaisesRegex(Refused, "explicit stable"):
            self.box.prepare(session, intent_id="intent-03", source_message_id="ocr:abc",
                             identity_kind="ocr_fingerprint", target=self.target,
                             marker="OUT-01", text=self.text)
        self.assertNotIn(self.text.encode(), self.path.read_bytes())

    def test_agent_request_identity_is_separate_from_weak_chat_ocr_id(self):
        session = self.automatic()
        request = "5d814faa-e0fe-4fe6-a6bc-e882b527da48"
        kwargs = dict(intent_id=request, source_message_id="agent-request:" + request,
                      identity_kind="agent_request", target=self.target,
                      marker=request, text=request + " Plain fixed synthetic reply",
                      tag_existing=True)
        self.assertEqual(self.box.prepare(session, **kwargs), "prepared")
        self.assertEqual(self.box.prepare(session, **kwargs), "existing:prepared")
        with self.assertRaises(Refused):
            self.box.prepare(session, **{**kwargs, "text": request + " Different content"})

    def test_human_handoff_invalidates_lease_and_old_window_observation(self):
        session = self.automatic()
        self.draft_verified(session)
        old = self.observation(self.text)
        self.box.handoff_to_human(self.target.account_id)
        with self.assertRaises(Refused):
            self.box.submit_once(session, "intent-01", self.text, old, Driver())
        self.clock.advance()
        self.box.authorize_automatic(self.target.account_id, human_approved=True)
        renewed = self.box.acquire(self.target.account_id, "writer-A")
        with self.assertRaises(Refused):
            self.box.submit_once(renewed, "intent-01", self.text, old, Driver())
        self.box.verify_draft(renewed, "intent-01", self.text, self.observation(self.text))
        self.assertEqual(self.box.submit_once(renewed, "intent-01", self.text,
                         self.observation(self.text), Driver()), "submit_attempted")

    def test_concurrent_accounts_lease_race_has_single_owner(self):
        self.box.authorize_automatic(self.target.account_id, human_approved=True)
        barrier = threading.Barrier(2)

        def acquire(owner):
            box = Outbox(self.path, clock=self.clock)
            barrier.wait()
            try:
                return box.acquire(self.target.account_id, owner)
            except Refused:
                return None

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(acquire, ("writer-A", "writer-B")))
        self.assertEqual(sum(result is not None for result in results), 1)

    def test_takeover_waits_while_one_click_is_in_flight_even_if_lease_expires(self):
        session = self.automatic()
        self.draft_verified(session)
        entered = threading.Event()
        release = threading.Event()
        takeover_started = threading.Event()
        takeover_finished = threading.Event()

        def on_click():
            self.clock.advance(11)
            entered.set()
            self.assertTrue(release.wait(2))

        def do_submit():
            return self.box.submit_once(session, "intent-01", self.text,
                                        self.observation(self.text), Driver(callback=on_click))

        def do_takeover():
            takeover_started.set()
            result = Outbox(self.path, clock=self.clock).acquire(self.target.account_id, "writer-B")
            takeover_finished.set()
            return result

        with ThreadPoolExecutor(max_workers=2) as pool:
            submitting = pool.submit(do_submit)
            self.assertTrue(entered.wait(2))
            taking_over = pool.submit(do_takeover)
            self.assertTrue(takeover_started.wait(2))
            self.assertFalse(takeover_finished.wait(0.05))
            release.set()
            self.assertEqual(submitting.result(2), "submit_attempted")
            self.assertEqual(taking_over.result(2).owner_id, "writer-B")

    def test_expired_lease_bumps_epoch_and_old_owner_cannot_submit(self):
        session = self.automatic()
        self.draft_verified(session)
        self.clock.advance(11)
        takeover = self.box.acquire(self.target.account_id, "writer-B")
        self.assertGreater(takeover.epoch, session.epoch)
        with self.assertRaises(Refused):
            self.box.submit_once(session, "intent-01", self.text,
                                 self.observation(self.text), Driver())
        self.box.verify_draft(takeover, "intent-01", self.text, self.observation(self.text))

    def test_red_error_is_failed_not_delivered_or_retryable(self):
        session = self.automatic()
        self.draft_verified(session)
        self.box.submit_once(session, "intent-01", self.text,
                             self.observation(self.text), Driver())
        with self.assertRaisesRegex(Refused, "failed UI marker not exact"):
            self.box.record_ui_outcome(self.target.account_id, "intent-01",
                                       self.observation(failure_marker="OUT-OTHER"), "failed")
        self.assertEqual(self.box.record_ui_outcome(self.target.account_id, "intent-01",
                         self.observation(failure_marker="OUT-01"), "failed"), "failed")
        with self.assertRaises(Refused):
            self.box.submit_once(session, "intent-01", self.text,
                                 self.observation(self.text), Driver())
        self.assertEqual(self.box.state(self.target.account_id, "intent-01"), "failed")

    def test_local_ui_cannot_fake_recipient_receipt(self):
        session = self.automatic()
        self.draft_verified(session)
        self.box.submit_once(session, "intent-01", self.text,
                             self.observation(self.text), Driver())
        with self.assertRaisesRegex(Refused, "outgoing UI text not exact"):
            self.box.record_ui_outcome(self.target.account_id, "intent-01",
                                       self.observation(visible_outgoing_text="other"), "ui_observed")
        self.box.record_ui_outcome(self.target.account_id, "intent-01",
                                   self.observation(visible_outgoing_text=self.text), "ui_observed")
        verifier = Verifier()
        digest = hashlib.sha256(self.text.encode()).hexdigest()
        good = ReceiptEvidence(self.target, "intent-01", digest, "other-synthetic-account",
                               "independent-proof-01", self.clock(), "independent_recipient")
        for altered in (ReceiptEvidence(self.target, "intent-01", digest,
                                        self.target.account_id, "proof-same-sender", self.clock()),
                        ReceiptEvidence(self.target, "intent-01", digest, "other-account",
                                        "proof-local-ocr", self.clock(), "local_ui_ocr"),
                        ReceiptEvidence(self.target, "intent-01", "0" * 64, "other-account",
                                        "proof-wrong-digest", self.clock())):
            with self.assertRaises(Refused):
                self.box.confirm_recipient(self.target.account_id, "intent-01", altered, verifier)
        with self.assertRaises(Refused):
            self.box.confirm_recipient(self.target.account_id, "intent-01", good, Verifier(False))
        self.assertEqual(self.box.state(self.target.account_id, "intent-01"), "ui_observed")
        self.box.confirm_recipient(self.target.account_id, "intent-01", good, verifier)
        self.assertEqual(self.box.state(self.target.account_id, "intent-01"), "recipient_confirmed")


if __name__ == "__main__":
    unittest.main()

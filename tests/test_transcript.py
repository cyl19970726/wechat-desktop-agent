import os
import stat
import tempfile
import unittest
from pathlib import Path

from wechat_desktop_agent.transcript import (
    Bubble, TranscriptError, TranscriptStore, append_after_checkpoint, stitch_pages,
)


def b(text, **kw):
    return Bubble(text, "incoming", **kw)


class StitchTests(unittest.TestCase):
    def test_four_pages_preserve_order_and_distinct_repeated_text(self):
        whole = [b(x) for x in ("start", "yes", "alpha", "next", "yes", "bravo",
                                 "later", "yes", "charlie", "last", "yes", "end")]
        pages = (whole[:5], whole[2:8], whole[5:11], whole[8:])
        result = stitch_pages(pages[0], pages[1])
        self.assertEqual((result.coverage, result.overlap), ("from_confirmed_start", 3))
        for page in pages[2:]:
            result = stitch_pages(result.bubbles, page)
            self.assertEqual(result.coverage, "from_confirmed_start")
        self.assertEqual(list(result.bubbles), whole)
        self.assertEqual(sum(x.text == "yes" for x in result.bubbles), 4)
        self.assertEqual((result.older_start, result.newer_start), (8, 0))

    def test_rejects_short_ambiguous_and_missing_anchors(self):
        old = [b(x) for x in ("a", "b", "c", "d")]
        self.assertEqual(stitch_pages(old, [b("c"), b("d"), b("e")]).reason,
                         "anchor_missing")
        self.assertEqual(stitch_pages([b(x) for x in ("a", "b", "a", "b", "a")],
                                      [b(x) for x in ("a", "b", "a", "b", "a", "z")]).reason,
                         "ambiguous_anchor")
        self.assertEqual(stitch_pages(old, [b("x"), b("y"), b("z")]).reason,
                         "anchor_missing")
        self.assertEqual(append_after_checkpoint(old, [b(x) for x in ("b", "c", "C", "e")]).reason,
                         "anchor_missing")

    def test_unsafe_and_identity_conflicts(self):
        base = [b("a"), b("b"), b("c")]
        for changed in (Bubble("b", "unknown"), b("b", confidence=.5),
                        b("b", edge_truncated=True), b("b", unsupported=True)):
            with self.subTest(changed=changed):
                self.assertEqual(stitch_pages(base, [b("a"), changed, b("c")]).reason,
                                 "unsafe_candidate")
        for changed in (Bubble("b", "outgoing"), b("b", sender="other"),
                        b("b", time_candidate="11:00")):
            old = [b("a"), b("b", sender="sender", time_candidate="10:00"), b("c")]
            self.assertEqual(stitch_pages(old, [b("a"), changed, b("c")]).reason,
                             "anchor_missing")
        self.assertEqual(stitch_pages([b("a\r\nb"), b("c"), b("d")],
                                      [b("a\nb"), b("c"), b("d")]).coverage,
                         "from_confirmed_start")


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "transcript.sqlite3"
        self.baseline = [b("one"), b("two"), b("three"), b("four")]

    def store(self, **kw):
        return TranscriptStore(self.path, group_binding="synthetic-group",
                               agent_session_id="synthetic-session", **kw)

    def test_confirmation_append_replay_restart_and_failure_does_not_advance(self):
        store = self.store()
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        with self.assertRaisesRegex(TranscriptError, "human_confirmation_required"):
            store.confirm_baseline(self.baseline, user_confirmed=False)
        self.assertEqual(store.cursor, 0)
        self.assertEqual(store.confirm_baseline(self.baseline, user_confirmed=True), 4)
        first = store.read_since(0)
        self.assertEqual(first, store.read_since(0))
        for page, reason in (([b("three"), b("four")], "anchor_missing"),
                             ([b("two"), b("three"), b("THREE")], "anchor_missing"),
                             ([b("two"), b("three"), b("four", edge_truncated=True)], "unsafe_candidate")):
            self.assertEqual(store.append_page(page, checkpoint_cursor=4).reason, reason)
            self.assertEqual(store.cursor, 4)
        self.assertEqual(store.append_page([b("two"), b("three"), b("four"), b("five")],
                                           checkpoint_cursor=3).reason, "checkpoint_mismatch")
        self.assertEqual(store.append_page([b("two"), b("three"), b("four"), b("five")],
                                           checkpoint_cursor=4).coverage, "from_confirmed_start")
        self.assertEqual(store.cursor, 5)
        self.assertEqual(store.read_since(4)[0].bubble.text, "five")
        previous = store.read_since(0)
        store.close()
        restarted = self.store()
        self.assertEqual(restarted.cursor, 5)
        self.assertEqual(restarted.read_since(0), previous)
        self.assertEqual(restarted.read_since(5), ())
        restarted.close()

    def test_bound_and_binding_conflict(self):
        store = self.store(max_events=4)
        store.confirm_baseline(self.baseline, user_confirmed=True)
        with self.assertRaisesRegex(TranscriptError, "retention_limit"):
            store.append_page([b("two"), b("three"), b("four"), b("five")],
                              checkpoint_cursor=4)
        self.assertEqual(store.cursor, 4)
        store.close()
        with self.assertRaisesRegex(TranscriptError, "binding_conflict"):
            TranscriptStore(self.path, group_binding="other", agent_session_id="synthetic-session")

    def test_unsafe_existing_permissions_and_symlink_refused(self):
        bad = Path(self.temp.name) / "bad.sqlite3"
        bad.write_bytes(b"")
        os.chmod(bad, 0o644)
        with self.assertRaisesRegex(TranscriptError, "unsafe_database"):
            TranscriptStore(bad, group_binding="g", agent_session_id="s")
        link = Path(self.temp.name) / "link.sqlite3"
        link.symlink_to(bad)
        with self.assertRaisesRegex(TranscriptError, "unsafe_database"):
            TranscriptStore(link, group_binding="g", agent_session_id="s")


if __name__ == "__main__":
    unittest.main()

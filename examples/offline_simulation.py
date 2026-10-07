"""Fixed-literal, offline state demo. No desktop app, network, or AI model."""

from __future__ import annotations

import tempfile
from pathlib import Path

from wechat_desktop_agent import FullTarget, Observation, Outbox, WindowIdentity


class Clock:
    value = 1000.0

    def __call__(self):
        return self.value

    def advance(self):
        self.value += 0.1
        return self.value


class FakeDesktop:
    def click_send(self, target, window):
        print("synthetic click invoked; no GUI action")


def main():
    clock = Clock()
    with tempfile.TemporaryDirectory() as folder:
        box = Outbox(Path(folder) / "outbox.sqlite3", clock=clock)
        target = FullTarget("synthetic.desktop", "test-account", "test-team", "Test Team")
        window = WindowIdentity(100, 200, (10, 20, 900, 800))
        text = "OUT-001 Fixed synthetic reply"
        box.authorize_automatic(target.account_id, human_approved=True)
        session = box.acquire(target.account_id, "offline-simulator")
        box.prepare(session, intent_id="intent-001", source_message_id="message-001",
                    identity_kind="synthetic_fixture", target=target,
                    marker="OUT-001", text=text)
        box.verify_draft(session, "intent-001", text,
                         Observation(target, window, True, clock.advance(), draft_text=text))
        result = box.submit_once(session, "intent-001", text,
                                 Observation(target, window, True, clock.advance(), draft_text=text),
                                 FakeDesktop())
        print("journal state:", result)
        print("recipient delivery remains unverified")


if __name__ == "__main__":
    main()

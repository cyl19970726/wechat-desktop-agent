"""Local single-group channel CLI for an existing agent; no model service.

Read is one bounded viewport snapshot, not a subscription or complete history.
Send is enabled only for explicitly numbered synthetic tests. The native Mac
backend must supply real window/capture/draft evidence before any UI action.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from .channel import Channel, ChannelError
from .clipboard_records import MAX_CHARS


class JsonParser(argparse.ArgumentParser):
    def error(self, message):
        raise ChannelError("invalid_arguments", "invalid command arguments")


def _parser() -> argparse.ArgumentParser:
    parser = JsonParser(prog="wechat-desktop-agent")
    parser.add_argument("--state-dir", type=Path,
                        help="private local state directory (default: cwd/.local/wechat-desktop-agent)")
    commands = parser.add_subparsers(dest="command", required=True, parser_class=JsonParser)
    initialize = commands.add_parser("init")
    initialize.add_argument("--group-title", required=True)
    initialize.add_argument("--account-binding-id", required=True)
    initialize.add_argument("--session-id", required=True)
    commands.add_parser("status")
    doctor = commands.add_parser("doctor")
    doctor.add_argument("--session-id", required=True)
    doctor.add_argument("--activate-existing", action="store_true")
    read = commands.add_parser("read")
    read.add_argument("--session-id", required=True)
    page = commands.add_parser("page")
    page.add_argument("--session-id", required=True)
    page.add_argument("--delta", required=True, type=int, choices=(-3, -2, -1, 1, 2, 3))
    copied = commands.add_parser("parse-copy")
    copied.add_argument("--session-id", required=True)
    copied.add_argument("--expected-count", required=True, type=int)
    send = commands.add_parser("send")
    send.add_argument("--session-id", required=True)
    send.add_argument("--request-id", required=True)
    send.add_argument("--synthetic-test", action="store_true", required=True)
    commands.add_parser("pause")
    resume = commands.add_parser("resume")
    resume.add_argument("--session-id", required=True)
    return parser


def main(argv: list[str] | None = None, *, backend_factory=None,
         clock=time.time, stdin=None, stdout=None) -> int:
    stdin = stdin if stdin is not None else sys.stdin
    stdout = stdout if stdout is not None else sys.stdout
    try:
        args = _parser().parse_args(argv)
        channel = Channel(args.state_dir, backend_factory=backend_factory, clock=clock)
        if args.command == "init":
            result = channel.init(group_title=args.group_title,
                                  account_binding_id=args.account_binding_id,
                                  agent_session_id=args.session_id)
        elif args.command == "status":
            result = channel.status()
        elif args.command == "doctor":
            result = channel.diagnose_header(session_id=args.session_id,
                                             activate_existing=args.activate_existing)
        elif args.command == "read":
            result = channel.read(session_id=args.session_id)
        elif args.command == "page":
            result = channel.page(session_id=args.session_id, delta=args.delta)
        elif args.command == "parse-copy":
            copied_text = stdin.read(MAX_CHARS + 1)
            if len(copied_text) > MAX_CHARS:
                raise ChannelError("size_limit", "supplied copied text rejected")
            result = channel.parse_copy(session_id=args.session_id,
                                        expected_count=args.expected_count,
                                        text=copied_text)
        elif args.command == "pause":
            result = channel.pause()
        elif args.command == "resume":
            result = channel.resume(session_id=args.session_id)
        else:
            text = stdin.read(1001)
            if not text or len(text) > 1000:
                raise ChannelError("invalid_text", "synthetic reply must be 1–1000 characters")
            result = channel.send(session_id=args.session_id, request_id=args.request_id,
                                  text=text, synthetic_test=args.synthetic_test)
        blocked_diagnostic = args.command == "doctor" and result["status"] == "blocked"
        payload = {"ok": not blocked_diagnostic, **result}
        if blocked_diagnostic:
            payload["code"] = "native_blocked"
        stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
        return 2 if blocked_diagnostic else 0
    except ChannelError as error:
        stdout.write(json.dumps({"ok": False, "code": error.code,
                                 "message": str(error)}, ensure_ascii=False) + "\n")
        return 2
    except (OSError, ValueError, TypeError):
        stdout.write(json.dumps({"ok": False, "code": "local_error",
                                 "message": "local channel operation unavailable"}) + "\n")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

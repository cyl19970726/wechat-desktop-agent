"""Bounded macOS desktop channel for one explicitly configured conversation.

Imports of macOS frameworks are delayed until a real platform is constructed.
The reader returns observations, never WeChat message IDs or inferred senders.
"""

from __future__ import annotations

import fcntl
import hashlib
import io
import json
import math
import os
import re
import stat
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from .outbox import FullTarget, Observation, Outbox, Refused, Session, WindowIdentity


class NativeBlocked(Refused):
    """A desktop fact could not be established; no further UI action is safe."""


@dataclass(frozen=True)
class Crop:
    x: int
    y: int
    width: int
    height: int


@dataclass(frozen=True)
class Layout:
    kind: str
    width: int
    height: int
    header: Crop
    body: Crop
    input: Crop
    button: Crop
    input_ax_id: str
    button_ax_id: str

    @classmethod
    def load(cls, state_dir: Path) -> "Layout":
        path = state_dir / "layout.json"
        if path.is_symlink() or not path.is_file():
            raise NativeBlocked("approved local layout unavailable")
        metadata = path.stat(follow_symlinks=False)
        if not stat.S_ISREG(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) & 0o077:
            raise NativeBlocked("private layout permissions required")
        try:
            config = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise NativeBlocked("approved local layout unreadable") from None
        if not isinstance(config, dict) or set(config) != {"kind", "window_size", "regions", "ax"}:
            raise NativeBlocked("layout schema invalid")
        kind, size, regions, ax = (config[key] for key in ("kind", "window_size", "regions", "ax"))
        if (kind not in ("main", "chat_only") or not isinstance(size, list) or len(size) != 2
                or any(type(v) is not int or v < 300 or v > 3000 for v in size)
                or not isinstance(regions, dict) or set(regions) != {"header", "body", "input", "button"}
                or not isinstance(ax, dict) or set(ax) != {"input", "send_button"}
                or any(not isinstance(v, str) or not v or len(v) > 100 for v in ax.values())):
            raise NativeBlocked("layout schema invalid")
        crops: dict[str, Crop] = {}
        for name, value in regions.items():
            if (not isinstance(value, list) or len(value) != 4
                    or any(type(v) is not int for v in value)):
                raise NativeBlocked("layout crop invalid")
            x, y, width, height = value
            if x < 0 or y < 0 or width < 20 or height < 20 or x + width > size[0] or y + height > size[1]:
                raise NativeBlocked("layout crop outside window")
            if kind == "main" and x < size[0] * 0.30:
                raise NativeBlocked("main window crop may include sidebar")
            crops[name] = Crop(*value)
        if (crops["header"].y + crops["header"].height >= crops["body"].y
                or crops["body"].y + crops["body"].height >= crops["input"].y
                or crops["input"].y + crops["input"].height > crops["button"].y + crops["button"].height):
            raise NativeBlocked("layout regions overlap")
        return cls(kind, *size, crops["header"], crops["body"], crops["input"], crops["button"],
                   ax["input"], ax["send_button"])


@dataclass(frozen=True)
class OCRLine:
    text: str
    confidence: float
    box: tuple[float, float, float, float]


class DesktopPlatform(Protocol):
    def session_guard(self, purpose: str) -> str: ...
    def activate_existing(self) -> bool: ...
    def window(self, layout: Layout) -> WindowIdentity: ...
    def capture(self, window: WindowIdentity, crop: Crop) -> Any: ...
    def ocr(self, image: Any) -> list[OCRLine]: ...
    def ax_draft(self, window: WindowIdentity, identifier: str) -> str: ...
    def ax_input_focused(self, window: WindowIdentity, identifier: str) -> bool: ...
    def ax_send_enabled(self, window: WindowIdentity, identifier: str) -> bool: ...
    def click(self, x: float, y: float) -> None: ...
    def type_unicode(self, text: str) -> None: ...
    def settle(self, seconds: float) -> None: ...
    def scroll(self, window: WindowIdentity, crop: Crop, delta: int) -> None: ...


def _image_healthy(image: Any) -> None:
    if image is None or getattr(image, "mode", None) != "RGB":
        raise NativeBlocked("capture unavailable")
    extrema = image.getextrema()
    if len(extrema) != 3 or all(high <= 2 for _, high in extrema):
        raise NativeBlocked("capture blank or black")


def _exact_title(lines: list[OCRLine], title: str) -> bool:
    normalized = lambda value: re.sub(r"\s+", "", value).replace("（", "(").replace("）", ")")
    return sum(normalized(line.text) == normalized(title) and line.confidence >= 0.70 for line in lines) == 1


def _exact_draft(lines: list[OCRLine], text: str) -> bool:
    # OCR may split a single input field into lines. Preserve actual spaces.
    return bool(lines) and all(line.confidence >= 0.70 for line in lines) and "".join(line.text for line in lines) == text


def _button_point(image: Any, lines: list[OCRLine], crop: Crop, *, require_green: bool) -> tuple[float, float]:
    matches = [line for line in lines if re.sub(r"\s+", "", line.text) == "发送" and line.confidence >= 0.70]
    if len(matches) != 1:
        raise NativeBlocked("send button label unavailable")
    line = matches[0]
    x, y, width, height = line.box
    cx, cy = x + width / 2, y + height / 2
    if not (0.1 < cx < 0.9 and 0.1 < cy < 0.9 and 0 < width < 0.9 and 0 < height < 0.9):
        raise NativeBlocked("send button geometry unavailable")
    if require_green:
        image_width, image_height = image.size
        pixels = image.crop((max(0, int((x - 0.03) * image_width)),
                             max(0, int((1 - y - height - 0.03) * image_height)),
                             min(image_width, int((x + width + 0.03) * image_width)),
                             min(image_height, int((1 - y + 0.03) * image_height))))
        colors = list(pixels.getdata())
        if not colors or sum(g > 85 and g >= r + 20 and g >= b + 8 for r, g, b in colors) < len(colors) * 0.05:
            raise NativeBlocked("green send control unavailable")
    return crop.x + cx * crop.width, crop.y + (1 - cy) * crop.height


def _red_failure_icon(image: Any, line: OCRLine) -> bool:
    """Recognize only a dense red warning immediately beside the OCR line."""
    width, height = image.size
    left = int(line.box[0] * width)
    middle_y = int((1 - line.box[1] - line.box[3] / 2) * height)
    x0, x1 = max(0, left - 75), max(0, left - 4)
    y0, y1 = max(0, middle_y - 24), min(height, middle_y + 24)
    if x0 >= x1 or y0 >= y1:
        return False
    count = 0
    for y in range(y0, y1):
        for x in range(x0, x1):
            red, green, blue = image.getpixel((x, y))
            if red > 150 and red >= green + 60 and red >= blue + 60:
                count += 1
    return count >= 20


def _single_containing_display(displays: Any, rectangle: tuple[float, float, float, float]):
    x, y, width, height = rectangle
    if width <= 0 or height <= 0:
        raise NativeBlocked("capture region invalid")
    matches = []
    for display in displays:
        frame = display.frame()
        if (frame.origin.x <= x and frame.origin.y <= y
                and x + width <= frame.origin.x + frame.size.width
                and y + height <= frame.origin.y + frame.size.height):
            matches.append(display)
    if len(matches) != 1:
        raise NativeBlocked("capture must fit one display")
    return matches[0]


def _window_local_pixel_crop(window: WindowIdentity, crop: Crop, image_size: tuple[int, int],
                             expected_scale: float) -> Crop:
    """Map an approved window-local point rectangle into a single-window CGImage."""
    _, _, width, height = window.bounds
    image_width, image_height = image_size
    if (width <= 0 or height <= 0 or crop.width <= 0 or crop.height <= 0
            or crop.x < 0 or crop.y < 0 or crop.x + crop.width > width
            or crop.y + crop.height > height or type(image_width) is not int
            or type(image_height) is not int or image_width <= 0 or image_height <= 0
            or not math.isfinite(expected_scale) or expected_scale not in (1.0, 2.0)):
        raise NativeBlocked("single-window crop geometry invalid")
    # A shadow, changed frame, or a differently scaled source would shift the
    # approved region toward other content, so do not infer a new origin.
    if image_width != width * expected_scale or image_height != height * expected_scale:
        raise NativeBlocked("single-window image dimensions changed")
    scale = int(expected_scale)
    return Crop(crop.x * scale, crop.y * scale, crop.width * scale, crop.height * scale)


class NativeChannelBackend:
    """Single-conversation desktop reader and one-attempt sender.

    A caller must first prepare the same request in Outbox using marker=request_id
    and text beginning with ``request_id + ' '``. OCR lines are not events.
    """

    def __init__(self, target: FullTarget, state_dir: Path, *, platform: DesktopPlatform | None = None):
        if not isinstance(target, FullTarget) or target.application != "com.tencent.xinWeChat":
            raise NativeBlocked("unsupported application target")
        self.target = target
        self.state_dir = Path(state_dir)
        if self.state_dir.is_symlink() or not self.state_dir.is_dir():
            raise NativeBlocked("private state directory unavailable")
        if stat.S_IMODE(self.state_dir.stat().st_mode) & 0o077:
            raise NativeBlocked("private state directory permissions required")
        self.platform = platform if platform is not None else MacDesktopPlatform()

    def _layout(self) -> Layout:
        layout = Layout.load(self.state_dir)
        binder = getattr(self.platform, "bind_layout", None)
        if binder is not None:
            binder(layout)
        return layout

    def _binding(self, *, sending: bool) -> dict[str, Any]:
        path = self.state_dir / "channel.json"
        if path.is_symlink() or not path.is_file():
            raise NativeBlocked("local channel binding unavailable")
        try:
            binding = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise NativeBlocked("local channel binding unreadable") from None
        if (not isinstance(binding, dict) or binding.get("version") != 1
                or binding.get("channel_id") != self.target.conversation_id
                or binding.get("account_binding_id") != self.target.account_id
                or binding.get("group_title") != self.target.displayed_title
                or not isinstance(binding.get("agent_session_id"), str)
                or type(binding.get("paused")) is not bool):
            raise NativeBlocked("local channel binding changed")
        metadata = path.stat(follow_symlinks=False)
        if not stat.S_ISREG(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) & 0o077:
            raise NativeBlocked("private channel binding permissions required")
        if sending and binding["paused"]:
            raise NativeBlocked("human takeover active")
        return binding

    @contextmanager
    def _desktop_lock(self):
        path = self.state_dir / "desktop.lock"
        fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            metadata = os.fstat(fd)
            if not stat.S_ISREG(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) & 0o077:
                raise NativeBlocked("private desktop lock required")
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def _same(self, layout: Layout, expected: WindowIdentity) -> None:
        if self.platform.window(layout) != expected:
            raise NativeBlocked("foreground window changed")

    def _lines(self, window: WindowIdentity, crop: Crop) -> list[OCRLine]:
        image = self.platform.capture(window, crop)
        _image_healthy(image)
        lines = self.platform.ocr(image)
        if not isinstance(lines, list) or any(not isinstance(line, OCRLine) for line in lines):
            raise NativeBlocked("OCR unavailable")
        return lines

    def _title(self, layout: Layout, window: WindowIdentity) -> None:
        self._same(layout, window)
        if not _exact_title(self._lines(window, layout.header), self.target.displayed_title):
            raise NativeBlocked("exact conversation title unavailable")
        self._same(layout, window)

    def _read_locked(self, layout: Layout, window: WindowIdentity) -> dict[str, Any]:
        lock_evidence = self.platform.session_guard("read")
        self._title(layout, window)
        self._title(layout, window)
        image = self.platform.capture(window, layout.body)
        _image_healthy(image)
        lines = self.platform.ocr(image)
        if not isinstance(lines, list) or any(not isinstance(line, OCRLine) for line in lines):
            raise NativeBlocked("OCR unavailable")
        self._title(layout, window)
        after_lock_evidence = self.platform.session_guard("read")
        if after_lock_evidence != "screen_unlocked_marker_false":
            lock_evidence = "active_session_lock_marker_unavailable"
        return {
            "channel_id": self.target.conversation_id,
            "session_id": self.target.conversation_id,
            "window": {"pid": window.pid, "window_id": window.window_id, "bounds": list(window.bounds)},
            "title_verified": True,
            "target_verified": True,
            "capture_healthy": True,
            "local_binding_verified": True,
            "account_binding_verified": False,
            "actual_wechat_account_verified": False,
            "screen_lock_evidence": lock_evidence,
            "coverage": "viewport_only",
            "viewport_hash": hashlib.sha256(image.tobytes()).hexdigest(),
            "message_id_available": False,
            "deduplication": "unavailable",
            "direction": "unknown",
            "observed_at": time.time(),
            "lines": [{"text": line.text, "confidence": line.confidence, "box": list(line.box),
                       "direction": "unknown"} for line in lines],
        }

    def read(self, *, activate_existing: bool = False) -> dict[str, Any]:
        with self._desktop_lock():
            binding = self._binding(sending=False)
            self.platform.session_guard("read")
            activated = self.platform.activate_existing() if activate_existing else False
            layout = self._layout()
            result = self._read_locked(layout, self.platform.window(layout))
            result["session_id"] = binding["agent_session_id"]
            result["activation"] = "existing_app_activated" if activated else "already_frontmost_or_not_requested"
            self._binding(sending=False)
            return result

    def page(self, scroll_delta: int, *, activate_existing: bool = False) -> dict[str, Any]:
        """Inspect one adjacent viewport of this same focused conversation only."""
        if type(scroll_delta) is not int or scroll_delta == 0 or abs(scroll_delta) > 3:
            raise NativeBlocked("bounded scroll delta required")
        with self._desktop_lock():
            binding = self._binding(sending=False)
            self.platform.session_guard("read")
            activated = self.platform.activate_existing() if activate_existing else False
            layout = self._layout()
            window = self.platform.window(layout)
            self._title(layout, window)
            self.platform.scroll(window, layout.body, scroll_delta)
            self.platform.settle(0.25)
            self._title(layout, window)
            result = self._read_locked(layout, window)
            result["session_id"] = binding["agent_session_id"]
            result["activation"] = "existing_app_activated" if activated else "already_frontmost_or_not_requested"
            self._binding(sending=False)
            result["scroll_delta"] = scroll_delta
            return result

    def _attempt_path(self, request_id: str) -> Path:
        if not isinstance(request_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", request_id):
            raise NativeBlocked("request identity invalid")
        return self.state_dir / ("type-" + hashlib.sha256(request_id.encode()).hexdigest() + ".json")

    @staticmethod
    def _persist_once(path: Path, value: dict[str, Any]) -> None:
        encoded = (json.dumps(value, sort_keys=True) + "\n").encode()
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
        try:
            written = 0
            while written < len(encoded):
                written += os.write(fd, encoded[written:])
            os.fsync(fd)
        finally:
            os.close(fd)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)

    def _empty_draft(self, layout: Layout, window: WindowIdentity, *, require_focus: bool = False) -> None:
        image = self.platform.capture(window, layout.input)
        _image_healthy(image)
        if self.platform.ax_draft(window, layout.input_ax_id) != "":
            raise NativeBlocked("draft nonempty or AX unavailable")
        if require_focus and self.platform.ax_input_focused(window, layout.input_ax_id) is not True:
            raise NativeBlocked("input focus not verified")
        if self.platform.ocr(image):
            raise NativeBlocked("draft pixels contain text or hint")
        # Allow only a slim green insertion caret in the calibrated top-left
        # of an otherwise flat field. Other decoration/text fails closed.
        pixels = image.getdata()
        if not pixels:
            raise NativeBlocked("draft pixels unavailable")
        sample = image.getpixel((0, 0))
        exceptions = 0
        caret = 0
        for index, pixel in enumerate(pixels):
            if max(abs(c - base) for c, base in zip(pixel, sample)) <= 20:
                continue
            x, y = index % image.width, index // image.width
            red, green, blue = pixel
            if (require_focus and 16 <= x <= 32 and 0 <= y <= 35
                    and green > 110 and green >= red + 40 and green >= blue + 20):
                caret += 1
            else:
                exceptions += 1
            if exceptions >= 3 or caret > 160:
                break
        if exceptions >= 3 or caret > 160:
            raise NativeBlocked("draft pixels not provably empty")

    def _button(self, layout: Layout, window: WindowIdentity, *, enabled: bool) -> tuple[float, float]:
        image = self.platform.capture(window, layout.button)
        _image_healthy(image)
        lines = self.platform.ocr(image)
        if self.platform.ax_send_enabled(window, layout.button_ax_id) is not enabled:
            raise NativeBlocked("send button AX state unavailable or changed")
        x, y = _button_point(image, lines, layout.button, require_green=enabled)
        return window.bounds[0] + x, window.bounds[1] + y

    def dispatch(self, outbox: Outbox, session: Session, request_id: str, text: str) -> dict[str, Any]:
        if (not isinstance(session, Session) or session.account_id != self.target.account_id
                or not isinstance(request_id, str) or not isinstance(text, str)
                or not text.startswith(request_id + " ")
                or len(text) > 1000 or any(ord(c) < 32 for c in text)):
            raise NativeBlocked("prepared outgoing text invalid")
        with self._desktop_lock():
            self._binding(sending=True)
            self.platform.session_guard("send")
            if outbox.state(session.account_id, request_id) != "prepared":
                raise NativeBlocked("outbox request not prepared or already attempted")
            attempt = self._attempt_path(request_id)
            if attempt.exists():
                raise NativeBlocked("desktop input already attempted")
            layout = self._layout()
            window = self.platform.window(layout)
            self._title(layout, window)
            self._title(layout, window)
            if any(line.text.startswith(request_id) for line in self._lines(window, layout.body)):
                raise NativeBlocked("request marker already visible")
            self._title(layout, window)
            self._empty_draft(layout, window)
            self._button(layout, window, enabled=False)
            self._title(layout, window)
            self._empty_draft(layout, window)
            self._binding(sending=True)
            self.platform.session_guard("send")
            self._persist_once(attempt, {"phase": "type_attempt", "request_id_sha256": hashlib.sha256(request_id.encode()).hexdigest(),
                                         "window_id": window.window_id, "created_at": time.time()})
            try:
                self.platform.click(window.bounds[0] + layout.input.x + layout.input.width / 2,
                                    window.bounds[1] + layout.input.y + layout.input.height / 2)
                self._title(layout, window)
                self._empty_draft(layout, window, require_focus=True)
                self.platform.session_guard("send")
                self.platform.type_unicode(text)
                self.platform.settle(0.2)
                self._title(layout, window)
                if self.platform.ax_draft(window, layout.input_ax_id) != text:
                    raise NativeBlocked("typed draft AX mismatch")
                if not _exact_draft(self._lines(window, layout.input), text):
                    raise NativeBlocked("typed draft OCR mismatch")
                self._title(layout, window)
                self._button(layout, window, enabled=True)
                self._title(layout, window)
                observation = Observation(self.target, window, True, time.time(), draft_text=text)
                self._binding(sending=True)
                outbox.verify_draft(session, request_id, text, observation)

                class Driver:
                    def click_send(inner, target: FullTarget, expected: WindowIdentity) -> None:
                        if target != self.target or expected != window:
                            raise NativeBlocked("send target changed")
                        self._title(layout, window)
                        if self.platform.ax_draft(window, layout.input_ax_id) != text:
                            raise NativeBlocked("send draft changed")
                        if self.platform.ax_input_focused(window, layout.input_ax_id) is not True:
                            raise NativeBlocked("send focus changed")
                        if not _exact_draft(self._lines(window, layout.input), text):
                            raise NativeBlocked("send OCR draft changed")
                        x, y = self._button(layout, window, enabled=True)
                        self._title(layout, window)
                        self._binding(sending=True)
                        self.platform.session_guard("send")
                        self.platform.click(x, y)

                state = outbox.submit_once(session, request_id, text,
                                           Observation(self.target, window, True, time.time(), draft_text=text), Driver())
                if state != "submit_attempted":
                    return {"status": "indeterminate", "message_id_available": False}
                self.platform.settle(0.4)
                self._title(layout, window)
                body_image = self.platform.capture(window, layout.body)
                _image_healthy(body_image)
                after = self.platform.ocr(body_image)
                if not isinstance(after, list) or any(not isinstance(line, OCRLine) for line in after):
                    raise NativeBlocked("post-send OCR unavailable")
                self._title(layout, window)
                self._empty_draft(layout, window)
                matching = [line for line in after if line.text == text and line.confidence >= 0.70]
                if len(matching) == 1:
                    if _red_failure_icon(body_image, matching[0]):
                        failed = Observation(self.target, window, True, time.time(), failure_marker=request_id)
                        outbox.record_ui_outcome(session.account_id, request_id, failed, "failed")
                        return {"status": "failed", "message_id_available": False,
                                "recipient_confirmed": False}
                    seen = Observation(self.target, window, True, time.time(), visible_outgoing_text=text)
                    outbox.record_ui_outcome(session.account_id, request_id, seen, "ui_observed")
                    return {"status": "ui_observed", "message_id_available": False,
                            "recipient_confirmed": False}
            except Exception:
                # The input or click may have occurred. Never erase the attempt
                # or retry automatically; the caller must inspect the draft/UI.
                pass
            return {"status": "indeterminate", "message_id_available": False,
                    "recipient_confirmed": False}


class MacDesktopPlatform:
    """Real macOS APIs; created only on an explicitly requested native run."""

    def __init__(self):
        try:
            import AppKit
            import ApplicationServices as AX
            import Foundation
            import Quartz
            import ScreenCaptureKit as SC
            import Vision
            from PIL import Image
        except ImportError:
            raise NativeBlocked("optional macOS desktop dependencies unavailable") from None
        self.AppKit, self.AX, self.Foundation, self.Quartz, self.SC, self.Vision, self.Image = (
            AppKit, AX, Foundation, Quartz, SC, Vision, Image)

    def bind_layout(self, layout: Layout) -> None:
        self._active_layout = layout

    def activate_existing(self) -> bool:
        """Bring one already-running WeChat app forward, without choosing a chat."""
        apps = self.AppKit.NSRunningApplication.runningApplicationsWithBundleIdentifier_("com.tencent.xinWeChat")
        if len(apps) != 1:
            raise NativeBlocked("unique existing WeChat process unavailable")
        pid = apps[0].processIdentifier()
        front = self.AppKit.NSWorkspace.sharedWorkspace().frontmostApplication()
        if front is not None and front.processIdentifier() == pid:
            return False
        option = self.AppKit.NSApplicationActivateIgnoringOtherApps
        if apps[0].activateWithOptions_(option) is not True:
            raise NativeBlocked("WeChat activation failed")
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            front = self.AppKit.NSWorkspace.sharedWorkspace().frontmostApplication()
            if front is not None and front.processIdentifier() == pid:
                return True
            self.AppKit.NSRunLoop.currentRunLoop().runUntilDate_(
                self.AppKit.NSDate.dateWithTimeIntervalSinceNow_(0.05))
        raise NativeBlocked("WeChat did not become foreground")

    def _ax(self, element, attribute):
        error, value = self.AX.AXUIElementCopyAttributeValue(element, attribute, None)
        return value if error == 0 else None

    def _point(self, value):
        if value is None:
            return None
        error, point = self.AX.AXValueGetValue(value, self.AX.kAXValueCGPointType, None)
        return point if error else None

    def _size(self, value):
        if value is None:
            return None
        error, size = self.AX.AXValueGetValue(value, self.AX.kAXValueCGSizeType, None)
        return size if error else None

    def _window_ax(self, layout: Layout):
        self.session_guard("read")
        apps = self.AppKit.NSRunningApplication.runningApplicationsWithBundleIdentifier_("com.tencent.xinWeChat")
        front = self.AppKit.NSWorkspace.sharedWorkspace().frontmostApplication()
        if len(apps) != 1 or front is None or front.processIdentifier() != apps[0].processIdentifier():
            raise NativeBlocked("WeChat not unique foreground application")
        pid = apps[0].processIdentifier()
        root = self.AX.AXUIElementCreateApplication(pid)
        focused = self._ax(root, self.AX.kAXFocusedWindowAttribute)
        windows = self._ax(root, self.AX.kAXWindowsAttribute) or []
        if focused is None or sum(item == focused for item in windows) != 1:
            raise NativeBlocked("focused WeChat window unavailable")
        main = self._ax(root, self.AX.kAXMainWindowAttribute)
        if layout.kind == "main" and main != focused:
            raise NativeBlocked("main window not focused")
        if self._ax(focused, "AXModal") is not False:
            raise NativeBlocked("window modal state unknown")
        if self._ax(focused, self.AX.kAXSubroleAttribute) != "AXStandardWindow":
            raise NativeBlocked("unsupported window type")
        if len(windows) > 2:
            raise NativeBlocked("other WeChat windows visible")
        if len(windows) == 2:
            other = next(item for item in windows if item != focused)
            if self._ax(other, "AXModal") is not False:
                raise NativeBlocked("other window may be modal")
            if layout.kind != "main":
                raise NativeBlocked("other chat window present")
            focused_position = self._point(self._ax(focused, self.AX.kAXPositionAttribute))
            other_position = self._point(self._ax(other, self.AX.kAXPositionAttribute))
            other_size = self._size(self._ax(other, self.AX.kAXSizeAttribute))
            if (focused_position is None or other_position is None or other_size is None
                    or self._ax(other, self.AX.kAXSubroleAttribute) != "AXDialog"
                    or tuple(other_position) != (focused_position[0] + 6, focused_position[1] + 5)
                    or tuple(other_size) != (52, 20)):
                raise NativeBlocked("other chat window present")
        position = self._point(self._ax(focused, self.AX.kAXPositionAttribute))
        size = self._size(self._ax(focused, self.AX.kAXSizeAttribute))
        if position is None or size is None or tuple(size) != (layout.width, layout.height):
            raise NativeBlocked("window geometry changed")
        if position[0] != int(position[0]) or position[1] != int(position[1]):
            raise NativeBlocked("fractional window origin unsupported")
        return pid, focused, (int(position[0]), int(position[1]), layout.width, layout.height)

    def session_guard(self, purpose: str) -> str:
        if purpose not in ("read", "send"):
            raise NativeBlocked("session purpose unavailable")
        session = self.Quartz.CGSessionCopyCurrentDictionary()
        if (session is None or not hasattr(session, "get")
                or session.get(self.Quartz.kCGSessionOnConsoleKey) is not True
                or session.get(self.Quartz.kCGSessionLoginDoneKey) is not True):
            raise NativeBlocked("active console session unavailable")
        locked = session.get("CGSSessionScreenIsLocked")
        if locked is True:
            raise NativeBlocked("screen is locked")
        if purpose == "send" and locked is not False:
            raise NativeBlocked("unlocked screen not verified for send")
        return ("screen_unlocked_marker_false" if locked is False
                else "active_session_lock_marker_unavailable")

    def window(self, layout: Layout) -> WindowIdentity:
        pid, _, bounds = self._window_ax(layout)
        q = self.Quartz
        windows = q.CGWindowListCopyWindowInfo(q.kCGWindowListOptionOnScreenOnly | q.kCGWindowListExcludeDesktopElements,
                                                q.kCGNullWindowID) or []
        matches = [item for item in windows if item.get("kCGWindowOwnerPID") == pid
                   and dict(item.get("kCGWindowBounds", {})) == dict(zip(("X", "Y", "Width", "Height"), bounds))
                   and item.get("kCGWindowLayer") == 0]
        if len(matches) != 1:
            raise NativeBlocked("unique on-screen window unavailable")
        return WindowIdentity(pid, int(matches[0]["kCGWindowNumber"]), bounds)

    def _await(self, start):
        result = []
        start(lambda value, error: result.append((value, error)))
        deadline = time.monotonic() + 4
        while not result and time.monotonic() < deadline:
            self.AppKit.NSRunLoop.currentRunLoop().runUntilDate_(self.AppKit.NSDate.dateWithTimeIntervalSinceNow_(0.02))
        if not result or result[0][1] is not None or result[0][0] is None:
            raise NativeBlocked("window capture unavailable")
        return result[0][0]

    def capture(self, window: WindowIdentity, crop: Crop):
        # Rebind the AX/CG identity immediately before every screenshot.
        layout = getattr(self, "_active_layout", None)
        if layout is None or self.window(layout) != window:
            raise NativeBlocked("window identity changed before capture")
        sx, sy, sw, sh = window.bounds
        if crop.x < 0 or crop.y < 0 or crop.x + crop.width > sw or crop.y + crop.height > sh:
            raise NativeBlocked("crop outside window")
        content = self._await(lambda cb: self.SC.SCShareableContent
                              .getShareableContentExcludingDesktopWindows_onScreenWindowsOnly_completionHandler_(True, True, cb))
        candidates = [item for item in content.windows() if item.owningApplication() is not None
                      and item.owningApplication().processID() == window.pid and item.windowID() == window.window_id
                      and (item.frame().origin.x, item.frame().origin.y,
                           item.frame().size.width, item.frame().size.height) == window.bounds]
        if len(candidates) != 1:
            raise NativeBlocked("capture source unavailable")
        _single_containing_display(content.displays(),
                                   (sx + crop.x, sy + crop.y, crop.width, crop.height))
        filt = self.SC.SCContentFilter.alloc().initWithDesktopIndependentWindow_(candidates[0])
        if filt is None or not hasattr(filt, "pointPixelScale"):
            raise NativeBlocked("single-window capture scale unavailable")
        scale = float(filt.pointPixelScale())
        if not math.isfinite(scale) or scale not in (1.0, 2.0):
            raise NativeBlocked("single-window capture scale unsupported")
        config = self.SC.SCStreamConfiguration.alloc().init()
        # ScreenCaptureKit ignores sourceRect for a single-window filter. It
        # produces the window image; crop its CGImage before reading any pixels.
        config.setWidth_(int(sw * scale))
        config.setHeight_(int(sh * scale))
        config.setShowsCursor_(False)
        if hasattr(config, "setIgnoreShadowsSingleWindow_"):
            config.setIgnoreShadowsSingleWindow_(True)
        cg = self._await(lambda cb: self.SC.SCScreenshotManager.captureImageWithFilter_configuration_completionHandler_(
            filt, config, cb))
        q = self.Quartz
        pixel_crop = _window_local_pixel_crop(window, crop, (q.CGImageGetWidth(cg), q.CGImageGetHeight(cg)), scale)
        clipped = q.CGImageCreateWithImageInRect(cg, q.CGRectMake(pixel_crop.x, pixel_crop.y,
                                                                 pixel_crop.width, pixel_crop.height))
        if (clipped is None or q.CGImageGetWidth(clipped) != pixel_crop.width
                or q.CGImageGetHeight(clipped) != pixel_crop.height):
            raise NativeBlocked("single-window crop unavailable")
        # A cropped CGImage may still share its original data provider. Never
        # copy any CGImage provider; draw only the clipped image into a newly
        # allocated buffer whose dimensions are exactly the approved crop.
        cg = None
        pixels = bytearray(pixel_crop.width * pixel_crop.height * 4)
        color_space = q.CGColorSpaceCreateWithName(q.kCGColorSpaceSRGB)
        context = q.CGBitmapContextCreate(pixels, pixel_crop.width, pixel_crop.height, 8, pixel_crop.width * 4,
                                          color_space, q.kCGImageAlphaPremultipliedLast | q.kCGBitmapByteOrder32Big)
        if context is None:
            raise NativeBlocked("capture color conversion unavailable")
        rectangle = q.CGRectMake(0, 0, pixel_crop.width, pixel_crop.height)
        q.CGContextDrawImage(context, rectangle, clipped)
        if not any(pixels[3::4]):
            raise NativeBlocked("capture fully transparent")
        if all(max(pixels[channel::4]) <= 2 for channel in (0, 1, 2)):
            raise NativeBlocked("capture blank or black")
        q.CGContextSetRGBFillColor(context, 1, 1, 1, 1)
        q.CGContextFillRect(context, rectangle)
        q.CGContextDrawImage(context, rectangle, clipped)
        image = self.Image.frombytes("RGBA", (pixel_crop.width, pixel_crop.height), bytes(pixels)).convert("RGB")
        _image_healthy(image)
        if self.window(layout) != window:
            raise NativeBlocked("window changed during capture")
        return image

    def ocr(self, image: Any) -> list[OCRLine]:
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        encoded = buffer.getvalue()
        data = self.Foundation.NSData.dataWithBytes_length_(encoded, len(encoded))
        request = self.Vision.VNRecognizeTextRequest.alloc().init()
        request.setRecognitionLevel_(0)
        request.setUsesLanguageCorrection_(False)
        request.setRecognitionLanguages_(["zh-Hans", "en-US"])
        handler = self.Vision.VNImageRequestHandler.alloc().initWithData_options_(data, None)
        completed = handler.performRequests_error_([request], None)
        succeeded = completed[0] if isinstance(completed, tuple) else completed
        if not succeeded:
            raise NativeBlocked("Vision recognition unavailable")
        lines = []
        for item in request.results() or []:
            candidates = item.topCandidates_(1)
            if not candidates:
                continue
            candidate = candidates[0]
            box = item.boundingBox()
            lines.append(OCRLine(str(candidate.string()), float(candidate.confidence()),
                                 (float(box.origin.x), float(box.origin.y),
                                  float(box.size.width), float(box.size.height))))
        return sorted(lines, key=lambda line: (-line.box[1], line.box[0]))

    def _element(self, window: WindowIdentity, identifier: str, role: str, crop: Crop):
        layout = getattr(self, "_active_layout", None)
        if layout is None:
            raise NativeBlocked("layout unavailable")
        pid, root, bounds = self._window_ax(layout)
        if pid != window.pid or bounds != window.bounds:
            raise NativeBlocked("window changed")
        matches = []
        pending = [(root, 0)]
        visited = 0
        while pending:
            item, depth = pending.pop()
            visited += 1
            if visited > 500 or depth > 20:
                raise NativeBlocked("AX traversal limit")
            if layout.kind == "main" and depth:
                position = self._point(self._ax(item, self.AX.kAXPositionAttribute))
                size = self._size(self._ax(item, self.AX.kAXSizeAttribute))
                if (position is not None and size is not None
                        and position[0] + size[0] <= bounds[0] + layout.width * 0.30):
                    continue
            if (self._ax(item, self.AX.kAXIdentifierAttribute) == identifier
                    and self._ax(item, self.AX.kAXRoleAttribute) == role):
                matches.append(item)
            else:
                pending.extend((child, depth + 1) for child in (self._ax(item, self.AX.kAXChildrenAttribute) or []))
        if len(matches) != 1:
            raise NativeBlocked("required AX control unavailable")
        element = matches[0]
        position = self._point(self._ax(element, self.AX.kAXPositionAttribute))
        size = self._size(self._ax(element, self.AX.kAXSizeAttribute))
        if position is None or size is None or size[0] <= 0 or size[1] <= 0:
            raise NativeBlocked("AX control geometry unavailable")
        center_x, center_y = position[0] + size[0] / 2, position[1] + size[1] / 2
        if not (window.bounds[0] + crop.x <= center_x <= window.bounds[0] + crop.x + crop.width
                and window.bounds[1] + crop.y <= center_y <= window.bounds[1] + crop.y + crop.height):
            raise NativeBlocked("AX control outside calibrated crop")
        return element

    def ax_draft(self, window: WindowIdentity, identifier: str) -> str:
        layout = self._active_layout
        value = self._ax(self._element(window, identifier, self.AX.kAXTextAreaRole, layout.input),
                         self.AX.kAXValueAttribute)
        if not isinstance(value, str):
            raise NativeBlocked("AX draft unavailable")
        return value

    def ax_input_focused(self, window: WindowIdentity, identifier: str) -> bool:
        layout = self._active_layout
        field = self._element(window, identifier, self.AX.kAXTextAreaRole, layout.input)
        root = self.AX.AXUIElementCreateApplication(window.pid)
        focused = self._ax(root, self.AX.kAXFocusedUIElementAttribute)
        return self._ax(field, self.AX.kAXFocusedAttribute) is True and focused == field

    def ax_send_enabled(self, window: WindowIdentity, identifier: str) -> bool:
        layout = self._active_layout
        value = self._ax(self._element(window, identifier, self.AX.kAXButtonRole, layout.button),
                         self.AX.kAXEnabledAttribute)
        if value is not True and value is not False:
            raise NativeBlocked("AX send state unavailable")
        return value

    def click(self, x: float, y: float) -> None:
        q = self.Quartz
        point = q.CGPoint(x, y)
        down = q.CGEventCreateMouseEvent(None, q.kCGEventLeftMouseDown, point, 0)
        up = q.CGEventCreateMouseEvent(None, q.kCGEventLeftMouseUp, point, 0)
        if down is None or up is None:
            raise NativeBlocked("mouse event unavailable")
        q.CGEventPost(q.kCGHIDEventTap, down)
        q.CGEventPost(q.kCGHIDEventTap, up)

    def scroll(self, window: WindowIdentity, crop: Crop, delta: int) -> None:
        layout = getattr(self, "_active_layout", None)
        if layout is None or self.window(layout) != window:
            raise NativeBlocked("scroll window changed")
        q = self.Quartz
        event = q.CGEventCreateScrollWheelEvent(None, q.kCGScrollEventUnitLine, 1, delta)
        if event is None:
            raise NativeBlocked("scroll event unavailable")
        x = window.bounds[0] + crop.x + crop.width / 2
        y = window.bounds[1] + crop.y + crop.height / 2
        q.CGEventSetLocation(event, q.CGPoint(x, y))
        q.CGEventPost(q.kCGHIDEventTap, event)

    def type_unicode(self, text: str) -> None:
        q = self.Quartz
        units = len(text.encode("utf-16-le")) // 2
        down = q.CGEventCreateKeyboardEvent(None, 0, True)
        up = q.CGEventCreateKeyboardEvent(None, 0, False)
        if down is None or up is None:
            raise NativeBlocked("keyboard event unavailable")
        for event in (down, up):
            q.CGEventSetFlags(event, 0)
            q.CGEventKeyboardSetUnicodeString(event, units, text)
        q.CGEventPost(q.kCGHIDEventTap, down)
        q.CGEventPost(q.kCGHIDEventTap, up)

    def settle(self, seconds: float) -> None:
        self.AppKit.NSRunLoop.currentRunLoop().runUntilDate_(self.AppKit.NSDate.dateWithTimeIntervalSinceNow_(seconds))

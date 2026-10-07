"""Offline guards for the bounded native adapter; no macOS API is invoked."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

from wechat_desktop_agent.native import (
    Crop, Layout, MacDesktopPlatform, NativeBlocked, NativeChannelBackend, OCRLine, _single_containing_display,
    _window_local_pixel_crop,
)
from wechat_desktop_agent.outbox import FullTarget, Outbox, Session, WindowIdentity


TARGET = FullTarget("com.tencent.xinWeChat", "local-account-label", "local-channel-id", "Synthetic group (3)")
WINDOW = WindowIdentity(404, 202, (10, 20, 913, 875))
LAYOUT = {
    "kind": "main", "window_size": [913, 875],
    "regions": {"header": [320, 20, 400, 32], "body": [320, 60, 580, 630],
                "input": [320, 746, 560, 65], "button": [820, 814, 75, 40]},
    "ax": {"input": "synthetic_input", "send_button": "synthetic_send"},
}


class FakeImage:
    mode = "RGB"

    def __init__(self, tag, size=(800, 64)):
        self.tag, self.size = tag, size
        self.green = False

    @property
    def width(self):
        return self.size[0]

    def getextrema(self):
        return ((250, 250), (250, 250), (250, 250))

    def tobytes(self):
        return (self.tag + ":pixels").encode()

    def getdata(self):
        return ([(30, 150, 50)] if self.green else [(250, 250, 250)]) * 30

    def getpixel(self, point):
        return (250, 250, 250)

    def crop(self, box):
        return self


class BlankImage(FakeImage):
    def getextrema(self):
        return ((0, 0), (0, 0), (0, 0))


class CaretImage(FakeImage):
    def __init__(self, *, extra_mark=False):
        super().__init__("input", (1160, 130))
        self.extra_mark = extra_mark

    def getdata(self):
        pixels = [(250, 250, 250)] * (self.size[0] * self.size[1])
        for y in range(5, 29):
            for x in (20, 21):
                pixels[y * self.size[0] + x] = (20, 170, 40)
        if self.extra_mark:
            for x in (100, 101, 102):
                pixels[25 * self.size[0] + x] = (50, 50, 50)
        return pixels


class RedWarningImage(FakeImage):
    def getpixel(self, point):
        x, y = point
        return (210, 30, 35) if 100 <= x <= 107 and 42 <= y <= 49 else (250, 250, 250)


class FakePlatform:
    def __init__(self):
        self.window_identity = WINDOW
        self.calls = []
        self.fail_type = False
        self.draft = ""
        self.header_text = TARGET.displayed_title
        self.header_confidence = 0.99
        self.header_duplicate = False
        self.fail_header_capture = False
        self.change_after_header_capture = False
        self.blank_body = False
        self.change_after_scroll = False
        self.sent_text = None
        self.focused = False
        self.caret_after_focus = False
        self.extra_input_mark = False
        self.red_after_send = False
        self.lock_marker = False
        self.running_app_count = 1
        self.activation_fails = False
        self.frontmost = True

    def session_guard(self, purpose):
        self.calls.append(("session_guard", purpose))
        if self.lock_marker is True or (purpose == "send" and self.lock_marker is not False):
            raise NativeBlocked("synthetic locked or unknown send session")
        return ("screen_unlocked_marker_false" if self.lock_marker is False
                else "active_session_lock_marker_unavailable")

    def activate_existing(self):
        self.calls.append(("activate_existing",))
        if self.running_app_count != 1 or self.activation_fails:
            raise NativeBlocked("synthetic activation unavailable")
        activated = not self.frontmost
        self.frontmost = True
        return activated

    def window(self, layout):
        if not self.frontmost:
            raise NativeBlocked("synthetic WeChat not foreground")
        return self.window_identity

    def capture(self, window, crop: Crop):
        tag = "header" if crop.y == 20 else "body" if crop.y == 60 else "input" if crop.y == 746 else "button"
        self.calls.append(("capture", tag))
        if tag == "header" and self.fail_header_capture:
            raise NativeBlocked("synthetic empty frame")
        if tag == "header" and self.change_after_header_capture:
            self.window_identity = WindowIdentity(WINDOW.pid, WINDOW.window_id + 1, WINDOW.bounds)
        image = (BlankImage(tag) if tag == "body" and self.blank_body
                 else RedWarningImage(tag) if tag == "body" and self.sent_text and self.red_after_send
                 else CaretImage(extra_mark=self.extra_input_mark)
                 if tag == "input" and self.focused and self.caret_after_focus
                 else FakeImage(tag))
        image.green = tag == "button" and bool(self.draft)
        return image

    def ocr(self, image):
        if image.tag == "header":
            found = OCRLine(self.header_text, self.header_confidence, (0.2, 0.2, 0.5, 0.5))
            return [found, found] if self.header_duplicate else [found]
        if image.tag == "body":
            return [OCRLine("Synthetic request", 0.95, (0.2, 0.3, 0.4, 0.1))] + (
                [OCRLine(self.sent_text, 0.99, (0.2, 0.2, 0.6, 0.1))] if self.sent_text else [])
        if image.tag == "button":
            return [OCRLine("发送", 0.99, (0.3, 0.3, 0.4, 0.3))]
        if image.tag == "input" and self.draft:
            return [OCRLine(self.draft, 0.99, (0.2, 0.2, 0.6, 0.3))]
        return []

    def ax_draft(self, window, identifier):
        return self.draft

    def ax_input_focused(self, window, identifier):
        return self.focused

    def ax_send_enabled(self, window, identifier):
        return bool(self.draft)

    def click(self, x, y):
        self.calls.append(("click", x, y))
        if x <= WINDOW.bounds[0] + 800:
            self.focused = True
        if x > WINDOW.bounds[0] + 800 and self.draft:
            self.sent_text = self.draft
            self.draft = ""

    def type_unicode(self, text):
        self.calls.append(("type", text))
        if self.fail_type:
            raise RuntimeError("synthetic post uncertainty")
        self.draft = text

    def settle(self, seconds):
        self.calls.append(("settle", seconds))

    def scroll(self, window, crop, delta):
        self.calls.append(("scroll", delta, crop.y))
        if self.change_after_scroll:
            self.window_identity = WindowIdentity(WINDOW.pid, WINDOW.window_id + 1, WINDOW.bounds)


class FakeOutbox:
    def state(self, account_id, request_id):
        return "prepared"


class NativeGuardTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.state = Path(self.temporary.name)
        os.chmod(self.state, 0o700)
        (self.state / "layout.json").write_text(json.dumps(LAYOUT))
        os.chmod(self.state / "layout.json", 0o600)
        (self.state / "channel.json").write_text(json.dumps({
            "version": 1, "channel_id": TARGET.conversation_id,
            "account_binding_id": TARGET.account_id, "group_title": TARGET.displayed_title,
            "agent_session_id": "agent-session-label", "paused": False,
        }))
        os.chmod(self.state / "channel.json", 0o600)
        self.platform = FakePlatform()
        self.backend = NativeChannelBackend(TARGET, self.state, platform=self.platform)

    def test_read_is_viewport_only_and_direction_unknown(self):
        result = self.backend.read(activate_existing=True)
        self.assertEqual(result["coverage"], "viewport_only")
        self.assertEqual(result["session_id"], "agent-session-label")
        self.assertEqual(result["lines"][0]["direction"], "unknown")
        self.assertEqual(result["lines"][0]["confidence"], 0.95)
        self.assertFalse(result["message_id_available"])
        self.assertFalse(result["actual_wechat_account_verified"])
        self.assertEqual(result["screen_lock_evidence"], "screen_unlocked_marker_false")
        self.assertEqual({call[1] for call in self.platform.calls if call[0] == "capture"},
                         {"header", "body"})
        self.assertLess(self.platform.calls.index(("session_guard", "read")),
                        self.platform.calls.index(("activate_existing",)))
        self.assertEqual(result["activation"], "already_frontmost_or_not_requested")

    def test_header_diagnostic_reports_exact_low_confidence_without_gate(self):
        self.platform.header_confidence = 0.5
        result = self.backend.diagnose_header()
        self.assertEqual(set(result), {"status", "observed_at", "window", "capture_healthy",
                                       "title_exact", "matching_title_confidences",
                                       "automatic_title_gate_passed", "identity_verified", "body_read",
                                       "activation_requested", "activated_existing", "phase", "reason"})
        self.assertEqual(result["status"], "header_observed")
        self.assertTrue(result["capture_healthy"])
        self.assertTrue(result["title_exact"])
        self.assertEqual(result["matching_title_confidences"], [0.5])
        self.assertFalse(result["automatic_title_gate_passed"])
        self.assertFalse(result["identity_verified"])
        self.assertFalse(result["body_read"])
        self.assertFalse(result["activation_requested"])
        self.assertIsNone(result["activated_existing"])
        self.assertEqual(result["phase"], "complete")
        self.assertEqual(result["reason"], "automatic_title_gate_not_passed")
        self.assertEqual([call for call in self.platform.calls if call[0] == "capture"], [("capture", "header")])
        self.assertFalse(any(call[0] in ("click", "type", "scroll") for call in self.platform.calls))
        self.platform.calls.clear()
        with self.assertRaises(NativeBlocked):
            self.backend.read()
        self.assertNotIn(("capture", "body"), self.platform.calls)

    def test_header_diagnostic_activation_is_explicit_and_never_reads_body(self):
        self.platform.frontmost = False
        blocked = self.backend.diagnose_header()
        self.assertEqual(blocked["phase"], "window")
        self.assertFalse(any(call[0] == "activate_existing" for call in self.platform.calls))
        self.platform.calls.clear()
        observed = self.backend.diagnose_header(activate_existing=True)
        self.assertEqual(observed["status"], "header_observed")
        self.assertTrue(observed["activation_requested"])
        self.assertTrue(observed["activated_existing"])
        self.assertTrue(observed["automatic_title_gate_passed"])
        self.assertEqual([call for call in self.platform.calls if call[0] == "capture"], [("capture", "header")])

    def test_header_diagnostic_wrong_title_is_blocked_without_disclosure(self):
        self.platform.header_text = "Other synthetic group (3)"
        result = self.backend.diagnose_header()
        self.assertEqual(result["status"], "blocked")
        self.assertTrue(result["capture_healthy"])
        self.assertFalse(result["title_exact"])
        self.assertEqual(result["matching_title_confidences"], [])
        self.assertFalse(result["automatic_title_gate_passed"])
        self.assertEqual(result["phase"], "ocr")
        self.assertEqual(result["reason"], "title_mismatch")
        self.assertNotIn(self.platform.header_text, json.dumps(result))
        self.assertNotIn(("capture", "body"), self.platform.calls)

    def test_header_diagnostic_duplicate_exact_title_is_not_unique(self):
        self.platform.header_duplicate = True
        result = self.backend.diagnose_header()
        self.assertEqual(result["status"], "blocked")
        self.assertFalse(result["title_exact"])
        self.assertEqual(result["matching_title_confidences"], [0.99, 0.99])
        self.assertFalse(result["automatic_title_gate_passed"])
        self.assertEqual(result["reason"], "title_mismatch")
        self.assertNotIn(("capture", "body"), self.platform.calls)
        self.platform.calls.clear()
        original_ocr = self.platform.ocr
        self.platform.ocr = lambda image: ([OCRLine(TARGET.displayed_title, 0.95, (0.2, 0.2, 0.5, 0.5)),
                                            OCRLine(TARGET.displayed_title, 0.5, (0.2, 0.2, 0.5, 0.5))]
                                           if image.tag == "header" else original_ocr(image))
        mixed = self.backend.diagnose_header()
        self.assertEqual(mixed["status"], "blocked")
        self.assertFalse(mixed["automatic_title_gate_passed"])

    def test_header_diagnostic_rejects_non_boolean_activation_before_platform(self):
        for value in (1, "false", None):
            with self.subTest(value=value), self.assertRaises(NativeBlocked):
                self.backend.diagnose_header(activate_existing=value)
        self.assertEqual(self.platform.calls, [])

    def test_header_diagnostic_capture_failure_or_window_change_never_claims_title(self):
        for changed in ("fail_header_capture", "change_after_header_capture"):
            with self.subTest(changed=changed):
                platform = FakePlatform()
                setattr(platform, changed, True)
                backend = NativeChannelBackend(TARGET, self.state, platform=platform)
                result = backend.diagnose_header()
                self.assertEqual(result["status"], "blocked")
                self.assertFalse(result["title_exact"])
                self.assertFalse(result["automatic_title_gate_passed"])
                self.assertEqual(result["matching_title_confidences"], [])
                self.assertEqual(result["phase"], "capture" if changed == "fail_header_capture" else "recheck")
                self.assertNotIn(("capture", "body"), platform.calls)
                self.assertFalse(any(call[0] in ("click", "type", "scroll") for call in platform.calls))

    def test_header_diagnostic_missing_layout_blocks_before_capture(self):
        (self.state / "layout.json").unlink()
        result = self.backend.diagnose_header()
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["phase"], "layout")
        self.assertFalse(result["title_exact"])
        self.assertFalse(any(call[0] == "capture" for call in self.platform.calls))

    def test_page_scrolls_only_body_after_title_check(self):
        result = self.backend.page(-2)
        self.assertEqual(result["scroll_delta"], -2)
        self.assertIn(("scroll", -2, 60), self.platform.calls)
        with self.assertRaises(NativeBlocked):
            self.backend.page(4)

    def test_page_window_change_refuses_new_snapshot(self):
        self.platform.change_after_scroll = True
        with self.assertRaises(NativeBlocked):
            self.backend.page(-1)
        self.assertNotIn(("capture", "body"), self.platform.calls)

    def test_wrong_header_refuses_body_capture(self):
        self.platform.header_text = "Other synthetic group (3)"
        with self.assertRaises(NativeBlocked):
            self.backend.read()
        self.assertNotIn(("capture", "body"), self.platform.calls)

    def test_activation_failure_or_multiple_apps_never_captures(self):
        self.platform.frontmost = False
        self.platform.running_app_count = 2
        with self.assertRaises(NativeBlocked):
            self.backend.read(activate_existing=True)
        self.assertFalse(any(call[0] == "capture" for call in self.platform.calls))
        self.platform.running_app_count = 1
        self.platform.activation_fails = True
        with self.assertRaises(NativeBlocked):
            self.backend.read(activate_existing=True)
        self.assertFalse(any(call[0] == "capture" for call in self.platform.calls))

    def test_activation_is_opt_in_and_default_still_requires_frontmost(self):
        self.platform.frontmost = False
        with self.assertRaises(NativeBlocked):
            self.backend.read()
        self.assertFalse(any(call[0] == "activate_existing" for call in self.platform.calls))
        self.assertFalse(any(call[0] == "capture" for call in self.platform.calls))
        snapshot = self.backend.read(activate_existing=True)
        self.assertEqual(snapshot["activation"], "existing_app_activated")
        self.assertTrue(self.platform.frontmost)

    def test_page_activation_is_opt_in(self):
        self.platform.frontmost = False
        with self.assertRaises(NativeBlocked):
            self.backend.page(-1)
        self.assertFalse(any(call[0] == "activate_existing" for call in self.platform.calls))
        self.assertFalse(any(call[0] == "scroll" for call in self.platform.calls))
        self.backend.page(-1, activate_existing=True)
        self.assertIn(("scroll", -1, 60), self.platform.calls)

    def test_negative_origin_one_display_allowed_cross_display_refused(self):
        def display(x, y, width, height):
            frame = SimpleNamespace(origin=SimpleNamespace(x=x, y=y),
                                    size=SimpleNamespace(width=width, height=height))
            return SimpleNamespace(frame=lambda: frame)

        left = display(-1920, 0, 1920, 1080)
        right = display(0, 0, 1920, 1080)
        self.assertIs(_single_containing_display([left, right], (-700, 500, 400, 100)), left)
        self.assertIs(_single_containing_display([left, right], (100, 500, 400, 100)), right)
        with self.assertRaises(NativeBlocked):
            _single_containing_display([left, right], (-100, 500, 400, 100))

    def test_single_window_crop_uses_local_coordinates_even_on_negative_display(self):
        window = WindowIdentity(404, 202, (-971, 436, 787, 622))
        self.assertEqual(_window_local_pixel_crop(window, Crop(270, 16, 465, 38),
                                                  (1574, 1244), 2), Crop(540, 32, 930, 76))
        self.assertEqual(_window_local_pixel_crop(window, Crop(270, 16, 465, 38),
                                                  (787, 622), 1), Crop(270, 16, 465, 38))

    def test_single_window_crop_rejects_bad_frame_scale_or_bounds(self):
        window = WindowIdentity(404, 202, (-971, 436, 787, 622))
        approved = Crop(270, 16, 465, 38)
        for size, scale in [((1575, 1244), 2), ((1574, 1243), 2),
                            ((2361, 1866), 3), ((0, 1244), 2), ((1574, 1244), float("nan"))]:
            with self.subTest(size=size, scale=scale), self.assertRaises(NativeBlocked):
                _window_local_pixel_crop(window, approved, size, scale)
        for crop in (Crop(-1, 16, 465, 38), Crop(270, 16, 600, 38),
                     Crop(270, 600, 465, 38), Crop(270, 16, 0, 38)):
            with self.subTest(crop=crop), self.assertRaises(NativeBlocked):
                _window_local_pixel_crop(window, crop, (1574, 1244), 2)

    def test_single_window_capture_crops_before_provider_access(self):
        window = WindowIdentity(404, 202, (-971, 436, 787, 622))
        crop = Crop(270, 16, 465, 38)
        calls = []
        frame = SimpleNamespace(origin=SimpleNamespace(x=-971, y=436),
                                size=SimpleNamespace(width=787, height=622))
        source = SimpleNamespace(owningApplication=lambda: SimpleNamespace(processID=lambda: 404),
                                 windowID=lambda: 202, frame=lambda: frame)
        display_frame = SimpleNamespace(origin=SimpleNamespace(x=-1920, y=0),
                                        size=SimpleNamespace(width=1920, height=1080))
        content = SimpleNamespace(windows=lambda: [source],
                                  displays=lambda: [SimpleNamespace(frame=lambda: display_frame)])
        full = SimpleNamespace(kind="full", size=(1574, 1244))
        clipped = SimpleNamespace(kind="clipped", size=(930, 76))
        render_mode = ["visible"]

        class Filter:
            @classmethod
            def alloc(cls):
                return cls()

            def initWithDesktopIndependentWindow_(self, item):
                self.assert_source = item
                calls.append(("single_window_filter", item))
                return self

            def pointPixelScale(self):
                return 2

        class Config:
            @classmethod
            def alloc(cls):
                return cls()

            def init(self):
                return self

            def setWidth_(self, value):
                calls.append(("width", value))

            def setHeight_(self, value):
                calls.append(("height", value))

            def setShowsCursor_(self, value):
                calls.append(("cursor", value))

            def setIgnoreShadowsSingleWindow_(self, value):
                calls.append(("ignore_shadows", value))

        class QuartzStub:
            kCGColorSpaceSRGB = "srgb"
            kCGImageAlphaPremultipliedLast = 1
            kCGBitmapByteOrder32Big = 2

            @staticmethod
            def CGRectMake(*values):
                return values

            @staticmethod
            def CGImageGetWidth(image):
                return image.size[0]

            @staticmethod
            def CGImageGetHeight(image):
                return image.size[1]

            @staticmethod
            def CGImageCreateWithImageInRect(image, rect):
                self.assertIs(image, full)
                calls.append(("crop", rect))
                return clipped

            @staticmethod
            def CGImageGetDataProvider(image):
                raise AssertionError("CGImage provider accessed")

            @staticmethod
            def CGDataProviderCopyData(image):
                raise AssertionError("CGImage provider copied")

            @staticmethod
            def CGColorSpaceCreateWithName(name):
                return name

            @staticmethod
            def CGBitmapContextCreate(pixels, width, height, bits, stride, space, flags):
                self.assertEqual((width, height, stride, len(pixels)), (930, 76, 3720, 282720))
                calls.append(("bounded_context", width, height))
                return pixels

            @staticmethod
            def CGContextDrawImage(pixels, rect, image):
                color = {"visible": b"\xfa\xfa\xfa\xff", "black": b"\x00\x00\x00\xff",
                         "transparent": b"\x00\x00\x00\x00"}[render_mode[0]]
                pixels[:] = color * (len(pixels) // 4)

            @staticmethod
            def CGContextSetRGBFillColor(*args):
                pass

            @staticmethod
            def CGContextFillRect(*args):
                pass

        platform = MacDesktopPlatform.__new__(MacDesktopPlatform)
        platform._active_layout = object()
        platform.window = lambda layout: window
        platform._await = lambda start: start(lambda value, error: None)
        platform.SC = SimpleNamespace(
            SCShareableContent=SimpleNamespace(
                getShareableContentExcludingDesktopWindows_onScreenWindowsOnly_completionHandler_=
                lambda exclude, onscreen, callback: content),
            SCContentFilter=Filter, SCStreamConfiguration=Config,
            SCScreenshotManager=SimpleNamespace(captureImageWithFilter_configuration_completionHandler_=
                                                lambda filt, config, callback: full),
        )
        platform.Quartz = QuartzStub
        class CapturedImage(FakeImage):
            def convert(self, mode):
                return self

        platform.Image = SimpleNamespace(frombytes=lambda mode, size, pixels: CapturedImage("cropped", size))
        image = platform.capture(window, crop)
        self.assertEqual(image.size, (930, 76))
        self.assertIn(("crop", (540, 32, 930, 76)), calls)
        self.assertIn(("bounded_context", 930, 76), calls)
        self.assertIn(("ignore_shadows", True), calls)
        self.assertFalse(any(name == "source_rect" for name, *_ in calls))
        for mode in ("black", "transparent"):
            with self.subTest(mode=mode), self.assertRaises(NativeBlocked):
                render_mode[0] = mode
                platform.capture(window, crop)

    def test_real_platform_activation_adapter_requires_one_existing_app(self):
        class Running:
            def __init__(self, pid, succeeds=True):
                self.pid, self.succeeds, self.calls = pid, succeeds, 0

            def processIdentifier(self):
                return self.pid

            def activateWithOptions_(self, options):
                self.calls += 1
                if self.succeeds:
                    workspace.foreground = self
                return self.succeeds

        class Workspace:
            foreground = None

            def frontmostApplication(self):
                return self.foreground

        workspace = Workspace()
        app = Running(404)
        running = [app, Running(405)]
        platform = MacDesktopPlatform.__new__(MacDesktopPlatform)
        platform.AppKit = SimpleNamespace(
            NSRunningApplication=SimpleNamespace(runningApplicationsWithBundleIdentifier_=lambda bundle: running),
            NSWorkspace=SimpleNamespace(sharedWorkspace=lambda: workspace),
            NSApplicationActivateIgnoringOtherApps=1,
        )
        with self.assertRaises(NativeBlocked):
            platform.activate_existing()
        self.assertEqual(app.calls, 0)
        running.pop()
        app.succeeds = False
        with self.assertRaises(NativeBlocked):
            platform.activate_existing()
        self.assertEqual(app.calls, 1)
        app.succeeds = True
        self.assertTrue(platform.activate_existing())
        self.assertEqual(app.calls, 2)
        self.assertFalse(platform.activate_existing())
        self.assertEqual(app.calls, 2)

    def test_exact_nonmodal_corner_badge_allows_resized_main_only(self):
        layout = replace(Layout.load(self.state), width=787, height=622)
        root, focused, badge = object(), object(), object()
        values = {
            root: {"focused": focused, "windows": [focused, badge], "main": focused},
            focused: {"AXModal": False, "subrole": "AXStandardWindow", "position": (439, 248),
                      "size": (787, 622)},
            badge: {"AXModal": False, "subrole": "AXDialog", "position": (445, 253),
                    "size": (52, 20)},
        }
        attrs = SimpleNamespace(kAXFocusedWindowAttribute="focused", kAXWindowsAttribute="windows",
                                kAXMainWindowAttribute="main", kAXSubroleAttribute="subrole",
                                kAXPositionAttribute="position", kAXSizeAttribute="size")
        app = SimpleNamespace(processIdentifier=lambda: 404)
        platform = MacDesktopPlatform.__new__(MacDesktopPlatform)
        platform.session_guard = lambda purpose: "read-only"
        platform.AppKit = SimpleNamespace(
            NSRunningApplication=SimpleNamespace(runningApplicationsWithBundleIdentifier_=lambda bundle: [app]),
            NSWorkspace=SimpleNamespace(sharedWorkspace=lambda: SimpleNamespace(frontmostApplication=lambda: app)),
        )
        platform.AX = attrs
        attrs.AXUIElementCreateApplication = lambda pid: root
        platform._ax = lambda element, attribute: values[element].get(attribute)
        platform._point = lambda value: value
        platform._size = lambda value: value
        self.assertEqual(platform._window_ax(layout)[2], (439, 248, 787, 622))
        for changed in ({"position": (446, 253)}, {"size": (53, 20)},
                        {"subrole": "AXUnknown"}, {"AXModal": True}):
            with self.subTest(changed=changed):
                original = values[badge].copy()
                values[badge].update(changed)
                with self.assertRaises(NativeBlocked):
                    platform._window_ax(layout)
                values[badge] = original
        with self.assertRaises(NativeBlocked):
            platform._window_ax(replace(layout, kind="chat_only"))

    def test_blank_body_is_not_returned_as_valid_snapshot(self):
        self.platform.blank_body = True
        with self.assertRaises(NativeBlocked):
            self.backend.read()

    def test_missing_or_sidebar_layout_blocks_before_capture(self):
        (self.state / "layout.json").unlink()
        with self.assertRaises(NativeBlocked):
            self.backend.read()
        self.assertFalse(any(call[0] == "capture" for call in self.platform.calls))
        layout = json.loads(json.dumps(LAYOUT))
        layout["regions"]["body"][0] = 0
        (self.state / "layout.json").write_text(json.dumps(layout))
        os.chmod(self.state / "layout.json", 0o600)
        with self.assertRaises(NativeBlocked):
            self.backend.read()
        self.assertFalse(any(call[0] == "capture" for call in self.platform.calls))

    def test_binding_change_blocks_read(self):
        binding = json.loads((self.state / "channel.json").read_text())
        binding["group_title"] = "Other synthetic group"
        (self.state / "channel.json").write_text(json.dumps(binding))
        with self.assertRaises(NativeBlocked):
            self.backend.read()
        self.assertFalse(self.platform.calls)

    def test_nonempty_draft_blocks_before_type_attempt(self):
        self.platform.draft = "existing draft"
        with self.assertRaises(NativeBlocked):
            self.backend.dispatch(FakeOutbox(), Session(TARGET.account_id, "owner", 1), "req123", "req123 reply")
        self.assertFalse(list(self.state.glob("type-*.json")))
        self.assertFalse(any(call[0] == "type" for call in self.platform.calls))

    def test_type_uncertainty_consumes_request_and_no_retry(self):
        self.platform.fail_type = True
        result = self.backend.dispatch(FakeOutbox(), Session(TARGET.account_id, "owner", 1),
                                       "req123", "req123 reply")
        self.assertEqual(result["status"], "indeterminate")
        self.assertEqual(len(list(self.state.glob("type-*.json"))), 1)
        with self.assertRaises(NativeBlocked):
            self.backend.dispatch(FakeOutbox(), Session(TARGET.account_id, "owner", 1),
                                  "req123", "req123 reply")
        self.assertEqual(sum(call[0] == "type" for call in self.platform.calls), 1)

    def test_focused_narrow_caret_allowed_but_other_mark_refused(self):
        self.platform.caret_after_focus = True
        result = self.backend.dispatch(FakeOutbox(), Session(TARGET.account_id, "owner", 1),
                                       "req123", "req123 reply")
        self.assertEqual(result["status"], "indeterminate")
        self.assertEqual(sum(call[0] == "type" for call in self.platform.calls), 1)
        other = self.state / "second"
        other.mkdir(mode=0o700)
        (other / "layout.json").write_text((self.state / "layout.json").read_text())
        (other / "channel.json").write_text((self.state / "channel.json").read_text())
        os.chmod(other / "layout.json", 0o600)
        os.chmod(other / "channel.json", 0o600)
        platform = FakePlatform()
        platform.caret_after_focus = True
        platform.extra_input_mark = True
        guarded = NativeChannelBackend(TARGET, other, platform=platform)
        self.assertEqual(guarded.dispatch(FakeOutbox(), Session(TARGET.account_id, "owner", 1),
                                          "req124", "req124 reply")["status"], "indeterminate")
        self.assertFalse(any(call[0] == "type" for call in platform.calls))

    def test_one_synthetic_click_records_only_local_ui_observation(self):
        outbox = Outbox(self.state / "outbox.sqlite3")
        outbox.authorize_automatic(TARGET.account_id, human_approved=True)
        session = outbox.acquire(TARGET.account_id, "single-desktop-owner")
        self.assertEqual(outbox.prepare(session, intent_id="req123", source_message_id="agent-request:req123",
                                        identity_kind="agent_request", target=TARGET, marker="req123",
                                        text="req123 reply"), "prepared")
        result = self.backend.dispatch(outbox, session, "req123", "req123 reply")
        self.assertEqual(result["status"], "ui_observed")
        self.assertFalse(result["recipient_confirmed"])
        self.assertEqual(outbox.state(TARGET.account_id, "req123"), "ui_observed")
        self.assertEqual(sum(call[0] == "click" for call in self.platform.calls), 2)
        with self.assertRaises(NativeBlocked):
            self.backend.dispatch(outbox, session, "req123", "req123 reply")

    def test_red_warning_records_failed_not_ui_observed(self):
        self.platform.red_after_send = True
        outbox = Outbox(self.state / "outbox.sqlite3")
        outbox.authorize_automatic(TARGET.account_id, human_approved=True)
        session = outbox.acquire(TARGET.account_id, "single-desktop-owner")
        outbox.prepare(session, intent_id="req123", source_message_id="agent-request:req123",
                       identity_kind="agent_request", target=TARGET, marker="req123", text="req123 reply")
        result = self.backend.dispatch(outbox, session, "req123", "req123 reply")
        self.assertEqual(result["status"], "failed")
        self.assertEqual(outbox.state(TARGET.account_id, "req123"), "failed")

    def test_paused_binding_blocks_send_but_allows_read(self):
        binding = json.loads((self.state / "channel.json").read_text())
        binding["paused"] = True
        (self.state / "channel.json").write_text(json.dumps(binding))
        self.assertEqual(self.backend.read()["coverage"], "viewport_only")
        with self.assertRaises(NativeBlocked):
            self.backend.dispatch(FakeOutbox(), Session(TARGET.account_id, "owner", 1),
                                  "req123", "req123 reply")

    def test_missing_lock_marker_allows_read_only(self):
        self.platform.lock_marker = None
        result = self.backend.read()
        self.assertEqual(result["screen_lock_evidence"], "active_session_lock_marker_unavailable")
        with self.assertRaises(NativeBlocked):
            self.backend.dispatch(FakeOutbox(), Session(TARGET.account_id, "owner", 1),
                                  "req123", "req123 reply")
        self.assertFalse(self.platform.calls[-1][0] == "type")

    def test_session_dictionary_absent_lock_flag_is_read_only(self):
        values = {"kCGSessionOnConsoleKey": True, "kCGSessionLoginDoneKey": True}
        platform = MacDesktopPlatform.__new__(MacDesktopPlatform)
        platform.Quartz = SimpleNamespace(
            kCGSessionOnConsoleKey="kCGSessionOnConsoleKey",
            kCGSessionLoginDoneKey="kCGSessionLoginDoneKey",
            CGSessionCopyCurrentDictionary=lambda: values,
        )
        self.assertEqual(platform.session_guard("read"), "active_session_lock_marker_unavailable")
        with self.assertRaises(NativeBlocked):
            platform.session_guard("send")
        values["CGSSessionScreenIsLocked"] = True
        with self.assertRaises(NativeBlocked):
            platform.session_guard("read")
        values["CGSSessionScreenIsLocked"] = False
        self.assertEqual(platform.session_guard("send"), "screen_unlocked_marker_false")

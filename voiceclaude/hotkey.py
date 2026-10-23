"""Single-key tap / double-tap / hold detection by polling GetAsyncKeyState (Windows, no hook, no admin)."""
from __future__ import annotations

import ctypes
import logging
import threading
import time
from typing import Callable

from . import testing

log = logging.getLogger(__name__)

# Keys that do nothing on their own when tapped. Shift toggles the IME's 中/英 (and five taps open Sticky
# Keys), Caps Lock toggles caps, Insert toggles overwrite mode. A lone Alt tap would light up the menu bar (or
# Windows 11's key tips) of the program in front; right Alt is allowed as the mute/send key (the default)
# because the listener masks that, see inject_menu_mask.
VK = {
    "right ctrl": 0xA3, "left ctrl": 0xA2, "right alt": 0xA5,
    "pause": 0x13, "scroll lock": 0x91,
    **{f"f{n}": 0x6F + n for n in range(13, 25)},
}
# Pressing a left/right modifier also sets the generic modifier code; don't count that as "another key".
GENERIC = {0xA2: 0x11, 0xA3: 0x11, 0xA0: 0x10, 0xA1: 0x10, 0xA4: 0x12, 0xA5: 0x12}
MOUSE_BUTTONS = {0x01, 0x02, 0x04, 0x05, 0x06}
ALT_KEYS = {0xA4, 0xA5}
# Browser, volume, media and launch keys (0xA6-0xB7) and 0xFF are never half of a key combination, and headsets or
# keyboard utilities can leave one reported as held for hours (seen: 0xB0, next track), which would spoil every tap.
NOT_A_COMBO = set(range(0xA6, 0xB8)) | {0xFF}
MENU_MASK_VK = 0xE8  # unassigned; a keystroke while Alt is down keeps its release from opening a menu
KEYEVENTF_KEYUP = 0x0002


def inject_menu_mask() -> bool:
    """While an Alt key is held: send one unassigned keystroke so that releasing Alt alone does not activate the
    menu bar or key tips of the program in front (the same trick AutoHotkey uses). Refused under the test runner."""
    if testing.under_test_runner():
        return False
    user32 = ctypes.windll.user32
    user32.keybd_event(MENU_MASK_VK, 0, 0, 0)
    user32.keybd_event(MENU_MASK_VK, 0, KEYEVENTF_KEYUP, 0)
    return True


class TapDetector:
    """Pure state machine: feed(now, key_down, other_key_down) -> None | 'tap' | 'double' | 'hold'.

    A tap is a press-and-release with no other key pressed meanwhile, shorter than `hold_seconds`.
    A second tap within `double_tap_seconds` of the first becomes 'double'. Keeping the key down for
    `hold_seconds` fires 'hold' once, while still held (the emergency stop); releasing after that reports nothing.

    Two flavours of 'tap':
    - delay_tap=False (the main key): 'tap' is reported at once on release, so a double tap arrives as
      'tap' then 'double' and the caller undoes the tap (the main key's tap is cheap to undo).
    - delay_tap=True (the mute/send key): 'tap' is reported only once the double-tap window has passed
      without a second press, so a single tap and a double tap are never confused (costs the window's
      length in latency, fine for a mute toggle).
    """

    def __init__(self, double_tap_seconds: float = 0.4, hold_seconds: float = 0.8, delay_tap: bool = False):
        self.window = double_tap_seconds
        self.hold_seconds = hold_seconds
        self.delay_tap = delay_tap
        self.down = False
        self.spoiled = False
        self.held = False
        self.pressed_at = 0.0
        self.last_tap: float | None = None
        self.pending_tap = False  # delay_tap: a tap waiting for the window to pass

    def feed(self, now: float, down: bool, other_down: bool = False) -> str | None:
        if down and not self.down:
            self.down, self.spoiled, self.held, self.pressed_at = True, other_down, False, now
            return None
        if down:
            if other_down:
                self.spoiled = True
            if not self.spoiled and not self.held and now - self.pressed_at >= self.hold_seconds:
                self.held = True
                self.last_tap, self.pending_tap = None, False
                return "hold"
            return None
        if not self.down:
            if self.pending_tap and now - (self.last_tap or 0.0) > self.window:
                self.pending_tap, self.last_tap = False, None
                return "tap"
            return None
        self.down = False
        if self.spoiled or self.held or now - self.pressed_at >= self.hold_seconds:
            self.pending_tap, self.last_tap = False, None
            return None
        if self.last_tap is not None and now - self.last_tap <= self.window:
            self.last_tap, self.pending_tap = None, False
            return "double"
        self.last_tap = now
        if self.delay_tap:
            self.pending_tap = True
            return None
        return "tap"


class HotkeyListener(threading.Thread):
    def __init__(self, key: str, double_tap_seconds: float, on_tap: Callable[[], None], on_double: Callable[[], None],
                 on_hold: Callable[[], None] | None = None, hold_seconds: float = 0.8, delay_tap: bool = False):
        super().__init__(daemon=True, name=f"hotkey-{key}")
        if key not in VK:
            raise ValueError(f"不认识的按键 {key!r}，可选：{', '.join(VK)}")
        self.vk = VK[key]
        self.mask_menu = self.vk in ALT_KEYS
        self.ignore = {self.vk, GENERIC.get(self.vk, self.vk), MENU_MASK_VK} | MOUSE_BUTTONS | NOT_A_COMBO
        self.others = [vk for vk in range(0x08, 0x100) if vk not in self.ignore]  # keys that make a press a combination
        self.detector = TapDetector(double_tap_seconds, hold_seconds, delay_tap)
        self.on_tap, self.on_double, self.on_hold = on_tap, on_double, on_hold
        self._stop = threading.Event()

    def stop(self):
        self._stop.set()

    def run(self):
        user32 = ctypes.windll.user32
        others = self.others
        while not self._stop.is_set():
            down = bool(user32.GetAsyncKeyState(self.vk) & 0x8000)
            if down and not self.detector.down and self.mask_menu:
                inject_menu_mask()
            other = down and any(user32.GetAsyncKeyState(vk) & 0x8000 for vk in others)
            event = self.detector.feed(time.monotonic(), down, other)
            if event == "tap":
                self._call(self.on_tap)
            elif event == "double":
                self._call(self.on_double)
            elif event == "hold" and self.on_hold:
                self._call(self.on_hold)
            time.sleep(0.01)

    @staticmethod
    def _call(fn):
        try:
            fn()
        except Exception:
            log.exception("hotkey callback failed")

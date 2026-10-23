import unittest

from voiceclaude import hotkey
from voiceclaude.hotkey import TapDetector


def press(det, t0, hold=0.1, other=False):
    events = [det.feed(t0, True, other)]
    events.append(det.feed(t0 + hold / 2, True, other))
    events.append(det.feed(t0 + hold, False, False))
    return [e for e in events if e]


class TapDetectorTests(unittest.TestCase):
    def test_single_tap(self):
        det = TapDetector(0.4)
        self.assertEqual(press(det, 0.0), ["tap"])
        self.assertEqual(det.feed(1.0, False), None)

    def test_double_tap(self):
        det = TapDetector(0.4)
        self.assertEqual(press(det, 0.0), ["tap"])
        self.assertEqual(press(det, 0.3), ["double"])
        # a third tap starts over as a plain tap (no triple tap)
        self.assertEqual(press(det, 0.5), ["tap"])

    def test_slow_second_tap_is_a_new_tap(self):
        det = TapDetector(0.4)
        press(det, 0.0)
        self.assertEqual(press(det, 1.0), ["tap"])

    def test_combo_is_not_a_tap(self):
        det = TapDetector(0.4)
        self.assertEqual(press(det, 0.0, other=True), [])
        det.feed(1.0, True)
        det.feed(1.05, True, True)  # another key pressed while held
        self.assertEqual(det.feed(1.1, False), None)

    def test_long_hold_is_not_a_tap(self):
        det = TapDetector(0.4, hold_seconds=0.8)
        events = [det.feed(t, True) for t in (0.0, 0.5, 0.9)] + [det.feed(1.0, False)]
        self.assertEqual([e for e in events if e], ["hold"])  # fired while held, nothing on release

    def test_hold_fires_once_at_threshold(self):
        det = TapDetector(0.4, hold_seconds=0.8)
        self.assertIsNone(det.feed(0.0, True))
        self.assertIsNone(det.feed(0.5, True))
        self.assertEqual(det.feed(0.8, True), "hold")
        self.assertIsNone(det.feed(1.5, True))   # still held: no repeat
        self.assertIsNone(det.feed(2.0, False))  # release after a hold is not a tap
        self.assertEqual(press(det, 3.0), ["tap"])  # and a later tap is a plain tap, not a double

    def test_hold_with_other_key_is_a_combo(self):
        det = TapDetector(0.4, hold_seconds=0.8)
        det.feed(0.0, True)
        det.feed(0.2, True, True)  # e.g. Ctrl+C with the right Ctrl
        self.assertIsNone(det.feed(1.0, True))
        self.assertIsNone(det.feed(1.1, False))


class DelayedTapTests(unittest.TestCase):
    """The mute/send key: a single tap is reported only after the double-tap window, a double at once."""

    def test_single_tap_reported_after_window(self):
        det = TapDetector(0.4, delay_tap=True)
        self.assertEqual(press(det, 0.0), [])            # nothing yet
        self.assertIsNone(det.feed(0.3, False))           # still inside the window
        self.assertEqual(det.feed(0.6, False), "tap")     # window passed: a single tap
        self.assertIsNone(det.feed(0.7, False))           # reported once

    def test_double_tap_reported_at_once_and_no_stray_tap(self):
        det = TapDetector(0.4, delay_tap=True)
        self.assertEqual(press(det, 0.0), [])
        self.assertEqual(press(det, 0.3), ["double"])
        for t in (0.6, 0.9, 1.5):
            self.assertIsNone(det.feed(t, False))          # the first tap was not reported separately

    def test_hold_cancels_pending_tap(self):
        det = TapDetector(0.4, hold_seconds=0.8, delay_tap=True)
        press(det, 0.0)
        events = [det.feed(t, True) for t in (0.2, 0.6, 1.1)] + [det.feed(1.2, False), det.feed(2.0, False)]
        self.assertEqual([e for e in events if e], ["hold"])



class MenuMaskTests(unittest.TestCase):
    def test_only_an_alt_key_masks_the_menu(self):
        noop = lambda: None
        self.assertTrue(hotkey.HotkeyListener("right alt", 0.4, noop, noop).mask_menu)
        self.assertFalse(hotkey.HotkeyListener("right ctrl", 0.4, noop, noop).mask_menu)

    def test_the_mask_key_never_spoils_a_tap(self):
        noop = lambda: None
        self.assertIn(hotkey.MENU_MASK_VK, hotkey.HotkeyListener("right alt", 0.4, noop, noop).ignore)

    def test_no_keystroke_is_sent_under_the_test_runner(self):
        self.assertFalse(hotkey.inject_menu_mask())

    def test_media_and_browser_keys_never_spoil_a_tap(self):
        # a headset or keyboard utility can leave a media key reported as held (seen: VK 0xB0, next track)
        noop = lambda: None
        others = hotkey.HotkeyListener("right ctrl", 0.4, noop, noop).others
        for vk in (0xB0, 0xB3, 0xAD, 0xAF, 0xA6, 0xAC, 0xB7, hotkey.MENU_MASK_VK, 0xFF):
            self.assertNotIn(vk, others, hex(vk))
        for vk in (0xA0, 0x10, 0x41, 0x5B, 0x12):  # shift, a letter, Win, Alt: a real combination
            self.assertIn(vk, others, hex(vk))

if __name__ == "__main__":
    unittest.main()

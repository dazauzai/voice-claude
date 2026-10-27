"""A configured device that is not plugged in (Bluetooth headphones switched off): startup falls back to the system
default and says so; choosing a device by name later still reports a name that matches nothing. No real device is
opened: the device list is faked."""
import unittest
from unittest import mock

from voiceclaude import audio
from voiceclaude.app import missing_note

DEVICES = [
    {"name": "Microsoft 声音映射器 - Output", "hostapi": 0, "max_input_channels": 0, "max_output_channels": 2},
    {"name": "扬声器 (Realtek(R) Audio)", "hostapi": 0, "max_input_channels": 0, "max_output_channels": 2},
    {"name": "麦克风阵列 (Realtek(R) Audio)", "hostapi": 0, "max_input_channels": 2, "max_output_channels": 0},
]


class MissingDeviceTests(unittest.TestCase):
    def setUp(self):
        p = mock.patch.object(audio.sd, "query_devices", lambda *a: DEVICES[a[0]] if a else DEVICES)
        p.start()
        self.addCleanup(p.stop)

    def test_a_name_that_matches_is_found(self):
        self.assertEqual(audio.find_device("realtek", output=True), 1)

    def test_a_missing_name_raises_without_fallback(self):
        with self.assertRaises(ValueError):
            audio.find_device("耳机 (Buds)", output=True)

    def test_a_missing_name_gives_the_system_default_with_fallback(self):
        with self.assertLogs("voiceclaude.audio", "WARNING"):
            self.assertIsNone(audio.find_device("耳机 (Buds)", output=True, fallback=True))
        with self.assertLogs("voiceclaude.audio", "WARNING"):
            self.assertIsNone(audio.find_device("耳机 (Buds)", output=False, fallback=True))

    def test_an_empty_name_is_the_default_and_not_missing(self):
        self.assertIsNone(audio.find_device("", output=True, fallback=True))

    def test_the_startup_note_names_the_missing_device(self):
        self.assertEqual(missing_note("耳机 (Buds)"), "（没找到「耳机 (Buds)」，用系统默认）")
        self.assertEqual(missing_note(""), "")


if __name__ == "__main__":
    unittest.main()

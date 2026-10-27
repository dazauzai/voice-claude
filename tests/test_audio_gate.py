"""The microphone's level gate: sound the voice detector takes for speech only counts when it is clearly above the
room's noise floor, so a fan, a keyboard or a distant voice does not send a whole message. The detector itself is
replaced here; no model or device is used."""
import unittest
from unittest import mock

import numpy as np

from voiceclaude import audio


def chunk(level: float) -> np.ndarray:
    return np.full(audio.BLOCK, level, np.float32)  # RMS == level


class LevelGateTests(unittest.TestCase):
    def make(self, min_level=0.003, level_ratio=3.0, quiet_blocks=30, quiet=0.002):
        with mock.patch.object(audio.sherpa_onnx, "VoiceActivityDetector", mock.Mock()):
            ep = audio.Endpointer(0.5, min_speech=0.0, min_level=min_level, level_ratio=level_ratio)
        ep._speech = lambda c: self.says_speech
        self.says_speech = False
        for _ in range(quiet_blocks):  # the room: quiet, the detector hears nothing
            ep.feed(chunk(quiet))
        return ep

    def test_a_noise_the_detector_takes_for_speech_near_the_floor_does_not_start(self):
        ep = self.make()
        self.says_speech = True
        for _ in range(10):
            self.assertIsNone(ep.feed(chunk(0.004)))  # twice the floor: not three times
        self.assertFalse(ep.active)

    def test_a_voice_well_above_the_floor_starts_and_ends_normally(self):
        ep = self.make()
        self.says_speech = True
        for _ in range(10):
            ep.feed(chunk(0.05))
        self.assertTrue(ep.active)
        for _ in range(6):
            ep.feed(chunk(0.002))  # the quiet tail the detector still calls speech counts once started ...
        self.assertEqual(ep.speech_blocks, 16)
        self.assertEqual(ep.silent_blocks, 0)  # ... so a short word is not cut down below min_speech
        self.says_speech = False
        got = [u for u in (ep.feed(chunk(0.002)) for _ in range(40)) if u is not None]
        self.assertEqual(len(got), 1)
        self.assertFalse(ep.active)

    def test_a_quiet_voice_in_a_silent_room_still_counts(self):
        ep = self.make(quiet=0.0005)  # a very quiet room: the floor is tiny ...
        self.says_speech = True
        for _ in range(10):
            ep.feed(chunk(0.004))  # ... so this is far above it, and above the fixed minimum
        self.assertTrue(ep.active)

    def test_below_the_fixed_minimum_never_counts(self):
        ep = self.make(quiet=0.0)
        self.says_speech = True
        for _ in range(10):
            self.assertIsNone(ep.feed(chunk(0.002)))
        self.assertFalse(ep.active)

    def test_the_floor_follows_the_room(self):
        ep = self.make(quiet=0.002)
        low = ep.floor
        self.says_speech = False
        for _ in range(200):
            ep.feed(chunk(0.02))  # the fan came on
        self.assertGreater(ep.floor, low * 5)
        self.says_speech = True
        for _ in range(10):
            ep.feed(chunk(0.03))  # a voice only slightly above the fan: not counted now
        self.assertFalse(ep.active)

    def test_the_room_is_learned_while_nobody_listens(self):
        ep = self.make(quiet_blocks=0)
        for _ in range(200):
            ep.observe(chunk(0.02))  # the fan, heard while the window was idle
        self.assertGreater(ep.floor, 0.015)
        ep.observe(chunk(0.5))  # a bang is not the room
        self.assertLess(ep.floor, 0.03)

    def test_the_gate_can_be_switched_off(self):
        ep = self.make(min_level=0.0, level_ratio=0.0)
        self.says_speech = True
        ep.feed(chunk(0.0001))
        self.assertTrue(ep.active)


if __name__ == "__main__":
    unittest.main()

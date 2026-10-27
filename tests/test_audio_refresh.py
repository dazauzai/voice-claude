"""Headphones connected after startup: the device lists come from a short separate process (PortAudio in this one
lists devices only once), and picking a device this process does not know yet restarts PortAudio with both streams
closed, then opens them again. No real device or process is used."""
import json
import queue
import subprocess
import threading
import types
import unittest
from unittest import mock

from voiceclaude import app as appmod, audio

NOW = [
    {"name": "扬声器 (Realtek(R) Audio)", "hostapi": 0, "max_input_channels": 0, "max_output_channels": 2},
    {"name": "耳机 (Buds)", "hostapi": 0, "max_input_channels": 0, "max_output_channels": 2},
    {"name": "耳机 (Buds)", "hostapi": 1, "max_input_channels": 0, "max_output_channels": 2},  # another host API
    {"name": "Microsoft 声音映射器 - Output", "hostapi": 0, "max_input_channels": 0, "max_output_channels": 2},
    {"name": "麦克风 (USB MIC)", "hostapi": 0, "max_input_channels": 1, "max_output_channels": 0},
]


class CurrentDevicesTests(unittest.TestCase):
    def test_the_list_comes_from_a_separate_process(self):
        done = subprocess.CompletedProcess([], 0, stdout=json.dumps(NOW, ensure_ascii=False))
        with mock.patch.object(subprocess, "run", return_value=done) as run:
            self.assertEqual(audio.current_devices(True), ["扬声器 (Realtek(R) Audio)", "耳机 (Buds)"])
            self.assertEqual(audio.current_devices(False), ["麦克风 (USB MIC)"])
        self.assertIn("-I", run.call_args.args[0])

    def test_a_failed_listing_falls_back_to_this_process(self):
        with mock.patch.object(subprocess, "run", side_effect=subprocess.TimeoutExpired("x", 10)), \
                mock.patch.object(audio, "list_devices", return_value=["扬声器 (Realtek(R) Audio)"]), \
                self.assertLogs("voiceclaude.audio", "WARNING"):
            self.assertEqual(audio.current_devices(True), ["扬声器 (Realtek(R) Audio)"])


class FakePlayer:
    def __init__(self, name="", fallback=False, known=("扬声器",)):
        if name and not any(k in name for k in known):
            raise ValueError("找不到")
        self.name, self.device, self.closed = name or "扬声器 (Realtek(R) Audio)", 1, False
        self.thread = threading.Thread(target=lambda: None)
        self.thread.start()

    def set_device(self, name):
        FakePlayer.__init__(self, name)

    def close(self):
        self.closed = True


class FakeMic:
    def __init__(self, name, put, fallback=False):
        self.name, self.started, self.stopped = name or "麦克风 (USB MIC)", False, False

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True


def fake_app():
    a = types.SimpleNamespace(lock=threading.RLock(), ui_q=queue.Queue(), audio_q=queue.Queue(), notes=[],
                              speaker=mock.Mock(), endpointer=mock.Mock())
    a.player, a.mic = FakePlayer(), FakeMic("麦克风 (USB MIC)", None)
    a._note = a.notes.append
    for name in ("_set_speaker", "_set_mic", "_restart_audio"):
        setattr(a, name, getattr(appmod.App, name).__get__(a))
    return a


class PickingANewDeviceTests(unittest.TestCase):
    def setUp(self):
        for target, value in ((audio, "Player"), (audio, "Microphone"), (audio, "restart_portaudio"),
                              (appmod.config, "save")):
            p = mock.patch.object(target, value)
            self.addCleanup(p.stop)
            setattr(self, value, p.start())

    def test_a_known_speaker_switches_without_a_restart(self):
        a = fake_app()
        a._set_speaker("扬声器 (Realtek(R) Audio)")
        self.restart_portaudio.assert_not_called()
        self.assertEqual(a.notes, ["扬声器已切换到 扬声器 (Realtek(R) Audio)"])

    def test_a_speaker_connected_after_startup_restarts_portaudio_and_reopens_both(self):
        a = fake_app()
        old_player, old_mic = a.player, a.mic
        self.Player.return_value = types.SimpleNamespace(name="耳机 (Buds)", device=2)
        self.Microphone.return_value = FakeMic("麦克风 (USB MIC)", None)
        a._set_speaker("耳机 (Buds)")
        self.assertTrue(old_player.closed)
        self.assertTrue(old_mic.stopped)
        self.restart_portaudio.assert_called_once()
        self.Player.assert_called_once_with("耳机 (Buds)", fallback=True)
        self.assertTrue(a.mic.started)
        a.speaker.replace_player.assert_called_once_with(a.player)
        self.assertEqual(a.notes, ["扬声器已切换到 耳机 (Buds)"])
        appmod.config.save.assert_called_once_with("audio", "speaker", "耳机 (Buds)")

    def test_a_microphone_connected_after_startup_restarts_too(self):
        a = fake_app()
        self.Microphone.side_effect = [ValueError("找不到"), FakeMic("麦克风 (新)", None)]
        self.Player.return_value = types.SimpleNamespace(name="扬声器 (Realtek(R) Audio)", device=1)
        a._set_mic("麦克风 (新)")
        self.restart_portaudio.assert_called_once()
        self.assertEqual(a.mic.name, "麦克风 (新)")
        self.assertTrue(a.mic.started)
        self.assertEqual(a.notes, ["麦克风已切换到 麦克风 (新)"])


if __name__ == "__main__":
    unittest.main()

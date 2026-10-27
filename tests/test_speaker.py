"""Speaker.stuck(): something waiting to be read out that has not moved for a while (a sound card that stopped taking
audio) is told apart from speech still being read; time asleep does not count. A stuck player can be given up for a
new one. No model is loaded and no sound device is opened."""
import os
import threading
import time
import types
import unittest
from unittest import mock

import numpy as np

from voiceclaude import audio, tts


class StuckTests(unittest.TestCase):
    def setUp(self):
        self.clock = [1000.0]
        self.release = threading.Event()
        self.generated = threading.Event()

        def generate(text, sid=0, speed=1.0):
            self.release.wait(5)
            self.generated.set()
            return types.SimpleNamespace(samples=np.ones(2400, np.float32))

        self.player = mock.Mock(progress_at=0.0)
        patches = [mock.patch.object(tts, "load_tts", lambda: types.SimpleNamespace(generate=generate)),
                   mock.patch.object(tts, "awake_clock", lambda: self.clock[0])]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(self.release.set)
        self.speaker = tts.Speaker(0, 1.0, self.player, on_idle=lambda: None)

    def test_nothing_to_say_is_not_stuck(self):
        self.assertFalse(self.speaker.stuck())

    def test_a_sentence_that_never_moves_is_stuck(self):
        s = self.speaker
        s.say("你好。")
        self.clock[0] += 30
        self.assertFalse(s.stuck())  # still being synthesised
        self.clock[0] += 31
        self.assertTrue(s.stuck())  # synthesis has not come back in a minute
        self.release.set()
        self.assertTrue(self.generated.wait(5))
        t = time.time()
        while not self.player.play.called and time.time() - t < 5:
            time.sleep(0.01)
        self.assertFalse(s.stuck())  # it moved: handed to the player
        self.clock[0] += 120
        self.assertTrue(s.stuck())  # the player never took a chunk
        self.player.progress_at = self.clock[0] - 5
        self.assertFalse(s.stuck())  # the player is writing
        done = self.player.play.call_args.args[2]
        done()
        self.clock[0] += 600
        self.assertTrue(s.idle())
        self.assertFalse(s.stuck())

    def test_saying_after_a_quiet_hour_starts_the_clock_again(self):
        s = self.speaker
        self.clock[0] += 3600
        s.say("还在吗？")
        self.assertFalse(s.stuck())


@unittest.skipUnless(os.name == "nt", "QueryUnbiasedInterruptTime")
class AwakeClockTests(unittest.TestCase):
    def test_it_counts_awake_seconds(self):
        # the PC does not sleep during a test, so awake time runs with time.monotonic here; asleep it stops
        a0, m0 = audio.awake_clock(), time.monotonic()
        time.sleep(0.2)
        a1, m1 = audio.awake_clock(), time.monotonic()
        self.assertAlmostEqual(a1 - a0, m1 - m0, delta=0.05)
        self.assertIsNot(audio._unbiased_ticks, None)  # not the time.monotonic fallback


class FakeStream:
    """An output stream whose writes block while `wedged` is clear."""

    def __init__(self, wedged: threading.Event):
        self.wedged = wedged
        self.entered = threading.Event()
        self.written = 0
        self.closed = False

    def start(self):
        pass

    def write(self, chunk):
        self.entered.set()
        self.wedged.wait()
        self.written += 1

    def close(self):
        self.closed = True


class ReplacePlayerTests(unittest.TestCase):
    def setUp(self):
        self.streams = []
        self.free = threading.Event()  # set: the first stream's writes return; later streams always write
        self.addCleanup(self.free.set)
        flowing = threading.Event()
        flowing.set()

        def output_stream(**kw):
            s = FakeStream(self.free if not self.streams else flowing)
            self.streams.append(s)
            return s

        patches = [mock.patch.object(audio, "sd", types.SimpleNamespace(OutputStream=output_stream)),
                   mock.patch.object(audio, "find_device", lambda *a, **k: 0),
                   mock.patch.object(audio, "device_name", lambda *a, **k: "Fake speaker"),
                   mock.patch.object(tts, "load_tts", lambda: types.SimpleNamespace(
                       generate=lambda text, sid=0, speed=1.0: types.SimpleNamespace(
                           samples=np.ones(2400, np.float32))))]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def wait(self, cond, secs=5.0):
        t = time.time()
        while not cond() and time.time() - t < secs:
            time.sleep(0.01)
        return cond()

    def test_abandoning_counts_the_stuck_sentence_and_the_queue_as_done(self):
        p = audio.Player("")
        done = []
        p.play(np.ones(2400, np.float32), 0, lambda: done.append("stuck"))
        self.assertTrue(self.wait(lambda: self.streams and self.streams[0].entered.is_set()))
        p.play(np.ones(2400, np.float32), 0, lambda: done.append("queued"))
        p.abandon()
        self.assertEqual(sorted(done), ["queued", "stuck"])
        self.free.set()  # the write comes back after all: nothing is counted twice, the thread ends
        p.thread.join(5)
        self.assertFalse(p.thread.is_alive())
        self.assertEqual(sorted(done), ["queued", "stuck"])
        self.assertTrue(self.streams[0].closed)

    def test_an_idle_player_given_up_ends_its_thread(self):
        self.free.set()
        p = audio.Player("")
        p.abandon()
        p.thread.join(5)
        self.assertFalse(p.thread.is_alive())

    def test_a_new_player_reads_out_after_a_stuck_one(self):
        idle = []
        old = audio.Player("")
        s = tts.Speaker(0, 1.0, old, on_idle=lambda: idle.append(1))
        s.say("第一句。")
        self.assertTrue(self.wait(lambda: self.streams and self.streams[0].entered.is_set()))
        self.assertFalse(s.idle())
        s.stop()
        self.assertFalse(s.idle())  # stop() cannot reach the sentence stuck in the write
        new = audio.Player("")
        s.replace_player(new)
        self.assertTrue(s.idle())  # the stuck sentence no longer counts
        self.assertIs(s.player, new)
        s.say("第二句。")
        self.assertTrue(self.wait(lambda: s.idle()))
        self.assertTrue(self.wait(lambda: len(self.streams) == 2 and self.streams[1].written > 0))  # the new one
        self.free.set()  # the old write returns at last: nothing is counted twice
        old.thread.join(5)
        self.assertTrue(s.idle())
        with s.lock:
            self.assertEqual(s.pending, 0)


if __name__ == "__main__":
    unittest.main()

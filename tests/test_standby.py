"""Standby and the wake word: keyword lines for the spotter, the standby loop's decisions, the Startup-folder switch
(refused under tests), named mutexes, and the window side (listen when woken, the wake word while idle, back to
standby when idle). No microphone, no window process is started."""
import os
import time
import tkinter as tk
import unittest
from unittest import mock

import numpy as np

from voiceclaude import app as appmod, config, standby, wake


@unittest.skipUnless(wake.model_ready(), "wake-word model not downloaded")
class KeywordTests(unittest.TestCase):
    def test_hi_chat_has_its_pronunciations(self):
        lines = wake.keyword_lines("hi chat")
        self.assertEqual(lines[0], "HH AY1 CH AE1 T :1.0 #0.15 @HI_CHAT")
        self.assertGreaterEqual(len(lines), 4)
        self.assertTrue(all(l.endswith("@HI_CHAT") for l in lines))

    def test_another_english_wake_word(self):
        self.assertEqual(wake.keyword_lines("Hey chat!", 0.2)[0], "HH EY1 CH AE1 T :1.0 #0.2 @HEY_CHAT")

    def test_words_the_model_cannot_spell(self):
        with self.assertRaises(wake.WakeWordError):
            wake.keyword_lines("hi zxqvw")
        with self.assertRaises(wake.WakeWordError):
            wake.keyword_lines("   ")

    def test_chinese_needs_pypinyin(self):
        try:
            import pypinyin  # noqa: F401
        except ImportError:
            with self.assertRaises(wake.WakeWordError):
                wake.keyword_lines("你好小克")
        else:
            self.assertTrue(wake.keyword_lines("你好小克")[0].endswith("@你好小克"))

    def test_silence_does_not_wake(self):
        sp = wake.Spotter()
        quiet = np.zeros(16000 * 3, dtype=np.float32)
        self.assertFalse(any(sp.feed(quiet[i:i + 512]) for i in range(0, len(quiet), 512)))


class StandbyLoopTests(unittest.TestCase):
    def test_the_wake_word_starts_the_window(self):
        launched = []
        sp = mock.Mock()
        sp.feed.side_effect = [False, False, True]
        sb = standby.Standby(sp, launch=lambda: launched.append(1), window_is_open=lambda: False)
        self.assertEqual([sb.heard(np.zeros(512, np.float32)) for _ in range(3)], [False, False, True])
        self.assertEqual(launched, [1])
        self.assertGreater(sb.woke_at, 0)

    def test_the_window_is_started_woken_and_from_standby(self):
        cmd = standby.window_command()
        self.assertEqual(cmd[1:], ["-X", "utf8", "-m", "voiceclaude", "--woken", "--standby"])
        self.assertEqual(standby.standby_command()[-2:], ["-m", "voiceclaude.standby"])

    def test_nothing_real_starts_under_tests(self):
        # the guard must refuse. Should it ever not, what is below it takes the hit: a temp folder for the Startup
        # shortcut and recorders for starting a process, the mutex and the listener
        import shutil
        import tempfile
        import types
        from pathlib import Path
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        did = []
        fake_subprocess = types.SimpleNamespace(Popen=lambda *a, **k: did.append("process"), DEVNULL=-3)
        with mock.patch.object(standby, "startup_link", lambda: tmp / standby.STARTUP_LINK), \
                mock.patch.object(standby, "subprocess", fake_subprocess), \
                mock.patch.object(standby, "hold_mutex", lambda *a, **k: did.append("mutex")), \
                mock.patch.object(standby, "_listen_loop", lambda: did.append("listen")), \
                mock.patch.object(standby.paths, "ensure", lambda: None), \
                mock.patch.object(standby.logging, "basicConfig", lambda **k: None), \
                mock.patch.object(standby.logging, "FileHandler", lambda *a, **k: mock.Mock()):
            for fn in (lambda: standby.set_autostart(True), standby.start_standby, standby.run):
                with self.assertRaises(RuntimeError):
                    fn()
        self.assertEqual(did, [])
        self.assertEqual(list(tmp.iterdir()), [])

    def test_a_link_to_another_copy_is_not_on(self):
        with mock.patch.object(standby, "startup_link") as link, mock.patch.object(standby, "_link_target") as target:
            link.return_value = mock.Mock(exists=lambda: True)
            target.return_value = (r"C:\gone\pythonw.exe", r"C:\gone")
            self.assertFalse(standby.autostart_enabled())  # the folder moved: the old link is dead

    def test_the_startup_link_lives_in_the_users_startup_folder(self):
        with mock.patch.dict(os.environ, {"APPDATA": r"C:\Users\x\AppData\Roaming"}):
            p = standby.startup_link()
        self.assertEqual(p.parent.name, "Startup")
        self.assertEqual(p.name, standby.STARTUP_LINK)

    @unittest.skipUnless(os.name == "nt", "Windows mutexes")
    def test_mutexes(self):
        name = f"Local\\voice-claude-test-{os.getpid()}"
        self.assertFalse(standby.mutex_exists(name))
        h = standby.hold_mutex(name, exclusive=True)
        self.assertIsNotNone(h)
        self.assertTrue(standby.mutex_exists(name))
        self.assertIsNone(standby.hold_mutex(name, exclusive=True))  # a second standby gives up


class ListenLoopTests(unittest.TestCase):
    """standby's own loop with fakes for the clock, the microphone, the spotter and the window: which steps run in
    which order. No sound device is opened and no window is started."""

    def run_loop(self, opens_before_stop=2, fail_first_open=False, fail_starts=0):
        import queue
        import shutil
        import tempfile
        import types
        from pathlib import Path
        from voiceclaude import audio

        clock, events, woke = [1000.0], [], []
        home = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, home, ignore_errors=True)

        def sleep(s):
            clock[0] += s
            if clock[0] > 1000 + 3600 and not (home / standby.STOP_FILE).exists():  # a regression fails, not hangs
                events.append(("still looping an hour later",))
                (home / standby.STOP_FILE).touch()

        class FakeQueue:
            def __init__(self, maxsize=0):
                pass

            def put_nowait(self, c):
                pass

            def empty(self):
                return True

            def get(self, timeout=None):
                sleep(0.032)
                return np.zeros(512, np.float32)

        class FakeMic:
            def __init__(self, name, put, fallback=False):
                if fail_first_open and not any(e[0] == "failed" for e in events):
                    events.append(("failed",))
                    raise OSError("no such device")
                events.append(("open", name))
                self.name = name
                if sum(e[0] == "open" for e in events) >= opens_before_stop:
                    (home / standby.STOP_FILE).touch()

            def start(self):
                pass

            def stop(self):
                events.append(("stop",))

        fed = [0]

        class FakeSpotter:
            def __init__(self, *a):
                pass

            def reset(self):
                pass

            def feed(self, chunk):
                fed[0] += 1
                return fed[0] == 3  # the wake word on the third chunk

        def launch(argv):
            woke.append(clock[0])
            events.append(("window",))

        def window_up(name):  # the window stays up for 40 s after the wake word
            return name == standby.WINDOW_MUTEX and bool(woke) and clock[0] < woke[-1] + 40

        class PortAudioError(Exception):  # what sounddevice raises when PortAudio will not start at import
            pass

        def sound_modules():
            if sum(e[0] == "no sound" for e in events) < fail_starts:
                events.append(("no sound", round(clock[0] - 1000)))
                raise PortAudioError("Error initializing PortAudio: Internal PortAudio error")
            return audio, wake

        cfg = types.SimpleNamespace(microphone="Headset", wake_keyword="", wake_threshold=0)
        with mock.patch.object(standby, "time", types.SimpleNamespace(sleep=sleep, monotonic=lambda: clock[0])), \
                mock.patch.object(standby, "queue", types.SimpleNamespace(Queue=FakeQueue, Full=queue.Full,
                                                                          Empty=queue.Empty)), \
                mock.patch.object(standby.paths, "HOME_DIR", home), \
                mock.patch.object(standby.config, "load", lambda: cfg), \
                mock.patch.object(standby, "mutex_exists", window_up), \
                mock.patch.object(standby, "_spawn", launch), \
                mock.patch.object(standby, "rescan_devices", lambda: events.append(("rescan",))), \
                mock.patch.object(standby, "_sound_modules", sound_modules), \
                mock.patch.object(audio, "Microphone", FakeMic), mock.patch.object(audio, "tone", lambda k: None), \
                mock.patch.object(wake, "model_ready", lambda: True), mock.patch.object(wake, "Spotter", FakeSpotter):
            standby._listen_loop()
        return events

    def test_the_devices_are_listed_again_before_every_open(self):
        # Windows renumbers microphones when one is plugged in or out while a window has the microphone; a stale
        # list would open another device under the chosen one's name
        events = self.run_loop()
        self.assertEqual(events, [("rescan",), ("open", "Headset"), ("window",), ("stop",),
                                  ("rescan",), ("open", "Headset"), ("stop",)])

    def test_a_failed_open_lists_the_devices_again_and_retries(self):
        events = self.run_loop(opens_before_stop=1, fail_first_open=True)
        self.assertEqual(events, [("rescan",), ("failed",), ("rescan",), ("open", "Headset"), ("stop",)])

    def test_sound_that_will_not_start_at_login_is_tried_again(self):
        # sounddevice starts PortAudio when it is imported; one failure there must not end standby
        events = self.run_loop(opens_before_stop=1, fail_starts=1)
        self.assertEqual(events, [("no sound", 0), ("rescan",), ("open", "Headset"), ("stop",)])

    def test_sound_that_keeps_failing_is_tried_less_and_less_often(self):
        events = self.run_loop(opens_before_stop=1, fail_starts=7)
        tries = [e[1] for e in events if e[0] == "no sound"]
        self.assertEqual(tries, [0, 10, 30, 70, 150, 310, 610])  # 10 s, doubling, at most 5 min apart
        self.assertEqual(events[-3:], [("rescan",), ("open", "Headset"), ("stop",)])

    def test_a_failed_start_does_not_keep_the_dead_module(self):
        # sounddevice registers its exit handler before it starts PortAudio; a failed import leaves that handler
        # (and through it the whole dead module) in the exit table, so every retry would leak one
        ns = {"__name__": "sounddevice"}
        exec("import atexit\n"
             "def _exit_handler():\n    pass\n"
             "atexit.register(_exit_handler)\n"
             "class PortAudioError(Exception):\n    pass\n"
             "def _initialize():\n    raise PortAudioError('Internal PortAudio error')\n", ns)
        import gc
        import weakref
        dead = weakref.ref(ns["_exit_handler"])
        try:
            ns["_initialize"]()
        except Exception as e:
            standby._forget_failed_sounddevice(e)
        del ns
        gc.collect()
        self.assertIsNone(dead())  # nothing holds the failed module's handler any more


class RescanTests(unittest.TestCase):
    """rescan_devices() with a fake sounddevice module: no PortAudio is loaded."""

    def fake_sd(self, fail_init=0):
        import types
        sd = types.ModuleType("sounddevice")
        sd.calls, sd._initialized, fails = [], 1, [fail_init]

        def stop():
            sd.calls.append("stop")

        def terminate():
            sd.calls.append("terminate")
            if not sd._initialized:
                raise RuntimeError("Error terminating PortAudio: PortAudio not initialized")
            sd._initialized -= 1

        def initialize():
            sd.calls.append("initialize")
            if fails[0]:
                fails[0] -= 1
                raise RuntimeError("Error initializing PortAudio: Internal PortAudio error")
            sd._initialized += 1

        sd.stop, sd._terminate, sd._initialize = stop, terminate, initialize
        return sd

    def test_the_finished_tone_is_closed_before_the_list_is_rebuilt(self):
        # PortAudio's terminate frees any stream still open (the wake word's beep) behind sounddevice's back
        sd = self.fake_sd()
        with mock.patch.dict("sys.modules", {"sounddevice": sd}):
            standby.rescan_devices()
        self.assertEqual(sd.calls, ["stop", "terminate", "initialize"])
        self.assertEqual(sd._initialized, 1)

    def test_a_failed_start_is_tried_again_next_time(self):
        sd = self.fake_sd(fail_init=1)
        with mock.patch.dict("sys.modules", {"sounddevice": sd}), self.assertLogs(standby.log, "WARNING"):
            standby.rescan_devices()
        self.assertEqual(sd._initialized, 0)
        with mock.patch.dict("sys.modules", {"sounddevice": sd}):
            standby.rescan_devices()
        self.assertEqual(sd.calls[3:], ["stop", "initialize"])  # nothing to terminate: just start again
        self.assertEqual(sd._initialized, 1)


class WindowSideTests(unittest.TestCase):
    def setUp(self):
        self.new_players = []

        def new_player(*a, **k):  # never a real sound device
            p = mock.Mock(name=f"new player {len(self.new_players) + 1}", device=7, progress_at=0.0)
            self.new_players.append(p)
            return p

        self.patches = [mock.patch.object(config, "save", lambda *a, **k: None),
                        mock.patch.object(appmod.audio, "tone", lambda *a, **k: None),
                        mock.patch.object(appmod.audio, "Player", new_player),
                        mock.patch.object(appmod.audio, "OUTPUT_DEVICE", None)]
        for p in self.patches:
            p.start()

    def make(self, **kw):
        try:
            a = appmod.App(config.Config(), **kw)
            a._build_window()
        except tk.TclError as e:  # no display
            self.skipTest(f"no display: {e}")
        a.root.withdraw()
        a.ready = True
        a.client = mock.Mock(connected=True, active="p-1")
        a.speaker, a.player = mock.Mock(), mock.Mock()
        a.endpointer = mock.Mock(active=False)
        self.app = a
        return a

    def tearDown(self):
        for p in self.patches:
            p.stop()
        try:
            self.app.root.destroy()
        except Exception:
            pass

    def test_woken_listens_once_it_is_quiet_and_does_not_greet(self):
        a = self.make(woken=True)
        a._greet()
        a.speaker.idle.return_value = False  # the service's greeting is being spoken
        a._tick_wake(time.monotonic())
        self.assertEqual(a.state, appmod.IDLE)
        a.speaker.idle.return_value = True
        a._tick_wake(time.monotonic())
        self.assertEqual(a.state, appmod.LISTENING)
        a.speaker.say.assert_not_called()

    def test_a_wake_nobody_speaks_into_gives_up(self):
        a = self.make()
        with a.lock:
            a._wake_listen()
        now = time.monotonic()
        a._tick_wake(now + 3)
        self.assertEqual(a.state, appmod.LISTENING)
        a._tick_wake(now + a.WAKE_LISTEN_SECONDS + 1)
        self.assertEqual(a.state, appmod.IDLE)  # a false wake does not keep the microphone listening

    def test_talking_after_the_wake_word_is_not_cut_off(self):
        a = self.make()
        with a.lock:
            a._wake_listen()
        a.endpointer.active = True  # you started talking
        a._tick_wake(time.monotonic() + 30)
        self.assertEqual(a.state, appmod.LISTENING)

    def test_started_by_hand_greets(self):
        a = self.make()
        a._greet()
        self.assertEqual(a.state, appmod.IDLE)
        a.speaker.say.assert_called_with("准备好了。")

    def test_the_wake_word_while_idle_starts_listening(self):
        a = self.make()
        a.spotter = mock.Mock()
        a.spotter.feed.side_effect = [False, True]
        with a.lock:
            a._idle_chunk(np.zeros(512, np.float32))
            self.assertEqual(a.state, appmod.IDLE)
            a._idle_chunk(np.zeros(512, np.float32))
        self.assertEqual(a.state, appmod.LISTENING)

    def test_back_to_standby_after_the_idle_minutes(self):
        a = self.make(from_standby=True)
        a.cfg.wake_idle_minutes = 5
        now = time.monotonic()
        a._idle_since = now - 4 * 60
        a._maybe_back_to_standby(now)
        self.assertFalse(a._quit_requested)  # 4 minutes: not yet
        a._idle_since = now - 6 * 60
        a._maybe_back_to_standby(now)
        self.assertTrue(a._quit_requested)
        t = time.time()
        while not a.client.shutdown.called and time.time() - t < 3:
            time.sleep(0.01)
        a.client.shutdown.assert_called_with(jobs="keep", lines_confirmed=True)  # a timer never stops a job

    def test_more_that_keeps_the_window(self):
        a = self.make(from_standby=True)
        a.cfg.wake_idle_minutes = 5
        now = time.monotonic()
        cases = {
            "muted": lambda: setattr(a, "muted", True),
            "unread": lambda: setattr(a, "_lines_snapshot", ({"p-1": {"state": "idle", "unread": 2}}, "p-1", "Atlas")),
            "relay": lambda: a._relays_waiting.add("s-1"),
            "settings": lambda: setattr(a, "page", "settings"),
        }
        for name, make_busy in cases.items():
            a.muted, a.page = False, "transcript"
            a._lines_snapshot, a._relays_waiting = ({}, "", ""), set()
            make_busy()
            a._idle_since = now - 60 * 60
            self.assertFalse(a._maybe_back_to_standby(now), name)
            self.assertFalse(a._quit_requested, name)

    def test_a_waiting_line_does_not_keep_the_window(self):
        a = self.make(from_standby=True)
        a.cfg.wake_idle_minutes = 5
        a._lines_snapshot = ({"p-1": {"state": "waiting", "unread": 0}}, "p-1", "Atlas")  # it asked you something
        a._idle_since = time.monotonic() - 60 * 60
        a._maybe_back_to_standby(time.monotonic())
        self.assertTrue(a._quit_requested)

    def test_a_gone_service_closes_without_a_dialog(self):
        a = self.make(from_standby=True)
        a.cfg.wake_idle_minutes = 5
        a.daemon_gone = "后台服务已关闭"
        a._idle_since = time.monotonic() - 60 * 60
        with mock.patch.object(a, "quit") as q, mock.patch.object(appmod, "ask_ok") as dialog:
            self.assertTrue(a._maybe_back_to_standby(time.monotonic()))
        q.assert_called_once()
        dialog.assert_not_called()
        a.client.shutdown.assert_not_called()

    def test_using_the_window_restarts_the_idle_clock(self):
        a = self.make(from_standby=True)
        a._idle_since = 0.0
        a._touched()
        self.assertGreater(a._idle_since, time.monotonic() - 1)

    def test_a_cough_after_the_wake_word_still_gives_up(self):
        a = self.make()
        with a.lock:
            a._wake_listen()
        now = time.monotonic()
        a.endpointer.active = True  # a short sound ...
        a._tick_wake(now)
        a.endpointer.active = False  # ... dropped as too short: nothing was sent
        a._tick_wake(now + a.WAKE_LISTEN_SECONDS + 1)
        self.assertEqual(a.state, appmod.IDLE)

    def test_a_gone_service_does_not_keep_the_window_by_what_it_last_said(self):
        a = self.make(from_standby=True)
        a.cfg.wake_idle_minutes = 5
        a.daemon_gone = "后台服务的连接断了"
        a._lines_snapshot = ({"p-1": {"state": "working", "unread": 3}}, "p-1", "Atlas")
        a.jobs = {"j1": {"state": "running"}}
        a._idle_since = time.monotonic() - 60 * 60
        with mock.patch.object(a, "quit") as q:
            self.assertTrue(a._maybe_back_to_standby(time.monotonic()))
        q.assert_called_once()

    def test_speech_still_playing_keeps_the_window(self):
        a = self.make(from_standby=True)
        a.speaker.idle.return_value = False
        a.speaker.stuck.return_value = False
        self.assertFalse(a._idle_now())

    def test_a_closed_window_ends_its_process_even_if_something_hangs(self):
        from voiceclaude import __main__ as entry
        ended = []
        with mock.patch.object(entry.os, "_exit", lambda code: ended.append(code)), \
                mock.patch.object(entry.logging, "shutdown", lambda: None), \
                mock.patch.object(entry.logging, "warning", lambda *a: None):
            t = entry.end_soon(0.01)
            t.join(2)
            t.cancel()  # never left running past the patches (it would end the test run)
            t.join()
        self.assertTrue(t.daemon)  # never keeps the process alive by itself
        self.assertEqual(ended, [0])

    def test_the_exit_guard_is_armed_when_the_window_fails_too(self):
        from voiceclaude import __main__ as entry
        from voiceclaude import app as appmod_

        class Broken:
            def __init__(self, *a, **k):
                pass

            def run(self):
                raise KeyboardInterrupt  # Ctrl+C in a console

        armed = []
        with mock.patch.object(entry, "end_soon", lambda *a, **k: armed.append(k.get("code", 0))), \
                mock.patch.object(appmod_, "App", Broken), mock.patch.object(standby, "hold_mutex", lambda *a, **k: None), \
                mock.patch.object(entry.logging, "FileHandler", lambda *a, **k: mock.Mock()), \
                mock.patch.object(entry.logging, "basicConfig", lambda **k: None), \
                mock.patch.object(entry.config, "load", config.Config), mock.patch("sys.argv", ["voiceclaude"]):
            with self.assertRaises(KeyboardInterrupt), self.assertLogs(level="ERROR") as logs:
                entry.main()
        self.assertEqual(armed, [1])  # ends with a failure code, not 0
        self.assertIn("KeyboardInterrupt", "\n".join(logs.output))  # in voice-claude.log even under pythonw

    def test_the_exit_guard_says_why_the_window_went(self):
        from voiceclaude import __main__ as entry
        said = []
        with mock.patch.object(entry.os, "_exit", lambda code: said.append(code)), \
                mock.patch.object(entry.logging, "shutdown", lambda: None), \
                mock.patch.object(entry.logging, "warning", lambda msg, *a: said.append(msg % a)):
            t = entry.end_soon(0.01, code=1)
            t.join(2)
            t.cancel()
            t.join()
        self.assertEqual(said, ["still running 0 s after the window stopped on an error; ending the process", 1])

    def test_speech_that_never_finishes_does_not_keep_the_window(self):
        a = self.make(from_standby=True)
        a.speaker.idle.return_value = False
        a.speaker.stuck.return_value = True  # the sound card stopped taking audio
        self.assertTrue(a._idle_now())

    def stuck_reply(self, a):
        """A reply was read out up to its done, then the sound card stopped taking audio."""
        a.state, a.turn_open, a.transcribing = appmod.BUSY, False, 0
        a.speaker.idle.return_value = False
        a.speaker.stuck.return_value = True

    def test_a_reply_whose_speech_never_moves_ends_the_turn(self):
        a = self.make(from_standby=True)
        self.stuck_reply(a)
        a.cfg.wake_idle_minutes = 5
        with self.assertLogs(appmod.log, "WARNING"):
            a._tick_ui()
        self.assertEqual(a.state, appmod.IDLE)
        a._drain_ui()
        self.assertIn("扬声器没有响应", a.textbox.get("1.0", "end"))
        now = time.monotonic()
        a._idle_since = now - 6 * 60
        a._maybe_back_to_standby(now)
        self.assertTrue(a._quit_requested)  # and the window goes back to standby

    def test_a_stuck_turn_reads_out_through_a_new_player(self):
        a = self.make()
        old_player, speaker = a.player, a.speaker
        self.stuck_reply(a)
        with self.assertLogs(appmod.log, "WARNING"):
            a._end_stuck_turn()
        speaker.stop.assert_called_once()  # the rest of that reply is dropped
        speaker.replace_player.assert_called_once_with(self.new_players[0])
        self.assertIs(a.player, self.new_players[0])  # ducking and the speaker switch use the new one
        self.assertEqual(appmod.audio.OUTPUT_DEVICE, 7)
        self.assertIsNot(a.player, old_player)

    def test_a_sound_card_that_keeps_failing_is_not_given_new_players_for_ever(self):
        a = self.make()
        a.player.progress_at = 0.0  # never played
        with self.assertLogs(appmod.log, "WARNING"):
            for _ in range(5):
                self.stuck_reply(a)
                a._end_stuck_turn()
                self.assertEqual(a.state, appmod.IDLE)  # every stuck reply still ends its turn
        self.assertEqual(len(self.new_players), a.MAX_NEW_PLAYERS)
        a._drain_ui()
        shown = a.textbox.get("1.0", "end")
        self.assertEqual(shown.count("扬声器没有响应"), a.MAX_NEW_PLAYERS + 1)  # said once more at the cap ...
        self.assertIn("重启 voice-claude", shown)  # ... with what to do

    def test_only_hangs_in_a_row_count_toward_the_cap(self):
        a = self.make()
        with self.assertLogs(appmod.log, "WARNING"):
            for _ in range(5):
                self.stuck_reply(a)
                a._end_stuck_turn()
                a.player.progress_at = 123.0  # the new player played fine before it hung too
        self.assertEqual(len(self.new_players), 5)

    def test_a_card_whose_question_is_stuck_is_asked_again_in_time(self):
        # the card waits 60 s; a 60 s stuck test would only fire after it has gone, unheard
        a = self.make()
        a.state = appmod.APPROVING
        a.approval = {"event": mock.Mock(), "answer": None, "text": "", "unclear": 0, "kind": "approve",
                      "prev": appmod.IDLE, "question": "要运行 pytest 吗？"}
        a.speaker.idle.return_value = False
        a.speaker.stuck.side_effect = lambda after=60.0: after <= 10  # nothing has moved for 10 s
        with self.assertLogs(appmod.log, "WARNING"):
            a._end_stuck_turn()
        a.speaker.replace_player.assert_called_once()
        a.speaker.say.assert_called_with("要运行 pytest 吗？")  # through the new player, while the card is up
        self.assertEqual(a.state, appmod.APPROVING)

    def test_ten_quiet_seconds_are_not_stuck_outside_a_card(self):
        a = self.make()
        self.stuck_reply(a)
        a.speaker.stuck.side_effect = lambda after=60.0: after <= 10
        a._end_stuck_turn()
        a.speaker.replace_player.assert_not_called()
        self.assertEqual(a.state, appmod.BUSY)

    def test_a_capped_player_that_plays_again_gets_a_new_one_when_it_hangs_again(self):
        a = self.make()
        a.player.progress_at = 0.0
        with self.assertLogs(appmod.log, "WARNING"):
            for _ in range(a.MAX_NEW_PLAYERS + 1):  # the cap is reached
                self.stuck_reply(a)
                a._end_stuck_turn()
            capped = a.player
            capped.progress_at = 999.0  # its write came back and it played for a while
            self.stuck_reply(a)
            a._end_stuck_turn()
        self.assertIsNot(a.player, capped)
        self.assertEqual(len(self.new_players), a.MAX_NEW_PLAYERS + 1)

    def test_speech_stuck_outside_a_turn_gets_a_new_player_too(self):
        # after 打断 / 别说了 the state is idle, and a greeting, a notice or a card's question can be what hangs
        for state in (appmod.IDLE, appmod.LISTENING, appmod.APPROVING):
            a = self.make()
            self.stuck_reply(a)
            a.state = state
            with self.assertLogs(appmod.log, "WARNING"):
                a._end_stuck_turn()
            a.speaker.replace_player.assert_called_once()
            self.assertEqual(a.state, state)  # no turn to end
            a.root.destroy()

    def test_a_failed_window_callback_is_logged(self):
        a = self.make()
        try:
            raise ValueError("boom")
        except ValueError as e:
            err = e
        with self.assertLogs(appmod.log, "ERROR") as logs:
            a.root.report_callback_exception(ValueError, err, err.__traceback__)
        self.assertIn("boom", "\n".join(logs.output))  # pythonw has no stderr for Tk to print to

    def test_speech_still_moving_or_more_to_come_keeps_the_turn(self):
        a = self.make(from_standby=True)
        cases = {
            "speech moving": lambda: setattr(a.speaker.stuck, "return_value", False),
            "answer still coming": lambda: setattr(a, "turn_open", True),
            "still transcribing": lambda: setattr(a, "transcribing", 1),
        }
        for name, change in cases.items():
            self.stuck_reply(a)
            change()
            a._end_stuck_turn()
            self.assertEqual(a.state, appmod.BUSY, name)

    def test_closing_while_speech_plays_is_not_called_stuck(self):
        a = self.make(from_standby=True)
        a.cfg.wake_idle_minutes = 5
        a.daemon_gone = "后台服务已关闭"
        a.speaker.idle.return_value = False
        a.speaker.stuck.return_value = False  # still reading the last reply
        a._idle_since = time.monotonic() - 60 * 60
        with mock.patch.object(a, "quit"), self.assertNoLogs(appmod.log, "WARNING"):
            self.assertTrue(a._maybe_back_to_standby(time.monotonic()))

    def test_after_stopping_standby_the_window_stays(self):
        a = self.make(from_standby=True)
        with mock.patch.object(standby, "stop_standby"):
            a._stop_standby()
        self.assertFalse(a.from_standby)

    def test_entering_standby_without_a_service_still_starts_it(self):
        a = self.make()
        a.daemon_gone = "后台服务已关闭"
        with mock.patch.object(standby, "start_standby") as start, mock.patch.object(a, "quit") as q,                 mock.patch.object(a, "_wake_problem", return_value=""):
            a._enter_standby()
        start.assert_called_once()
        q.assert_called_once()

    def test_the_wheel_restarts_the_idle_clock(self):
        from types import SimpleNamespace
        a = self.make(from_standby=True)
        a._idle_since = 0.0
        a._wheel(SimpleNamespace(x_root=0, y_root=0, delta=-120, widget=a.textbox))
        self.assertGreater(a._idle_since, time.monotonic() - 1)

    def test_anything_going_on_keeps_the_window(self):
        a = self.make(from_standby=True)
        a.cfg.wake_idle_minutes = 5
        now = time.monotonic()
        for busy in ("turn", "box", "job"):
            a._idle_since = now - 60 * 60
            if busy == "turn":
                a.turn_open = True
            elif busy == "box":
                a.turn_open = False
                a.typebox._hint(False)
                a.typebox.text.insert("1.0", "还没写完的话")
            else:
                a.typebox.text.delete("1.0", "end")
                a.jobs = {"j1": {"state": "running"}}
            a._maybe_back_to_standby(now)
            self.assertFalse(a._quit_requested, busy)
            self.assertEqual(a._idle_since, now, busy)  # the idle clock starts again


if __name__ == "__main__":
    unittest.main()

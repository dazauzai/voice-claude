"""Standby: only the wake word runs (about 1 % of one CPU core). On the wake word ("hi chat" unless config.toml's
[wake] says otherwise) it opens the full window, which starts listening at once; the window closes itself after a few
idle minutes and standby listens again. While any voice-claude window is open, standby leaves the microphone alone.

    pythonw -m voiceclaude.standby       (standby.bat; 设置 → 开机自动进入值守 puts it in the Startup folder)
"""
from __future__ import annotations

import ctypes
import logging
import os
import queue
import subprocess
import sys
import time
from pathlib import Path

from . import config, paths, testing

log = logging.getLogger(__name__)

WINDOW_MUTEX = "Local\\voice-claude-window"  # held by every running window (__main__)
STANDBY_MUTEX = "Local\\voice-claude-standby"
STARTUP_LINK = "voice-claude 值守.lnk"
_SYNCHRONIZE, _ERROR_ALREADY_EXISTS = 0x00100000, 183
DETACHED_PROCESS, CREATE_NEW_PROCESS_GROUP, CREATE_BREAKAWAY_FROM_JOB = 0x00000008, 0x00000200, 0x01000000
STOP_FILE = "standby.stop"  # in ~/.voice-claude: the window's 关掉值守 asks a running standby to end
SILENT_SECONDS = 10.0  # an open microphone that delivers nothing this long is opened again


# ---------- named mutexes: one standby, and "is a window open?" ----------
def _k32():
    k = ctypes.windll.kernel32
    k.CreateMutexW.restype = ctypes.c_void_p
    k.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
    k.OpenMutexW.restype = ctypes.c_void_p
    k.OpenMutexW.argtypes = [ctypes.c_ulong, ctypes.c_bool, ctypes.c_wchar_p]
    k.CloseHandle.argtypes = [ctypes.c_void_p]
    return k


def hold_mutex(name: str, exclusive: bool = False):
    """Create (and keep) a named mutex; with exclusive=True, None when another process already holds it."""
    if os.name != "nt":
        return object()
    k = _k32()
    h = k.CreateMutexW(None, False, name)
    if exclusive and h and ctypes.GetLastError() == _ERROR_ALREADY_EXISTS:
        k.CloseHandle(h)
        return None
    return h


def mutex_exists(name: str) -> bool:
    if os.name != "nt":
        return False
    k = _k32()
    h = k.OpenMutexW(_SYNCHRONIZE, False, name)
    if h:
        k.CloseHandle(h)
        return True
    return False


def window_open() -> bool:
    return mutex_exists(WINDOW_MUTEX)


# ---------- starting things ----------
def _pythonw() -> str:
    w = Path(sys.executable).with_name("pythonw.exe")
    return str(w if w.exists() else Path(sys.executable))


def window_command() -> list[str]:
    return [_pythonw(), "-X", "utf8", "-m", "voiceclaude", "--woken", "--standby"]


def standby_command() -> list[str]:
    return [_pythonw(), "-X", "utf8", "-m", "voiceclaude.standby"]


def _spawn(argv: list[str]) -> None:
    """Detached and, where the parent's job allows it, outside that job (an embedded terminal's job dies with its
    app)."""
    if testing.under_test_runner():
        raise RuntimeError(testing.REFUSAL)
    kw = dict(cwd=str(config.ROOT), close_fds=True, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
              stderr=subprocess.DEVNULL)
    try:
        subprocess.Popen(argv, creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_BREAKAWAY_FROM_JOB, **kw)
    except OSError:  # the job forbids breaking away
        subprocess.Popen(argv, creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP, **kw)


def standby_running() -> bool:
    return mutex_exists(STANDBY_MUTEX)


def stop_standby() -> None:
    """Ask a running standby to end (it checks every second)."""
    paths.ensure()
    (paths.HOME_DIR / STOP_FILE).write_text("stop", encoding="utf-8")


def start_standby() -> None:
    """From the window's 现在进入值守: standby waits by itself until this window has closed."""
    _spawn(standby_command())


# ---------- the Startup folder ----------
def startup_link() -> Path:
    return Path(os.environ.get("APPDATA", str(Path.home() / "AppData" / "Roaming"))) / "Microsoft" / "Windows" / \
        "Start Menu" / "Programs" / "Startup" / STARTUP_LINK


def _link_target(link: Path) -> tuple[str, str]:
    import win32com.client
    sc = win32com.client.Dispatch("WScript.Shell").CreateShortcut(str(link))
    return str(sc.TargetPath), str(sc.WorkingDirectory)


def autostart_enabled() -> bool:
    """On only when the shortcut starts this copy of voice-claude (a moved folder leaves a dead link behind)."""
    link = startup_link()
    if not link.exists():
        return False
    try:
        target, workdir = _link_target(link)
    except Exception:
        return False
    return Path(target).exists() and Path(workdir).resolve() == config.ROOT.resolve()


def set_autostart(on: bool) -> None:
    """Put a shortcut to standby in the user's Startup folder, or take it out. Nothing else on the system changes."""
    if testing.under_test_runner():
        raise RuntimeError(testing.REFUSAL)
    link = startup_link()
    if not on:
        link.unlink(missing_ok=True)
        return
    link.parent.mkdir(parents=True, exist_ok=True)
    cmd = standby_command()
    import win32com.client  # pywin32, a voice-claude dependency
    sc = win32com.client.Dispatch("WScript.Shell").CreateShortcut(str(link))
    sc.TargetPath, sc.Arguments = cmd[0], " ".join(cmd[1:])
    sc.WorkingDirectory, sc.Description = str(config.ROOT), "voice-claude：听唤醒词，听到后打开窗口"
    sc.Save()


# ---------- the listener ----------
class Standby:
    """The loop, apart from the microphone and the window so it can be tested: run_once() handles one chunk."""

    def __init__(self, spotter, launch=None, window_is_open=window_open):
        self.spotter = spotter
        self.launch = launch or (lambda: _spawn(window_command()))
        self.window_is_open = window_is_open
        self.woke_at = 0.0

    def heard(self, chunk) -> bool:
        """True when the wake word was heard and the window was started."""
        if not self.spotter.feed(chunk):
            return False
        log.info("wake word heard")
        self.woke_at = time.monotonic()
        self.launch()
        return True


def rescan_devices() -> None:
    """PortAudio lists devices once, when it starts: a microphone plugged in later is seen only after a restart, and
    one plugged in or out renumbers the others, so a stale list opens another device under the chosen one's name.
    Called before every open (about 0.03 s), when the microphone is closed. The wake word's beep stays open until
    the next one, so it is closed first: PortAudio's terminate would free it behind sounddevice's back. A failed
    start leaves PortAudio stopped; the next call starts it again rather than failing to terminate it."""
    try:
        import sounddevice as sd
    except Exception as e:
        log.warning("standby: could not list the sound devices again (%s)", e)
        return
    try:
        sd.stop()
        if sd._initialized:
            sd._terminate()
    except Exception as e:
        log.warning("standby: could not stop the sound devices (%s)", e)
    try:
        sd._initialize()
    except Exception as e:
        log.warning("standby: could not list the sound devices again (%s)", e)


def run() -> None:
    if testing.under_test_runner():
        raise RuntimeError(testing.REFUSAL)
    paths.ensure()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        handlers=[logging.FileHandler(paths.HOME_DIR / "standby.log", encoding="utf-8")])
    me = hold_mutex(STANDBY_MUTEX, exclusive=True)
    if me is None:
        log.info("standby is already running")
        return
    try:
        _listen_loop()
    except Exception:
        log.exception("standby stopped on an error")


def _sound_modules():
    """audio (sounddevice starts PortAudio when it is imported) and wake."""
    from . import audio, wake
    return audio, wake


def _forget_failed_sounddevice(e: BaseException) -> None:
    """sounddevice registers its exit handler before it starts PortAudio, so a failed import leaves that handler, and
    through it the whole dead module, in the exit table: take it out, or every retry would keep one more."""
    import atexit
    tb = e.__traceback__
    while tb is not None:
        g = tb.tb_frame.f_globals
        if g.get("__name__") == "sounddevice" and callable(g.get("_exit_handler")):
            atexit.unregister(g["_exit_handler"])
            return
        tb = tb.tb_next


def _listen_loop() -> None:
    stop = paths.HOME_DIR / STOP_FILE
    stop.unlink(missing_ok=True)
    wait = 10.0
    while True:
        try:
            audio, wake = _sound_modules()
            break
        except Exception as e:
            if type(e).__name__ != "PortAudioError":
                raise
            # just logged in, or a device mid-change: a failed import is undone, so the next one starts PortAudio again
            _forget_failed_sounddevice(e)
            log.warning("standby: sound devices not available (%s); trying again in %.0f s", e, wait)
        for _ in range(int(wait)):
            if stop.exists():
                stop.unlink(missing_ok=True)
                log.info("standby: stopped")
                return
            time.sleep(1.0)
        wait = min(wait * 2, 300.0)  # a failure that does not clear: less and less often, at most every 5 min
    if not wake.model_ready():
        log.error("the wake-word model is missing: %s", wake.KWS_DIR)
        return

    def settings():
        """Read again each time the microphone is opened: a changed wake word or microphone applies after the window
        closes, without restarting standby."""
        c = config.load()
        kw = getattr(c, "wake_keyword", "") or wake.DEFAULT_KEYWORD
        th = float(getattr(c, "wake_threshold", 0) or wake.DEFAULT_THRESHOLD)
        return c, kw, th

    def spotter_for(kw, th):
        try:
            return wake.Spotter(kw, th)
        except wake.WakeWordError as e:
            log.error("wake word %r unusable (%s); using %r", kw, e, wake.DEFAULT_KEYWORD)
            return wake.Spotter(wake.DEFAULT_KEYWORD, th)

    cfg, keyword, threshold = settings()
    sb = Standby(spotter_for(keyword, threshold))
    chunks: queue.Queue = queue.Queue(maxsize=100)

    def put(c):
        try:
            chunks.put_nowait(c)
        except queue.Full:
            pass  # behind: drop audio rather than block the sound card

    mic, last_chunk = None, 0.0
    log.info("standby: listening for %r (threshold %s)", keyword, threshold)
    while not stop.exists():
        if sb.window_is_open() or time.monotonic() - sb.woke_at < 30:  # a window is up (or still starting)
            if mic is not None:
                mic.stop()
                mic = None
                log.info("standby: window open, microphone released")
            time.sleep(1.0)
            continue
        if mic is None:
            try:
                new_cfg, new_kw, new_th = settings()
            except Exception as e:  # a typo in config.toml: keep listening with what worked before
                log.warning("standby: config.toml could not be read (%s); keeping the previous settings", e)
                new_cfg, new_kw, new_th = cfg, keyword, threshold
            if (new_kw, new_th) != (keyword, threshold):
                keyword, threshold = new_kw, new_th
                sb.spotter = spotter_for(keyword, threshold)
                log.info("standby: now listening for %r (threshold %s)", keyword, threshold)
            cfg = new_cfg
            while not chunks.empty():
                chunks.get_nowait()
            sb.spotter.reset()
            rescan_devices()
            try:
                mic = audio.Microphone(cfg.microphone, put, fallback=True)
                mic.start()
            except Exception as e:  # no device yet (just logged in), unplugged, taken: try again shortly
                log.warning("standby: microphone not available (%s); trying again in 10 s", e)
                mic = None
                time.sleep(10.0)
                continue
            last_chunk = time.monotonic()
            log.info("standby: microphone %s", mic.name)
        try:
            chunk = chunks.get(timeout=1.0)
        except queue.Empty:
            if time.monotonic() - last_chunk > SILENT_SECONDS:  # the device stopped delivering: open it again
                log.warning("standby: no sound from the microphone for %.0f s; opening it again", SILENT_SECONDS)
                try:
                    mic.stop()
                except Exception:
                    pass
                mic = None
            continue
        last_chunk = time.monotonic()
        if sb.heard(chunk):
            audio.tone("think")  # heard it: the window takes a few seconds to come up and then listens
            mic.stop()
            mic = None
    if mic is not None:
        mic.stop()
    stop.unlink(missing_ok=True)
    log.info("standby: stopped")


if __name__ == "__main__":
    run()

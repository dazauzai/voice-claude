# Architecture

How voice-claude fits together. The protocol between the two processes is in [protocol.md](protocol.md); desktop control and its gate in [desktop-control.md](desktop-control.md).

Design rules that explain most choices below: the simplest implementation that works; the feel of the interaction comes first; anything that acts on the computer is asked out loud first, and that gate lives in code, never in a prompt.

## Processes

```
window (pythonw -m voiceclaude)            background service (pythonw -m voiceclaude.daemon)
  microphone, VAD, speech recognition        switchboard: lines, manager, mailboxes, permission gate
  text-to-speech, hotkeys, the Tk window     jobs (detached background commands)
  client.py ── TCP, one JSON per line ──►    control endpoint (127.0.0.1 HTTP, POST /ask …)
                                               │
                                               ├─ line "manager"   claude -p (or rules, or Codex)
                                               ├─ line "scratch"   claude -p
                                               ├─ line per project claude -p  … or codex app-server
                                               │     each with its MCP tool servers:
                                               │     mcp_server.py (settings, permissions, jobs)
                                               │     desktop.py (desktop control)
                                               │     manager_server.py (manager only: connect, new project)
                                               └─ LiteLLM gateway (only for an OpenAI API key)
```

- **The window** (`app.py`) owns audio and the screen and nothing else. It talks to the service only through `client.py` (`DaemonClient`). If it is not running, `connect()` starts the service detached; when the window process lives inside a host's job object (terminals embedded in other apps), the service is started through the Task Scheduler so it outlives the host (`daemon.py`, `scripts/run_daemon.py`).
- **The background service** (`daemon.py` + `switchboard.py`) owns every model process. One front end at a time; a new one replaces the old (which gets `bye`). Quitting the window (× or 退出 voice-claude) calls `shutdown`; a window that crashes leaves the service running and reconnects on the next start.
- **Lines** (`line.py`): one line = one long-running conversation process with its own working directory. `claude.py` runs `claude -p --input-format stream-json --output-format stream-json` with `--mcp-config` for our tool servers, `--strict-mcp-config --no-chrome` (while `other_connectors` is deny, the default), `--permission-mode auto` and a deny list for common delete commands and for opening programs from the shell. `engine_codex.py` runs `codex app-server` (JSON-RPC over stdio) for the ChatGPT-plan route. Session ids are kept in `~/.voice-claude/lines.json`, so a project's conversation resumes next time.
- **The manager** is the line you start on. It routes you to projects ("去 Atlas"), creates projects, reports on the background, and knows the project index (`projects.py`: built from `~/.claude/projects`, plus aliases in `~/.voice-claude/projects.toml`). Without any model access it is a rules-only manager (`managers.py`).
- **Jobs** (`jobs.py`): "run this in the background" starts a detached command with its output in `~/.voice-claude/jobs/<id>.log`; it reports back when it ends.

## Who answers: providers and engines

`access.py` decides per provider whether it can answer and how:

| Provider | Route | Engine |
|---|---|---|
| Claude | the user's own logged-in Claude Code (`subscription`) or an Anthropic API key | Claude Code |
| DeepSeek, Kimi | API key, through the vendor's Anthropic-compatible endpoint | Claude Code |
| GPT | the user's own Codex login with a ChatGPT plan (`codex_login`) | Codex |
| GPT | OpenAI API key, through a local LiteLLM proxy translating Anthropic Messages to Chat Completions (`gateway.py`) | Claude Code |

LiteLLM is used for the OpenAI key route rather than a home-made translation because it already handles streaming, tool calls and tool results. Codex keeps its own login in its own folder (`CODEX_HOME`, by default `~/.codex`); voice-claude only asks `codex login status` and starts `codex login` when you press 登录….

Each line has its own provider; `[providers] default` is used for new lines and the manager. Keys live only in `~/.voice-claude/auth.json` (readable by the user only) and never appear in logs or events (masked to the last four characters). voice-claude never performs a vendor login itself: the 登录… button runs the vendor's own program (`claude auth login`, `codex login`).

Before a line starts on a vendor, a 3-second TLS probe (`access.reachable`) tells "this network blocks it" apart from a slow model.

## The permission gate

Every request that needs the user's yes arrives at one place: `switchboard.gate` (one action) or `gate_batch` (a series of desktop steps). Tool servers post them to the control endpoint (`POST /ask` with the action, the sentence to speak, a detail line, and plain `facts` about the action). The desktop tools cannot act without the gate's answer. The model's own shell and file tools are not confined by this gate (Claude Code's auto mode checks them); a model determined to get around it could, for example, drive the mouse from its shell. See desktop-control.md, "管不到的".

1. The permissions table (`[permissions]`, keys in `config.PERMISSION_LABELS`): `deny` refuses without asking, `allow` passes without asking, `ask` asks.
2. Transparent mode (`[permissions] transparent`): every `ask` passes, except deleting and overwriting an existing file, which are still asked with the reason first. `risk.py` decides from the facts (keys, typed text, shell command, Codex file changes; for a click, the model's own name for the target) and falls back to the texts. It recognises the common cases: delete keys, delete commands, delete / replace / overwrite buttons, typing the path of an existing file (or, in a Save As dialog, a name that already exists in its folder), confirming Windows' "file already exists, replace?" dialog. Saving a new file is not asked. Other ways of deleting or overwriting are not detected. Turning it on is always asked out loud.
3. Asking: the service sends an `ask` event; the window docks a request card, speaks the question and listens for 可以 / 不行 (or right Alt / right Ctrl, or the buttons). No answer within 60 s counts as no.

Changing a permission, switching models and switching providers by voice go through the same gate. Lines that are not in front may not touch the desktop.

## The window

`app.py` is a small state machine: `idle` → `recording` (tap) or `listening` (continuous conversation) → `busy` (the line is working or speaking) → back; `approving` while a request card is up (talking then answers the request instead of interrupting the line).

- Audio (`audio.py`): the microphone feeds Silero VAD (`Endpointer`) for "you have finished"; SenseVoice (`asr.py`) turns the utterance into text; replies are split into sentences as they stream (`text.SentenceSplitter`) and spoken by Kokoro (`tts.py`) while the rest is still being written. With headphones, talking over it first lowers its voice, then stops it; the line keeps working and your words are queued.
- Hotkeys (`hotkey.py`) are polled with `GetAsyncKeyState`; no keyboard hook is installed. Taps, double taps and holds of one key; a second key for mute / send now / yes.
- Fixed phrases (`text.fixed_phrase`: back to the manager, background status, transparent mode on / off, …) are handled without a model.
- The look (`ui_theme.py`, `ui_widgets.py`): the colour of the status bar answers one question, "can it hear me now?" (green = the microphone is open; amber = it waits for your decision). Every size derives from one font size (8–40). Widgets are drawn with Pillow so they stay crisp at any DPI. UI text names the function, its state and the action, nothing more.
- Pages in the same card: the transcript, settings, the permissions sub-page and the AI 服务商 (AI service) page (`ui_access.py`).

## Files

- `~/.voice-claude/`: `config.toml` (the user's settings, created from `config.example.toml`), `auth.json` (keys), `daemon.json` (port and token of the running service), `daemon.log`, `lines.json`, `projects.toml`, `jobs/`, `manager/` (the manager's own working directory and log), `scratch/`, `manager-prompt.md` (optional override of the manager's prompt).
- The repository: `voice-claude.log` (the window's log), `models/` (downloaded speech models), `.venv/`.

## Tests

`python -X utf8 -m unittest discover -s tests` runs everything that needs no models, microphone, keys or model service. `voiceclaude/testing.py` makes that safe by construction: under a unit-test runner (or with `VOICECLAUDE_TESTING=1`) no real Claude Code, Codex, gateway or service may start, and `~/.voice-claude` is replaced by a temporary directory. `scripts/selftest.py` drives a real temporary service with synthetic speech; see CONTRIBUTING.md before running it.

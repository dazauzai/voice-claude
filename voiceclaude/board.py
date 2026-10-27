"""The shared whiteboard, service side: one Excalidraw scene per project in ~/.voice-claude/boards/<key>.json (the
key is the project line's id without "p-", as for records), written only here. The board window's page reads and
saves the scene and waits for what the line's tools add; the tools (mcp_server.py -> switchboard) add cards, images
and shapes, ask for a snapshot, or clear the board.

The page talks to the control server under /board (see handle): the static page from voiceclaude/whiteboard/, and the
API under /board/api/, which needs the run's token (daemon.json "board_token"; ?token= or the X-Board-Token header)
and a Host of 127.0.0.1 or localhost, so no web page in a browser can read or change a board."""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import mimetypes
import random
import re
import secrets
import struct
import threading
import time
import uuid
from pathlib import Path
from typing import Callable
from urllib.parse import parse_qs, unquote, urlsplit

from .usage import write_json_atomic

log = logging.getLogger(__name__)

STATIC = Path(__file__).resolve().parent / "whiteboard"  # the Excalidraw page (built and put there by the window side)
MAX_IMAGE = 2 * 1024 * 1024  # bytes of one image put on the board
MAX_SNAPSHOT = 30 * 1024 * 1024
WRAP_UNITS = 60  # a card's line: 60 narrow characters, 30 wide (CJK) ones
GAP = 40
ROW_WIDTH = 1800  # cards go in rows this wide, then a new row below everything
SNAPSHOT_WAIT = 5.0
OPEN_FOR = 35.0  # a page that asked for events within this many seconds counts as open
POLL_WAIT = 25.0
# Lines with no board: a window opened while one of them was in front would show a board no line can see
NO_BOARD = {"manager": "总机", "scratch": "杂项"}
KEEP_EVENTS = 200
IMAGE_MIME = {".png": "image/png", ".svg": "image/svg+xml", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
              ".gif": "image/gif", ".webp": "image/webp"}
CARD_COLOR = "#fff9db"


def empty_scene() -> dict:
    return {"elements": [], "appState": {"viewBackgroundColor": "#ffffff"}, "files": {}}


# ---------- elements (Excalidraw's own shape, so a saved board opens as it is) ----------

def _base(kind: str, x: float, y: float, w: float, h: float, group: str = "", **extra) -> dict:
    now = int(time.time() * 1000)
    return {"id": uuid.uuid4().hex[:20], "type": kind, "x": round(x), "y": round(y), "width": round(w), "height": round(h),
            "angle": 0, "strokeColor": "#1e1e1e", "backgroundColor": "transparent", "fillStyle": "solid", "strokeWidth": 2,
            "strokeStyle": "solid", "roughness": 1, "opacity": 100, "groupIds": [group] if group else [], "frameId": None,
            "roundness": None, "seed": random.randint(1, 2 ** 31 - 1), "version": 1,
            "versionNonce": random.randint(1, 2 ** 31 - 1), "isDeleted": False, "boundElements": None, "updated": now,
            "link": None, "locked": False, "customData": {"by": "ai"}, **extra}


def _units(ch: str) -> int:
    return 2 if ord(ch) > 0x2E7F else 1  # CJK and other wide characters take two


def wrap(text: str, units: int = WRAP_UNITS) -> list[str]:
    """Lines of at most `units` (wide characters count two), breaking between words where there are words."""
    out = []
    for para in str(text).replace("\r\n", "\n").split("\n"):
        line, width = "", 0
        for token in re.findall(r"[A-Za-z0-9_'\"().,;:!?/%+\-]+ ?| |.", para):
            w = sum(_units(c) for c in token)
            if width + w > units and line.strip():
                out.append(line.rstrip())
                line, width = token.lstrip(), sum(_units(c) for c in token.lstrip())
            else:
                line, width = line + token, width + w
        out.append(line.rstrip())
    return out


def _text(text: str, x: float, y: float, size: int = 20, group: str = "") -> dict:
    lines = wrap(text)
    body = "\n".join(lines)
    w = max((sum(_units(c) for c in ln) for ln in lines), default=1) * size * 0.55
    h = max(1, len(lines)) * size * 1.25
    return _base("text", x, y, max(w, size), h, group, text=body, originalText=body, fontSize=size, fontFamily=2,
                 textAlign="left", verticalAlign="top", containerId=None, autoResize=True, lineHeight=1.25)


def _plain(markdown: str) -> str:
    """A card shows text, not Markdown: headings' # and **bold** markers taken off."""
    t = re.sub(r"(?m)^\s{0,3}#{1,6}\s*", "", str(markdown))
    return t.replace("**", "").strip()


def card(text: str, title: str, x: float, y: float) -> list[dict]:
    """A text card: a light rectangle with an optional title and the text, grouped so they move together."""
    group, pad, elements, top = uuid.uuid4().hex[:12], 16, [], y + 16
    if title:
        head = _text(_plain(title), x + pad, top, 24, group)
        elements.append(head)
        top += head["height"] + 8
    body = _text(_plain(text), x + pad, top, 20, group)
    elements.append(body)
    w = max(e["width"] for e in elements) + 2 * pad
    h = body["y"] + body["height"] + pad - y
    box = _base("rectangle", x, y, w, h, group, backgroundColor=CARD_COLOR, roundness={"type": 3})
    return [box] + elements


def image_size(data: bytes, mime: str) -> tuple[int, int]:
    try:
        if mime == "image/png" and data[:8] == b"\x89PNG\r\n\x1a\n":
            return struct.unpack(">II", data[16:24])
        if mime == "image/gif":
            return struct.unpack("<HH", data[6:10])
        if mime == "image/jpeg":
            i = 2
            while i < len(data) - 9:
                if data[i] != 0xFF:
                    i += 1
                    continue
                marker, size = data[i + 1], struct.unpack(">H", data[i + 2:i + 4])[0]
                if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
                    h, w = struct.unpack(">HH", data[i + 5:i + 9])
                    return w, h
                i += 2 + size
        if mime == "image/svg+xml":
            text = data[:4000].decode("utf-8", "replace")
            w = re.search(r'\bwidth="([\d.]+)(?:px)?"', text)
            h = re.search(r'\bheight="([\d.]+)(?:px)?"', text)
            if w and h:
                return int(float(w.group(1))), int(float(h.group(1)))
            vb = re.search(r'viewBox="[\d.\-]+[ ,]+[\d.\-]+[ ,]+([\d.]+)[ ,]+([\d.]+)"', text)
            if vb:
                return int(float(vb.group(1))), int(float(vb.group(2)))
    except (struct.error, ValueError, IndexError):
        pass
    return 400, 300


def image(path: str, title: str, x: float, y: float) -> tuple[list[dict], dict]:
    """An image file (PNG / SVG / JPEG / GIF / WebP, at most MAX_IMAGE) as an image element and its file."""
    p = Path(path)
    mime = IMAGE_MIME.get(p.suffix.lower())
    if not mime:
        raise ValueError(f"白板只放图片文件（PNG、SVG、JPG、GIF、WebP），不是 {p.suffix or p.name}")
    data = p.read_bytes()
    if len(data) > MAX_IMAGE:
        raise ValueError(f"图片太大（{len(data) // 1024} KB，最多 {MAX_IMAGE // 1024 // 1024} MB）")
    w, h = image_size(data, mime)
    scale = min(1.0, 800 / max(w, 1), 600 / max(h, 1))
    w, h = max(w * scale, 20), max(h * scale, 20)
    file_id = hashlib.sha1(data).hexdigest()
    now = int(time.time() * 1000)
    files = {file_id: {"mimeType": mime, "id": file_id, "dataURL": f"data:{mime};base64,{base64.b64encode(data).decode()}",
                       "created": now, "lastRetrieved": now}}
    group = uuid.uuid4().hex[:12] if title else ""
    elements = [_base("image", x, y, w, h, group, fileId=file_id, status="saved", scale=[1, 1], crop=None)]
    if title:
        elements.append(_text(_plain(title), x, y + h + 8, 18, group))
    return elements, files


def shape(kind: str, label: str, x: float, y: float) -> list[dict]:
    """A rectangle, an ellipse or an arrow (pointing right), with its label."""
    group = uuid.uuid4().hex[:12] if label else ""
    if kind == "arrow":
        elements = [_base("arrow", x, y + 30, 240, 0, group, points=[[0, 0], [240, 0]], lastCommittedPoint=None,
                          startBinding=None, endBinding=None, startArrowhead=None, endArrowhead="arrow", elbowed=False)]
        if label:
            elements.append(_text(label, x + 20, y, 18, group))
        return elements
    if kind not in ("rectangle", "ellipse"):
        raise ValueError(f"形状只有 rectangle、ellipse、arrow，没有 {kind}")
    t = _text(label, 0, 0, 20, group) if label else None
    w, h = max(240, (t["width"] + 48) if t else 0), max(120, (t["height"] + 48) if t else 0)
    elements = [_base(kind, x, y, w, h, group, roundness={"type": 3} if kind == "rectangle" else {"type": 2})]
    if t:
        t["x"], t["y"] = round(x + (w - t["width"]) / 2), round(y + (h - t["height"]) / 2)
        elements.append(t)
    return elements


def texts_of(scene: dict) -> list[str]:
    """Every text on the board, top to bottom and left to right, word for word."""
    items = [e for e in scene.get("elements") or [] if e.get("type") == "text" and not e.get("isDeleted")
             and str(e.get("text") or "").strip()]
    items.sort(key=lambda e: (round(float(e.get("y") or 0) / 20), float(e.get("x") or 0)))
    return [str(e.get("originalText") or e.get("text")) for e in items]


def _bounds(e: dict) -> tuple[float, float, float, float]:
    x, y = float(e.get("x") or 0), float(e.get("y") or 0)
    return x, y, x + abs(float(e.get("width") or 0)), y + abs(float(e.get("height") or 0))


# ---------- the store, the page's events, snapshots ----------

class Boards:
    def __init__(self, folder: Path, now: Callable[[], float] = time.time):
        self.folder, self.now = folder, now
        self.token = secrets.token_urlsafe(18)  # the page's key to the API (daemon.json "board_token")
        self.port = 0  # the control server's port: the Host the API answers to
        self.cond = threading.Condition()
        self.boards: dict[str, dict] = {}
        self.events: dict[str, list[dict]] = {}
        self.seq = 0
        self.polling: dict[str, int] = {}
        self.last_poll: dict[str, float] = {}
        self.snapshots: dict[str, dict] = {}
        # (key, snapshot) when the user pressed 让它看 in the window (request "user"): the switchboard hands it to the
        # board's project line
        self.on_user_snapshot: Callable[[str, dict], None] | None = None

    # -- store --

    def _file(self, key: str) -> Path:
        return self.folder / f"{re.sub(r'[^A-Za-z0-9_-]', '_', key)}.json"

    def _board(self, key: str, name: str = "") -> dict:
        b = self.boards.get(key)
        if b is None:
            try:
                b = json.loads(self._file(key).read_text("utf-8-sig"))
                if not isinstance(b, dict) or not isinstance(b.get("scene"), dict):
                    raise ValueError("not a board")
            except (OSError, ValueError):
                b = {"name": name, "scene": empty_scene(), "version": 0, "cursor": None}
            b.setdefault("added", {})
            self.boards[key] = b
        if name:
            b["name"] = name
        return b

    def _save(self, key: str) -> None:
        b = self.boards[key]
        self.folder.mkdir(parents=True, exist_ok=True)
        write_json_atomic(self._file(key), {k: v for k, v in b.items() if k != "added"})

    def scene(self, key: str, name: str = "") -> tuple[dict, int, str]:
        with self.cond:
            b = self._board(key, name)
            return json.loads(json.dumps(b["scene"])), b["version"], b.get("name") or ""

    def save_scene(self, key: str, scene: dict, base: int) -> int:
        """The page's whole scene. What a tool added after the version the page had seen, and the page does not
        have yet, is kept (the page would otherwise drop it with this save)."""
        with self.cond:
            b = self._board(key)
            have = {str(e.get("id")) for e in scene.get("elements") or []}
            late = [e for e in b["scene"]["elements"]
                    if any(str(e.get("id")) in ids for v, ids in b["added"].items() if int(v) > base) and str(e.get("id")) not in have]
            files = {**{k: v for k, v in b["scene"].get("files", {}).items()
                        if any(e.get("fileId") == k for e in late)}, **(scene.get("files") or {})}
            b["scene"] = {"elements": list(scene.get("elements") or []) + late,
                          "appState": dict(scene.get("appState") or {}), "files": files}
            b["version"] += 1
            self._save(key)
            return b["version"]

    def add(self, key: str, elements: list[dict], files: dict | None = None, name: str = "") -> list[str]:
        with self.cond:
            b = self._board(key, name)
            b["scene"]["elements"].extend(elements)
            b["scene"].setdefault("files", {}).update(files or {})
            b["version"] += 1
            ids = [e["id"] for e in elements]
            b["added"][str(b["version"])] = set(ids)
            self._save(key)
            self._event(key, {"kind": "added", "elements": elements, "files": files or {}, "version": b["version"]})
            return ids

    def clear(self, key: str) -> Path | None:
        """Empty the board; the old scene is kept beside it (<key>-cleared-<time>.json)."""
        with self.cond:
            b = self._board(key)
            backup = None
            if b["scene"]["elements"]:
                backup = self.folder / f"{self._file(key).stem}-cleared-{time.strftime('%Y%m%d-%H%M%S', time.localtime(self.now()))}.json"
                self.folder.mkdir(parents=True, exist_ok=True)
                write_json_atomic(backup, b["scene"])
            b["scene"], b["cursor"] = empty_scene(), None
            b["version"] += 1
            self._save(key)
            self._event(key, {"kind": "cleared", "version": b["version"]})
            return backup

    def place(self, key: str, w: float, h: float, near: str = "") -> tuple[float, float]:
        """Where a new item of this size goes: right of `near` (an element's id), else right of the previous item in
        the current row, else a new row below everything; never on top of something already there."""
        with self.cond:
            b = self._board(key)
            live = [e for e in b["scene"]["elements"] if not e.get("isDeleted")]
            if near:
                el = next((e for e in live if e.get("id") == near), None)
                if el:
                    x0, y0, x1, _ = _bounds(el)
                    return x1 + GAP, y0
            boxes = [_bounds(e) for e in live]
            cur = b.get("cursor")
            if cur:
                x, y = cur
                free = all(x + w + GAP <= bx0 or bx1 + GAP <= x or y + h + GAP <= by0 or by1 + GAP <= y
                           for bx0, by0, bx1, by1 in boxes)
                if free and x + w <= ROW_WIDTH:
                    b["cursor"] = [x + w + GAP, y]
                    return x, y
            x = min((bx0 for bx0, _, _, _ in boxes), default=0)
            y = max((by1 for _, _, _, by1 in boxes), default=-GAP) + GAP
            b["cursor"] = [x + w + GAP, y]
            return x, y

    # -- the page --

    def _event(self, key: str, event: dict) -> None:
        self.seq += 1
        lst = self.events.setdefault(key, [])
        lst.append({**event, "seq": self.seq})
        del lst[:-KEEP_EVENTS]
        self.cond.notify_all()

    def wait_events(self, key: str, since: int, wait: float = POLL_WAIT) -> dict:
        until = time.monotonic() + max(0.0, min(wait, POLL_WAIT))
        with self.cond:
            self.polling[key] = self.polling.get(key, 0) + 1
            try:
                while True:
                    new = [e for e in self.events.get(key, []) if e["seq"] > since]
                    left = until - time.monotonic()
                    if new or left <= 0:
                        return {"ok": True, "events": new, "seq": self.seq}
                    self.cond.wait(left)
            finally:
                self.polling[key] -= 1
                self.last_poll[key] = self.now()

    def is_open(self, key: str) -> bool:
        with self.cond:
            return self.polling.get(key, 0) > 0 or self.now() - self.last_poll.get(key, -1e9) < OPEN_FOR

    def open_keys(self) -> list[str]:
        """Every board a window is showing now."""
        with self.cond:
            return sorted(k for k in set(self.polling) | set(self.last_poll) if self.is_open(k))

    def snapshot(self, key: str, wait: float = SNAPSHOT_WAIT) -> dict | None:
        """Ask the open page for the current view; its answer, or None (no page, or none within `wait`)."""
        if not self.is_open(key):
            return None
        request = uuid.uuid4().hex[:12]
        until = time.monotonic() + wait
        with self.cond:
            self._event(key, {"kind": "snapshot", "request": request})
            while request not in self.snapshots and time.monotonic() < until:
                self.cond.wait(until - time.monotonic())
            return self.snapshots.pop(request, None)

    def store_snapshot(self, key: str, request: str, png: str, texts: list) -> dict:
        data = base64.b64decode(png.split(",", 1)[-1] if png.startswith("data:") else png, validate=False)
        if len(data) > MAX_SNAPSHOT:
            raise ValueError("截图太大")
        stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(self.now()))
        stem = f"{self._file(key).stem}-{stamp}"
        self.folder.mkdir(parents=True, exist_ok=True)
        png_path, txt_path = self.folder / f"{stem}.png", self.folder / f"{stem}.txt"
        png_path.write_bytes(data)
        texts = [str(t) for t in texts or []]
        txt_path.write_text("\n\n".join(texts) + ("\n" if texts else ""), "utf-8", newline="\n")
        result = {"ok": True, "png": str(png_path), "txt": str(txt_path), "texts": texts}
        if request == "user":  # the user's own 让它看: no tool is waiting; the line is told (not on this request's thread)
            if self.on_user_snapshot:
                threading.Thread(target=self.on_user_snapshot, args=(key, result), daemon=True, name="board-look").start()
            return result
        with self.cond:
            if request:
                self.snapshots[request] = result
                self.cond.notify_all()
        return result

    # -- HTTP (the control server hands /board here) --

    def handle(self, method: str, path: str, headers: dict, body: bytes | None) -> tuple[int, str, bytes]:
        url = urlsplit(path)
        query = {k: v[-1] for k, v in parse_qs(url.query).items()}
        route = url.path.rstrip("/")
        if not route.startswith("/board/api"):
            return self._static(url.path) if method == "GET" else _json(405, {"ok": False, "message": "只能 GET"})
        host = str(headers.get("Host") or headers.get("host") or "")
        if host not in (f"127.0.0.1:{self.port}", f"localhost:{self.port}"):
            return _json(403, {"ok": False, "message": "只接受本机的白板窗口"})
        token = str(headers.get("X-Board-Token") or headers.get("x-board-token") or query.get("token") or "")
        if not secrets.compare_digest(token, self.token):
            return _json(403, {"ok": False, "message": "白板的口令不对"})
        try:
            data = json.loads(body or b"{}") if method == "POST" else {}
        except ValueError:
            return _json(400, {"ok": False, "message": "不是 JSON"})
        key = str(data.get("project") or query.get("project") or "").removeprefix("p-")
        if not key or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", key):
            return _json(400, {"ok": False, "message": "缺 project（项目线的 id）"})
        if key in NO_BOARD:
            return _json(404, {"ok": False, "message": f"{NO_BOARD[key]}没有白板：先进入一个项目，再说“打开白板”"})
        try:
            if route == "/board/api/scene" and method == "GET":
                scene, version, name = self.scene(key)
                return _json(200, {"ok": True, "scene": scene, "version": version, "name": name, "seq": self.seq})
            if route == "/board/api/scene" and method == "POST":
                scene = data.get("scene")
                if not isinstance(scene, dict):
                    return _json(400, {"ok": False, "message": "缺 scene"})
                return _json(200, {"ok": True, "version": self.save_scene(key, scene, int(data.get("version") or 0))})
            if route == "/board/api/events" and method == "GET":
                return _json(200, self.wait_events(key, int(query.get("since") or 0), float(query.get("wait") or POLL_WAIT)))
            if route == "/board/api/snapshot" and method == "POST":
                r = self.store_snapshot(key, str(data.get("request") or ""), str(data.get("png") or ""), data.get("texts") or [])
                return _json(200, {k: v for k, v in r.items() if k != "texts"})
        except (ValueError, TypeError) as e:
            return _json(400, {"ok": False, "message": str(e)})
        return _json(404, {"ok": False, "message": "没有这个白板接口"})

    def _static(self, path: str) -> tuple[int, str, bytes]:
        rel = unquote(path[len("/board"):]).lstrip("/") or "index.html"
        target = (STATIC / rel).resolve()
        if STATIC.resolve() not in target.parents or not target.is_file():  # nothing outside the page's folder
            return 404, "text/plain; charset=utf-8", "白板页面不在（voiceclaude/whiteboard/）".encode("utf-8")
        ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript", "application/json"):
            ctype += "; charset=utf-8"
        return 200, ctype, target.read_bytes()


def _json(status: int, data: dict) -> tuple[int, str, bytes]:
    return status, "application/json; charset=utf-8", json.dumps(data, ensure_ascii=False).encode("utf-8")

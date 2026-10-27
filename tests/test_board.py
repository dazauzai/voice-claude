"""The shared whiteboard's service side (board.py): elements, the store, the page's events, snapshots and the API."""
import base64
import json
import struct
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
import zlib
from pathlib import Path
from unittest import mock

from voiceclaude import board
from voiceclaude.control import ControlServer


def png(w, h):
    raw = b"".join(b"\x00" + b"\xff\xff\xff" * w for _ in range(h))
    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)) \
        + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")


class ElementTests(unittest.TestCase):
    def test_a_card_is_a_box_with_its_text_grouped(self):
        els = board.card("## 结论\n**D6** 的家族按 FDA 批准药物预先指定", "D6", 100, 50)
        self.assertEqual([e["type"] for e in els], ["rectangle", "text", "text"])
        self.assertEqual(len({tuple(e["groupIds"]) for e in els}), 1)  # one group: they move together
        self.assertEqual(els[2]["text"], "结论\nD6 的家族按 FDA 批准药物预先指定")  # Markdown marks taken off
        self.assertTrue(all(e["customData"] == {"by": "ai"} for e in els))
        box, body = els[0], els[2]
        self.assertGreaterEqual(box["x"] + box["width"], body["x"] + body["width"])
        for key in ("id", "seed", "versionNonce", "isDeleted", "fontSize", "originalText", "lineHeight"):
            self.assertIn(key, body)

    def test_wrapping_counts_wide_characters_double(self):
        self.assertTrue(all(sum(board._units(c) for c in ln) <= 60 for ln in board.wrap("中" * 100)))
        self.assertEqual(len(board.wrap("中" * 100)), 4)  # 30 a line
        lines = board.wrap("word " * 30)
        self.assertTrue(all(ln.endswith("word") for ln in lines))  # breaks between words

    def test_images_and_shapes(self):
        d = Path(tempfile.mkdtemp())
        (d / "a.png").write_bytes(png(1600, 900))
        (d / "b.svg").write_text('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 300 150"></svg>', "utf-8")
        els, files = board.image(str(d / "a.png"), "图 1", 0, 0)
        self.assertEqual([e["type"] for e in els], ["image", "text"])
        self.assertEqual((els[0]["width"], els[0]["height"]), (800, 450))  # scaled to fit 800 x 600
        f = files[els[0]["fileId"]]
        self.assertTrue(f["dataURL"].startswith("data:image/png;base64,"))
        els, _ = board.image(str(d / "b.svg"), "", 0, 0)
        self.assertEqual((els[0]["width"], els[0]["height"]), (300, 150))
        (d / "big.png").write_bytes(b"\x89PNG" + b"0" * (board.MAX_IMAGE + 1))
        with self.assertRaises(ValueError):
            board.image(str(d / "big.png"), "", 0, 0)
        with self.assertRaises(ValueError):
            board.image(str(d / "notes.txt"), "", 0, 0)
        self.assertEqual([e["type"] for e in board.shape("ellipse", "开始", 0, 0)], ["ellipse", "text"])
        arrow = board.shape("arrow", "", 0, 0)[0]
        self.assertEqual((arrow["type"], arrow["points"], arrow["endArrowhead"]), ("arrow", [[0, 0], [240, 0]], "arrow"))
        with self.assertRaises(ValueError):
            board.shape("star", "", 0, 0)


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.b = board.Boards(self.dir)

    def test_added_elements_are_saved_and_announced(self):
        ids = self.b.add("k1", board.card("一", "", 0, 0), {}, "Atlas")
        saved = json.loads((self.dir / "k1.json").read_text("utf-8"))
        self.assertEqual((saved["name"], saved["version"], [e["id"] for e in saved["scene"]["elements"]]), ("Atlas", 1, ids))
        self.assertNotIn("added", saved)
        ev = self.b.wait_events("k1", 0, wait=0)
        self.assertEqual([e["kind"] for e in ev["events"]], ["added"])
        self.assertEqual(self.b.wait_events("k1", ev["seq"], wait=0)["events"], [])

    def test_a_page_save_does_not_drop_what_a_tool_added_meanwhile(self):
        first = self.b.add("k", board.card("一", "", 0, 0))
        _, seen, _ = self.b.scene("k")
        late = self.b.add("k", board.card("二", "", 0, 300))  # added after the page loaded the scene
        user = {"id": "u1", "type": "freedraw", "x": 0, "y": 0, "width": 5, "height": 5}
        page = {"elements": [e for e in self.b.scene("k")[0]["elements"] if e["id"] in first] + [user], "appState": {}, "files": {}}
        self.b.save_scene("k", page, seen)
        ids = [e["id"] for e in self.b.scene("k")[0]["elements"]]
        self.assertTrue(set(first) | set(late) | {"u1"} <= set(ids))
        # the user deleting a tool's element they had seen is kept as deleted
        _, now, _ = self.b.scene("k")
        self.b.save_scene("k", {"elements": [user], "appState": {}, "files": {}}, now)
        self.assertEqual([e["id"] for e in self.b.scene("k")[0]["elements"]], ["u1"])

    def test_clearing_keeps_a_backup(self):
        self.b.add("k", board.card("一", "", 0, 0))
        backup = self.b.clear("k")
        self.assertEqual(self.b.scene("k")[0]["elements"], [])
        self.assertEqual(len(json.loads(backup.read_text("utf-8"))["elements"]), 2)  # its box and its text
        self.assertEqual(self.b.wait_events("k", 1, wait=0)["events"][-1]["kind"], "cleared")

    def test_placing_in_rows_next_to_and_never_on_top(self):
        self.b.add("k", [{"id": "u", "type": "rectangle", "x": 0, "y": 0, "width": 500, "height": 400}])
        x, y = self.b.place("k", 300, 200)
        self.assertGreaterEqual(y, 400)  # below what the user drew
        self.b.add("k", [{"id": "a", "type": "rectangle", "x": x, "y": y, "width": 300, "height": 200}])
        x2, y2 = self.b.place("k", 300, 200)
        self.assertEqual((x2, y2), (x + 300 + board.GAP, y))  # the same row
        self.assertEqual(self.b.place("k", 100, 100, near="u"), (500 + board.GAP, 0))
        for _ in range(10):
            px, py = self.b.place("k", 300, 200)
            self.b.add("k", [{"id": f"x{px}{py}", "type": "rectangle", "x": px, "y": py, "width": 300, "height": 200}])
            self.assertLessEqual(px + 300, board.ROW_WIDTH)

    def test_which_boards_are_open(self):
        now = self.b.now()
        self.b.last_poll.update(a=now, c=now - board.OPEN_FOR - 1)
        self.b.polling["b"] = 1  # a page waiting for events right now
        self.assertEqual(self.b.open_keys(), ["a", "b"])
        self.assertFalse(self.b.is_open("c"))

    def test_texts_in_reading_order(self):
        scene = {"elements": [{"type": "text", "text": "B", "x": 300, "y": 0}, {"type": "text", "text": "A", "x": 0, "y": 5},
                              {"type": "text", "text": "C", "x": 0, "y": 200}, {"type": "text", "text": "gone", "x": 0,
                                                                                  "y": 0, "isDeleted": True}]}
        self.assertEqual(board.texts_of(scene), ["A", "B", "C"])


class PageTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.b = board.Boards(self.dir)
        self.b.port = 5555
        self.h = {"Host": "127.0.0.1:5555", "X-Board-Token": self.b.token}

    def call(self, method, path, body=None, headers=None):
        status, ctype, payload = self.b.handle(method, path, self.h if headers is None else headers,
                                               json.dumps(body).encode() if body is not None else None)
        return status, json.loads(payload) if ctype.startswith("application/json") else payload

    def test_the_api_needs_the_token_and_this_machine(self):
        self.assertEqual(self.call("GET", "/board/api/scene?project=p-k")[0], 200)
        self.assertEqual(self.call("GET", "/board/api/scene?project=k", headers={"Host": "127.0.0.1:5555"})[0], 403)
        self.assertEqual(self.call("GET", f"/board/api/scene?project=k&token={self.b.token}",
                                   headers={"Host": "evil.example:5555"})[0], 403)  # DNS rebinding
        self.assertEqual(self.call("GET", f"/board/api/scene?project=k&token={self.b.token}",
                                   headers={"Host": "localhost:5555"})[0], 200)
        self.assertEqual(self.call("GET", "/board/api/scene")[0], 400)  # no project
        self.assertEqual(self.call("GET", "/board/api/scene?project=../../x")[0], 400)

    def test_the_manager_and_scratch_have_no_board(self):
        """A window opened while the manager was in front would show a board no line can see: the page says why."""
        for project, name in (("manager", "总机"), ("scratch", "杂项")):
            status, r = self.call("GET", f"/board/api/events?project={project}&since=0&wait=0")
            self.assertEqual(status, 404)
            self.assertTrue(r["message"].startswith(f"{name}没有白板"), r)
        self.assertEqual(self.b.open_keys(), [])

    def test_scene_round_trip_and_events(self):
        status, r = self.call("GET", "/board/api/scene?project=p-k")
        self.assertEqual((r["version"], r["scene"]["elements"], r["seq"]), (0, [], 0))
        status, r = self.call("POST", "/board/api/scene", {"project": "p-k", "scene": {"elements": [{"id": "u1"}]}, "version": 0})
        self.assertEqual((status, r["version"]), (200, 1))
        self.b.add("k", board.card("一", "", 0, 0))
        status, r = self.call("GET", "/board/api/events?project=p-k&since=0&wait=0")
        self.assertEqual([e["kind"] for e in r["events"]], ["added"])

    def test_a_snapshot_asked_by_a_tool_and_sent_by_the_page(self):
        self.assertIsNone(self.b.snapshot("k", wait=0.1))  # no page: nothing to ask

        def page():
            ev = self.b.wait_events("k", 0, wait=3)
            req = next(e for e in ev["events"] if e["kind"] == "snapshot")["request"]
            self.call("POST", "/board/api/snapshot", {"project": "p-k", "request": req,
                                                      "png": "data:image/png;base64," + base64.b64encode(png(4, 4)).decode(),
                                                      "texts": ["用户写的字"]})

        self.b.last_poll["k"] = self.b.now()  # the page asked for events a moment ago
        t = threading.Thread(target=page)
        t.start()
        snap = self.b.snapshot("k", wait=3)
        t.join()
        self.assertTrue(Path(snap["png"]).read_bytes().startswith(b"\x89PNG"))
        self.assertEqual(Path(snap["txt"]).read_text("utf-8"), "用户写的字\n")
        self.assertEqual(snap["texts"], ["用户写的字"])

    def test_the_users_own_look_is_handed_on(self):
        """The window's 让它看 button sends request "user": no tool waits for it, the switchboard is told."""
        got = []
        done = threading.Event()
        self.b.on_user_snapshot = lambda key, snap: (got.append((key, snap)), done.set())
        status, r = self.call("POST", "/board/api/snapshot", {"project": "p-k", "request": "user",
                                                              "png": base64.b64encode(png(2, 2)).decode(), "texts": ["圈"]})
        self.assertEqual(status, 200)
        self.assertTrue(done.wait(3))
        self.assertEqual((got[0][0], got[0][1]["texts"], got[0][1]["png"]), ("k", ["圈"], r["png"]))
        self.assertEqual(self.b.snapshots, {})

    def test_static_files_only_from_the_page_folder(self):
        page = Path(tempfile.mkdtemp()) / "whiteboard"
        page.mkdir()
        (page / "index.html").write_text("<html>白板</html>", "utf-8")
        (page.parent / "secret.txt").write_text("no", "utf-8")
        with mock.patch.object(board, "STATIC", page):
            status, ctype, body = self.b.handle("GET", "/board/", {}, None)
            self.assertEqual((status, ctype), (200, "text/html; charset=utf-8"))
            self.assertIn("白板".encode(), body)
            self.assertEqual(self.b.handle("GET", "/board/../secret.txt", {}, None)[0], 404)
            self.assertEqual(self.b.handle("GET", "/board/%2e%2e/secret.txt", {}, None)[0], 404)

    def test_through_the_control_server(self):
        server = ControlServer(lambda path, data: {"ok": True, "message": path}, web=self.b.handle)
        self.b.port = server.port
        try:
            req = urllib.request.Request(f"http://127.0.0.1:{server.port}/board/api/scene?project=p-k",
                                         headers={"X-Board-Token": self.b.token})
            with urllib.request.urlopen(req, timeout=5) as r:
                self.assertEqual(json.load(r)["version"], 0)
            req = urllib.request.Request(f"http://127.0.0.1:{server.port}/record", data=b"{}", method="POST",
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=5) as r:
                self.assertEqual(json.load(r)["message"], "record")  # the tools' requests as before
            for tool in ("board_show", "board_look", "board_clear", "boardx"):  # tools' requests, not the page's
                req = urllib.request.Request(f"http://127.0.0.1:{server.port}/{tool}", data=b"{}", method="POST",
                                             headers={"Content-Type": "application/json"})
                with urllib.request.urlopen(req, timeout=5) as r:
                    self.assertEqual(json.load(r)["message"], tool)
            with urllib.request.urlopen(f"http://127.0.0.1:{server.port}/board?project=p-k", timeout=5) as r:
                self.assertEqual(r.status, 200) if board.STATIC.joinpath("index.html").exists() else None
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 404)  # no page folder in this tree: still the page's route
        finally:
            server.close()


if __name__ == "__main__":
    unittest.main()

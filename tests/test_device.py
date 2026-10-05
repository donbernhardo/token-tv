import json
import io
import tempfile
import threading
import unittest
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse
from urllib.error import HTTPError
from unittest.mock import patch

from token_tv.device import PhotoDisplay


class DisplayTests(unittest.TestCase):
    def test_capture_does_not_guess_after_network_or_server_failures(self):
        for error in (OSError('offline'), HTTPError('http://clock/theme/list', 503, 'Unavailable', {}, io.BytesIO())):
            display = PhotoDisplay('http://clock')
            with patch.object(display, 'request', side_effect=error) as request:
                with self.assertRaises(OSError):
                    display.capture()
            self.assertIsNone(display._kind)
            request.assert_called_once_with('/theme/list')
            if isinstance(error, HTTPError):
                error.close()

    def test_geekmagic_capture_requires_complete_readable_settings(self):
        for failed_path in ('/app.json', '/album.json', '/img.json'):
            display = PhotoDisplay('http://clock')
            def request(path):
                if path == '/theme/list':
                    raise HTTPError('http://clock/theme/list', 404, 'Not found', {}, io.BytesIO())
                if path == failed_path:
                    raise OSError('read failed')
                return 200, json.dumps({'theme': 1, 'autoplay': 1, 'i_i': 5, 'img': '/image/original.jpg'}).encode()
            with patch.object(display, 'request', side_effect=request):
                with self.assertRaises(OSError):
                    display.capture()
            self.assertIsNone(display._kind)

    def test_failed_backup_prevents_upload_activation_and_backup_file(self):
        from token_tv import live
        from token_tv.state import UsageStore
        account = {'key': 'a', 'alias': 'A', 'provider': 'codex', 'email': 'a@example.com'}
        store = UsageStore([account], fetch=lambda a: dict(a, status='ok', windows=[], fetched_at=1))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / 'config.json'
            config.write_text(json.dumps({'accounts': [account], 'device_url': 'http://clock'}))
            display = PhotoDisplay('http://clock')
            with patch.object(live, 'UsageStore', return_value=store), \
                    patch.object(live, 'PhotoDisplay', return_value=display), \
                    patch.object(display, 'is_alive', return_value=True), \
                    patch.object(display, 'capture', side_effect=OSError('read failed')), \
                    patch.object(display, 'upload') as upload, patch.object(display, 'activate') as activate, \
                    patch('sys.argv', ['live', '--config', str(config), '--state-dir', str(root / 'state'),
                                       '--once', '--push', '--output', str(root / 'snapshot.json')]):
                live.main()
            upload.assert_not_called()
            activate.assert_not_called()
            self.assertEqual(list((root / 'state').glob('display-original*.json')), [])
            receipt = json.loads((root / 'state' / 'display-receipt.json').read_text())['devices'][0]
            self.assertEqual((receipt['status'], receipt['phase']), ('error', 'backup'))

    def test_live_publisher_sends_each_rotating_page(self):
        from token_tv import live
        from token_tv.display import render_page
        from token_tv.state import UsageStore
        accounts = [{'key': p, 'alias': p.upper() + ' A', 'provider': p, 'email': 'a@example.com'}
                    for p in ('claude', 'codex', 'grok', 'gemini')]
        store = UsageStore(accounts, fetch=lambda a: dict(a, status='ok', windows=[{'label': '5H', 'used_percent': 42}], fetched_at=1))
        store.refresh()
        original = {'device_kind': 'geekmagic', 'theme': 1, 'autoplay': 1, 'i_i': 5, 'img': '/image/original.jpg'}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / 'config.json'
            config.write_text(json.dumps({'accounts': accounts, 'device_url': 'http://clock'}))
            display = PhotoDisplay('http://clock')
            with patch.object(live, 'UsageStore', return_value=store), \
                    patch.object(live, 'PhotoDisplay', return_value=display), \
                    patch.object(display, 'is_alive', return_value=True), \
                    patch.object(display, 'capture', return_value=original), \
                    patch.object(display, 'upload', return_value={'status': 200}) as upload, \
                    patch.object(display, 'activate'), \
                    patch('sys.argv', ['live', '--config', str(config), '--state-dir', str(root / 'state'),
                                       '--once', '--push', '--output', str(root / 'snapshot.json')]):
                for epoch in (0, 10):
                    with patch('token_tv.live.time.time', return_value=epoch):
                        live.main()
                        self.assertEqual(upload.call_args.args[1], render_page(store.snapshot(), epoch // 10))
            self.assertEqual(upload.call_count, 2)

    def test_upload_contract_preserves_existing_files_and_restores_theme(self):
        calls = []
        themes = {"interval": 10, "themes": [{"id": 0, "enabled": True}, {"id": 2, "enabled": False}]}
        photos = {"interval": 10, "files": [{"name": "original.jpg", "size": 10, "enabled": True}, {"name": "space man.gif", "size": 20, "enabled": True}], "total": 3000000, "used": 1000000}
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                return
            def do_GET(self):
                calls.append(("GET", self.path, b""))
                url = urlparse(self.path)
                if url.path == "/photo/toggle":
                    query = parse_qs(url.query)
                    for photo in photos["files"]:
                        if photo["name"] == query["name"][0]:
                            photo["enabled"] = query["state"][0] == "1"
                if url.path == "/theme/toggle":
                    query = parse_qs(url.query)
                    identifier, enabled = int(query["id"][0]), query["state"][0] == "1"
                    proposed = [dict(t, enabled=enabled) if t["id"] == identifier else t for t in themes["themes"]]
                    if not any(t["enabled"] for t in proposed):
                        self.send_response(403)
                        self.end_headers()
                        return
                    themes["themes"] = proposed
                data = themes if self.path == "/theme/list" else photos if self.path == "/photo/list" else {"ok": True}
                body = json.dumps(data).encode()
                self.send_response(200)
                self.end_headers()
                self.wfile.write(body)
            def do_POST(self):
                body = self.rfile.read(int(self.headers["Content-Length"]))
                calls.append(("POST", self.path, body))
                self.send_response(200)
                self.end_headers()
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        try:
            display = PhotoDisplay(f"http://127.0.0.1:{server.server_port}")
            original = display.capture()
            photos["files"].extend([{"name": name, "enabled": True} for name in ("tokentv-c.jpg", "tokentv-m.jpg", "tokentv.jpg")])
            display.upload("tokentv.jpg", b"EXACT-JPEG")
            display.activate(original)
            self.assertEqual([p["name"] for p in photos["files"] if p["enabled"]], ["tokentv.jpg"])
            # Switching to the animated face shows only the GIF; switching back restores the JPEG.
            photos["files"].append({"name": "tokentv.gif", "enabled": False})
            display.upload("tokentv.gif", b"GIF89a-EXACT")
            display.activate(original, "tokentv.gif")
            self.assertEqual([p["name"] for p in photos["files"] if p["enabled"]], ["tokentv.gif"])
            display.activate(original, "tokentv.jpg")
            self.assertEqual([p["name"] for p in photos["files"] if p["enabled"]], ["tokentv.jpg"])
            with self.assertRaises(ValueError):
                display.upload("other.gif", b"GIF89a")
            display.restore(original)
            self.assertEqual([p["name"] for p in photos["files"] if p["enabled"]], ["original.jpg", "space man.gif"])
            uploads = [c for c in calls if c[0] == "POST"]
            self.assertEqual(len(uploads), 2)
            self.assertEqual(uploads[0][1], "/photo/upload")
            self.assertIn(b'name="file"; filename="tokentv.jpg"', uploads[0][2])
            self.assertIn(b"\r\n\r\nEXACT-JPEG\r\n", uploads[0][2])
            self.assertIn(b'filename="tokentv.gif"\r\nContent-Type: image/gif', uploads[1][2])
            paths = [c[1] for c in calls]
            self.assertIn("/theme/toggle?id=2&state=1", paths)
            self.assertIn("/theme/toggle?id=0&state=0", paths)
            self.assertIn("/theme/toggle?id=0&state=1", paths)
            self.assertIn("/photo/toggle?name=original.jpg&state=1", paths)
            self.assertIn("/photo/toggle?name=space%20man.gif&state=0", paths)
            self.assertIn("/photo/toggle?name=space%20man.gif&state=1", paths)
            self.assertFalse(any("delete" in p or "restart" in p or "update" in p for p in paths))
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_geekmagic_upload_and_activate_and_restore(self):
        calls = []
        state = {"theme": 1, "autoplay": 1, "i_i": 5, "img": "/image/original.jpg"}

        class GeekMagicHandler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                return
            def do_GET(self):
                calls.append(("GET", self.path, b""))
                url = urlparse(self.path)
                if url.path == "/theme/list":
                    self.send_response(404)
                    self.end_headers()
                    return
                if url.path == "/v.json":
                    body = json.dumps({"m": "SmallTV-Ultra", "v": "Ultra-V9.0.43"}).encode()
                elif url.path == "/app.json":
                    body = json.dumps({"theme": state["theme"]}).encode()
                elif url.path == "/album.json":
                    body = json.dumps({"autoplay": state["autoplay"], "i_i": state["i_i"]}).encode()
                elif url.path == "/img.json":
                    body = json.dumps({"img": state["img"]}).encode()
                elif url.path == "/set":
                    query = parse_qs(url.query)
                    if "theme" in query:
                        state["theme"] = int(query["theme"][0])
                    if "autoplay" in query:
                        state["autoplay"] = int(query["autoplay"][0])
                    if "img" in query:
                        state["img"] = query["img"][0]
                    body = b"OK"
                else:
                    body = b"OK"
                self.send_response(200)
                self.end_headers()
                self.wfile.write(body)
            def do_POST(self):
                body = self.rfile.read(int(self.headers["Content-Length"]))
                calls.append(("POST", self.path, body))
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"OK")

        server = ThreadingHTTPServer(("127.0.0.1", 0), GeekMagicHandler)
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        try:
            display = PhotoDisplay(f"http://127.0.0.1:{server.server_port}")
            original = display.capture()
            self.assertEqual(original["device_kind"], "geekmagic")
            self.assertEqual(original["theme"], 1)
            self.assertEqual(original["autoplay"], 1)

            display.upload("tokentv.jpg", b"EXACT-JPEG")
            display.activate(original, "tokentv.jpg")
            self.assertEqual(state["theme"], 3)
            self.assertEqual(state["autoplay"], 0)
            self.assertEqual(state["img"], "/image/tokentv.jpg")

            display.restore(original)
            self.assertEqual(state["theme"], 1)
            self.assertEqual(state["autoplay"], 1)
            self.assertEqual(state["img"], "/image/original.jpg")

            uploads = [c for c in calls if c[0] == "POST"]
            self.assertEqual(len(uploads), 1)
            self.assertEqual(uploads[0][1], "/doUpload?dir=/image/")
            self.assertIn(b'name="file"; filename="tokentv.jpg"', uploads[0][2])
            self.assertIn(b"\r\n\r\nEXACT-JPEG\r\n", uploads[0][2])
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_is_alive_online_and_offline(self):
        class PingHandler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                return
            def do_GET(self):
                self.send_response(200)
                self.end_headers()

        server = ThreadingHTTPServer(("127.0.0.1", 0), PingHandler)
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        try:
            port = server.server_port
            display = PhotoDisplay(f"http://127.0.0.1:{port}")
            self.assertTrue(display.is_alive(timeout=1.0))
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

        offline_display = PhotoDisplay(f"http://127.0.0.1:{port}")
        self.assertFalse(offline_display.is_alive(timeout=0.2))


if __name__ == "__main__":
    unittest.main()

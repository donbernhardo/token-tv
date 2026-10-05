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
    def test_pro_detection_missing_album_settings_and_dedicated_album(self):
        files = {'/image/old photo.jpg', '/image/tokentv.jpg', '/image/tokentv.gif'}
        calls = []
        display = PhotoDisplay('http://clock')

        def request(path, data=None, headers=None):
            calls.append(path)
            url = urlparse(path)
            if path == '/v.json':
                return 200, b'{"m":"GeekMagic SmallTV-PRO","v":"V3.3.75EN"}'
            if path == '/.sys/app.json':
                return 200, b'{"theme":"3"}'
            if url.path == '/filelist':
                return 200, ''.join(f'<a href="{file.replace("/image/", "/image//")}">photo</a>' for file in files).encode()
            if url.path == '/delete':
                files.discard(parse_qs(url.query)['file'][0].replace('/image//', '/image/'))
                return 200, b'FAIL'  # Real PRO firmware deletes successfully but replies FAIL.
            if url.path in ('/set', '/doUpload'):
                return 200, b'OK'
            if path == '/image/tokentv.jpg':
                return 200, b'EXACT-JPEG'
            raise HTTPError('http://clock' + path, 404, 'Not found', {}, io.BytesIO())

        with patch.object(display, 'request', side_effect=request):
            original = display.capture()
            self.assertEqual(original, {'device_kind': 'geekmagic_pro', 'theme': 3})
            display.upload('tokentv.jpg', b'EXACT-JPEG')
            display.activate(original)
            self.assertEqual(files, {'/image/tokentv.jpg'})
            self.assertIn('/set?theme=4', calls)
            self.assertIn('/set?i_i=1&gif_loop=1&autoplay=1', calls)
            self.assertIn('/delete?file=%2Fimage%2F%2Fold%20photo.jpg', calls)
            self.assertNotIn('/.sys/album.json', calls)
            display.restore(original)
            self.assertEqual(calls[-1], '/set?theme=3')

    def test_pro_missing_uploaded_file_prevents_deleting_other_photos(self):
        display = PhotoDisplay('http://clock')
        display._kind = 'geekmagic_pro'
        with patch.object(display, 'request', return_value=(200, b'<a href="/image/old.jpg">old</a>')) as request:
            with self.assertRaises(ValueError):
                display.activate({'device_kind': 'geekmagic_pro'})
            request.assert_called_once_with('/filelist?dir=/image/')

    def test_pro_closed_upload_requires_exact_content_verification(self):
        import http.client
        for stored in (b'EXACT-JPEG', b'OLD-JPEG'):
            display = PhotoDisplay('http://clock')
            display._kind = 'geekmagic_pro'
            with patch.object(display, 'request', side_effect=[http.client.RemoteDisconnected(), (200, stored)]):
                if stored == b'EXACT-JPEG':
                    self.assertEqual(display.upload('tokentv.jpg', stored)['status'], 200)
                else:
                    with self.assertRaises(ValueError):
                        display.upload('tokentv.jpg', b'EXACT-JPEG')

    def test_pro_rejected_setting_does_not_report_success(self):
        display = PhotoDisplay('http://clock')
        display._kind = 'geekmagic_pro'
        with patch.object(display, 'image_paths', return_value={'/image/tokentv.jpg': '/image/tokentv.jpg'}), \
                patch.object(display, 'image_files', return_value={'/image/tokentv.jpg'}), \
                patch.object(display, 'request', return_value=(200, b'FAIL')):
            with self.assertRaises(ValueError):
                display.activate({'device_kind': 'geekmagic_pro'})

    def test_pro_failed_album_cleanup_prevents_mode_change(self):
        display = PhotoDisplay('http://clock')
        display._kind = 'geekmagic_pro'
        paths = {'/image/tokentv.jpg': '/image/tokentv.jpg', '/image/old.jpg': '/image/old.jpg'}
        with patch.object(display, 'image_paths', return_value=paths), \
                patch.object(display, 'request', return_value=(200, b'FAIL')) as request:
            with self.assertRaises(ValueError):
                display.activate({'device_kind': 'geekmagic_pro'})
            request.assert_called_once_with('/delete?file=%2Fimage%2Fold.jpg')

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

    def test_upload_contract_clears_album_and_restores_theme(self):
        calls = []
        themes = {"interval": 10, "themes": [{"id": 0, "enabled": True}, {"id": 2, "enabled": False}]}
        photos = {"interval": 10, "files": [{"name": "original.jpg", "size": 10, "enabled": True}, {"name": "space man.gif", "size": 20, "enabled": True}], "total": 3000000, "used": 1000000}
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                return
            def do_GET(self):
                calls.append(("GET", self.path, b""))
                url = urlparse(self.path)
                if url.path == "/photo/delete":
                    query = parse_qs(url.query)
                    photos["files"] = [f for f in photos["files"] if f["name"] != query["name"][0]]
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
            # Switching faces removes the previous frame from the dedicated album.
            photos["files"].append({"name": "tokentv.gif", "enabled": False})
            display.upload("tokentv.gif", b"GIF89a-EXACT")
            display.activate(original, "tokentv.gif")
            self.assertEqual([p["name"] for p in photos["files"] if p["enabled"]], ["tokentv.gif"])
            photos["files"].append({"name": "tokentv.jpg", "enabled": False})
            display.upload("tokentv.jpg", b"EXACT-JPEG")
            display.activate(original, "tokentv.jpg")
            self.assertEqual([p["name"] for p in photos["files"]], ["tokentv.jpg"])
            with self.assertRaises(ValueError):
                display.upload("other.gif", b"GIF89a")
            display.restore(original)
            self.assertEqual([p["name"] for p in photos["files"] if p["enabled"]], [])
            uploads = [c for c in calls if c[0] == "POST"]
            self.assertEqual(len(uploads), 3)
            self.assertEqual(uploads[0][1], "/photo/upload")
            self.assertIn(b'name="file"; filename="tokentv.jpg"', uploads[0][2])
            self.assertIn(b"\r\n\r\nEXACT-JPEG\r\n", uploads[0][2])
            self.assertIn(b'filename="tokentv.gif"\r\nContent-Type: image/gif', uploads[1][2])
            paths = [c[1] for c in calls]
            self.assertIn("/theme/toggle?id=2&state=1", paths)
            self.assertIn("/theme/toggle?id=0&state=0", paths)
            self.assertIn("/theme/toggle?id=0&state=1", paths)
            self.assertIn("/photo/delete?name=original.jpg", paths)
            self.assertIn("/photo/delete?name=space%20man.gif", paths)
            self.assertFalse(any("restart" in p or "update" in p for p in paths))
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_geekmagic_upload_and_activate_and_restore(self):
        calls = []
        state = {"theme": 1, "autoplay": 1, "i_i": 5, "img": "/image/original.jpg"}
        files = {"original.jpg", "tokentv.jpg"}

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
                elif url.path == "/filelist":
                    body = "".join(f"<a href='/image/{name}'>{name}</a>" for name in files).encode()
                elif url.path == "/delete":
                    files.discard(parse_qs(url.query)["file"][0].rsplit("/", 1)[-1])
                    body = b"OK"
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
            self.assertEqual(state["img"], "/image/tokentv.jpg")
            self.assertEqual(files, {"tokentv.jpg"})

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

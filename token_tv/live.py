"""Live account dashboard and stock-firmware image publisher."""
import argparse
import hashlib
import json
import os
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from token_tv.device import FILES, PhotoDisplay
from token_tv.catalog import payload as theme_payload
from token_tv.display import PAGE_SECONDS, STYLES, pages, render_page
from token_tv.web_assets import HTML, ASSETS, asset
from token_tv.sources import load_config, normalize_device_targets
from token_tv.state import UsageStore


def write_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")))
    os.chmod(temporary, 0o600)
    temporary.replace(path)


class DisplayPreferences:
    def __init__(self, config_path, config):
        self.path = Path(config_path)
        self.config = dict(config)
        self.style = config.get('display_style', 'pixel')
        self.applied_style = None
        self.targets = normalize_device_targets(config)
        has_devices = bool(self.targets)
        self.status = 'queued' if has_devices else 'preview_only'
        self.device_statuses = {
            t["url"]: {
                "url": t["url"],
                "name": t.get("name") or t["url"],
                "style": t["style"],
                "configured_style": t.get("configured_style"),
                "status": "queued" if has_devices else "preview_only",
                "last_seen": None,
                "last_delivery": None,
            }
            for t in self.targets
        }
        self.lock = threading.Lock()
        self.changed = threading.Event()

    def snapshot(self):
        with self.lock:
            return {
                'style': self.style,
                'applied_style': self.applied_style,
                'status': self.status,
                'styles': list(STYLES),
                'targets': list(self.device_statuses.values()),
            }

    def set_style(self, style):
        if style not in STYLES:
            raise ValueError('Unknown display style')
        with self.lock:
            updated = dict(load_config(self.path), display_style=style)
            write_json(self.path, updated)
            self.config = updated
            self.style = style
            for dev in self.device_statuses.values():
                if not dev.get("configured_style"):
                    dev["style"] = style
            self.status = 'queued' if self.targets else 'preview_only'
        self.changed.set()

    def update_target(self, url, status, applied_style=None):
        with self.lock:
            if url in self.device_statuses:
                dev = self.device_statuses[url]
                dev["status"] = status
                now = int(time.time())
                if status in ("ok", "alive", "error"):
                    dev["last_seen"] = now
                if status == "ok":
                    dev["last_delivery"] = now
                    if applied_style:
                        dev["style"] = applied_style
            statuses = [d["status"] for d in self.device_statuses.values()]
            if not statuses:
                self.status = "preview_only"
            elif any(s == "ok" for s in statuses):
                self.status = "ok"
                self.applied_style = self.style
            elif all(s == "offline" for s in statuses):
                self.status = "offline"
            elif any(s == "queued" for s in statuses):
                self.status = "queued"
            else:
                self.status = "error"

    def delivered(self, style, success):
        with self.lock:
            if success:
                self.applied_style = style
            self.status = ('queued' if style != self.style else 'ok' if success else 'error')


def handler(store, preferences=None):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            return

        def do_GET(self):
            url = urlparse(self.path)
            path = url.path
            snapshot = store.snapshot()
            display = preferences.snapshot() if preferences else {'style': 'pixel', 'status': 'preview_only', 'styles': list(STYLES)}
            display['page_count'] = len(pages(snapshot))
            display['page_seconds'] = PAGE_SECONDS
            snapshot['display'] = display
            content_type = "application/json; charset=utf-8"
            if path == "/":
                body = HTML.encode()
                content_type = "text/html; charset=utf-8"
            elif path in ASSETS:
                body, content_type = asset(path)
            elif path == "/snapshot":
                body = json.dumps(snapshot, ensure_ascii=False, separators=(",", ":")).encode()
            elif path.startswith("/snapshot/") and path[10:] in snapshot["accounts"]:
                body = json.dumps(snapshot["accounts"][path[10:]], ensure_ascii=False, separators=(",", ":")).encode()
            elif path in ("/frame/0.jpg", "/frame/1.jpg"):
                style = parse_qs(url.query).get('style', [display['style']])[0]
                if style not in STYLES:
                    self.send_error(400, 'Unknown display style')
                    return
                body = render_page(snapshot, None if path[7] == '0' else 1, style)
                content_type = "image/gif" if body[:4] == b"GIF8" else "image/jpeg"
            elif path == '/themes':
                body = json.dumps(theme_payload()).encode()
            elif path == '/display':
                body = json.dumps(display).encode()
            elif path == "/health":
                body = json.dumps({"status": "ok", "updated_at": snapshot["updated_at"]}).encode()
            else:
                self.send_error(404)
                return
            self.reply(200, body, content_type)

        def reply(self, status, body, content_type='application/json; charset=utf-8'):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            if urlparse(self.path).path != '/display/style':
                self.send_error(404)
                return
            if preferences is None:
                self.send_error(409, 'Display settings unavailable')
                return
            origin = self.headers.get('Origin')
            if origin and (urlparse(origin).scheme not in ('http', 'https') or urlparse(origin).netloc != self.headers.get('Host')):
                self.send_error(403, 'Origin mismatch')
                return
            try:
                length = int(self.headers.get('Content-Length', '0'))
                if not 0 < length <= 1024 or self.headers.get('Content-Type', '').split(';')[0] != 'application/json':
                    raise ValueError('Invalid JSON request')
                data = json.loads(self.rfile.read(length))
                if not isinstance(data, dict) or set(data) != {'style'} or data['style'] not in STYLES:
                    raise ValueError('Unknown display style')
                preferences.set_style(data['style'])
            except (ValueError, UnicodeError):
                self.send_error(400, 'Choose a supported display style')
                return
            except OSError:
                self.send_error(500, 'Could not save display choice')
                return
            self.reply(200, json.dumps(preferences.snapshot()).encode())
    return Handler


def main():
    parser = argparse.ArgumentParser(description="TokenTV live account display")
    parser.add_argument("--config", required=True, help="Credential-free account metadata")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8788)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--push", action="store_true", help="Push to device display even with --once")
    parser.add_argument("--output", help="Write a normalized snapshot rather than stdout")
    parser.add_argument("--state-dir", default=".runtime")
    parser.add_argument("--restore-display", action="store_true")
    args = parser.parse_args()
    config = load_config(args.config)
    if config.get("font"):
        os.environ["TOKEN_TV_FONT"] = config["font"]
    state_dir = Path(args.state_dir)
    targets = normalize_device_targets(config)
    devices = {t["url"]: PhotoDisplay(t["url"]) for t in targets}
    backup_path = state_dir / "display-original.json"
    if args.restore_display:
        restored = 0
        for target in targets:
            url = target["url"]
            host_slug = re.sub(r'[^a-zA-Z0-9_-]', '_', urlparse(url).netloc)
            dev_backup = state_dir / f"display-original-{host_slug}.json"
            if not dev_backup.is_file() and backup_path.is_file():
                dev_backup = backup_path
            if dev_backup.is_file():
                devices[url].restore(json.loads(dev_backup.read_text()))
                restored += 1
        if not restored:
            parser.error("A display and its original state backup are required")
        print(f"Original display selection restored on {restored} display(s).")
        return
    store = UsageStore(config["accounts"])
    preferences = DisplayPreferences(args.config, config)
    interval = max(60, int(config.get("poll_seconds", 300)))
    stopping = threading.Event()
    backups = {}
    active_files = {}
    last_uploads = {}

    def cycle(refresh=True):
        nonlocal active_files, last_uploads, backups
        if refresh:
            store.refresh()
        snapshot = store.snapshot()
        pref_snap = preferences.snapshot()
        if (not args.once or args.push) and targets:
            all_receipts = []
            for target in targets:
                url = target["url"]
                dev = devices[url]
                target_style = target.get("configured_style") or pref_snap['style']
                if not dev.is_alive(timeout=2.0):
                    preferences.update_target(url, "offline")
                    all_receipts.append({"url": url, "style": target_style, "status": "offline"})
                    continue

                receipts = []
                phase = "backup"
                try:
                    host_slug = re.sub(r'[^a-zA-Z0-9_-]', '_', urlparse(url).netloc)
                    dev_backup = state_dir / f"display-original-{host_slug}.json"
                    orig = backups.get(url)
                    if orig is None:
                        if dev_backup.is_file():
                            orig = json.loads(dev_backup.read_text())
                        elif backup_path.is_file():
                            orig = json.loads(backup_path.read_text())
                        else:
                            orig = dev.capture()
                            if orig and not dev_backup.is_file():
                                write_json(dev_backup, orig)
                        backups[url] = orig

                    phase = "upload"
                    image = render_page(snapshot, None, target_style)
                    name = FILES[1] if image[:4] == b"GIF8" else FILES[0]
                    digest = hashlib.sha256(image).hexdigest()
                    if (name, digest) != last_uploads.get(url):
                        (state_dir / f"{host_slug}-{name}").write_bytes(image)
                        receipts.append(dict(dev.upload(name, image), sha256=digest))
                        last_uploads[url] = (name, digest)
                    if active_files.get(url) != name:
                        phase = "activate"
                        dev.activate(orig, name)
                        active_files[url] = name
                    preferences.update_target(url, "ok", applied_style=target_style)
                    all_receipts.append({"url": url, "style": target_style, "status": "ok", "uploads": receipts})
                except (OSError, ValueError, KeyError, TypeError) as error:
                    preferences.update_target(url, "error")
                    all_receipts.append({"url": url, "style": target_style, "status": "error", "phase": phase,
                                         "uploads": receipts, "http_status": getattr(error, "code", None)})
            write_json(state_dir / "display-receipt.json", {"at": int(time.time()), "devices": all_receipts})
        snapshot['display'] = preferences.snapshot()
        write_json(state_dir / "snapshot.json", snapshot)
        if args.output:
            write_json(args.output, snapshot)

    if args.once:
        cycle()
        if not args.output:
            print(json.dumps(store.snapshot(), ensure_ascii=False, separators=(",", ":")))
        return

    def poll():
        next_refresh = 0
        while not stopping.is_set():
            preferences.changed.clear()
            refresh = time.monotonic() >= next_refresh
            try:
                cycle(refresh)
            except Exception:
                # Never serialize exception text, which may include auth material.
                print("TokenTV poll failed; retained previous snapshot.", flush=True)
            if refresh:
                next_refresh = time.monotonic() + interval
            wait = max(0, next_refresh - time.monotonic())
            if targets and len(pages(store.snapshot())) > 1:
                wait = min(wait, PAGE_SECONDS - time.time() % PAGE_SECONDS)
            preferences.changed.wait(wait)

    thread = threading.Thread(target=poll, daemon=True)
    thread.start()
    try:
        with ThreadingHTTPServer((args.host, args.port), handler(store, preferences)) as server:
            print(f"TokenTV live listening at http://{args.host}:{args.port}", flush=True)
            server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stopping.set()
        preferences.changed.set()
        thread.join(timeout=2)


if __name__ == "__main__":
    main()

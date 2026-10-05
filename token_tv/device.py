"""Confirmed SD_PRO photo API. Never flash or delete existing photographs."""
import json
import uuid
from urllib.parse import quote, urlencode, urlparse
from urllib.request import Request, urlopen


FILES = ("tokentv.jpg", "tokentv.gif")
# The album page accepts GIFs up to 240×240; keep both well under the free space.
LIMITS = {"tokentv.jpg": (60000, "image/jpeg"), "tokentv.gif": (400000, "image/gif")}
LEGACY_FILES = ("tokentv-c.jpg", "tokentv-m.jpg")


class PhotoDisplay:
    def __init__(self, base_url):
        parsed = urlparse(base_url)
        if parsed.scheme != "http" or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.path not in ("", "/"):
            raise ValueError("A plain local display URL is required")
        self.base_url = base_url.rstrip("/")
        self._kind = None

    def request(self, path, data=None, headers=None):
        with urlopen(Request(self.base_url + path, data=data, headers=headers or {}), timeout=20) as response:
            body = response.read()
            return response.status, body

    def is_alive(self, timeout=2.0):
        """Check if the display's HTTP server is reachable and responding."""
        try:
            with urlopen(Request(self.base_url + "/"), timeout=timeout) as response:
                return response.status in (200, 204, 301, 302, 404)
        except Exception:
            return False

    def _detect(self):
        if self._kind is not None:
            return self._kind
        try:
            status, raw = self.request("/theme/list")
            if status == 200:
                themes = json.loads(raw)
                if isinstance(themes, dict) and "themes" in themes:
                    self._kind = "sd_pro"
                    return self._kind
        except Exception:
            pass
        self._kind = "geekmagic"
        return self._kind

    def capture(self):
        try:
            status, raw = self.request("/theme/list")
            if status == 200:
                themes = json.loads(raw)
                if isinstance(themes, dict) and "themes" in themes:
                    self._kind = "sd_pro"
                    _, praw = self.request("/photo/list")
                    photos = json.loads(praw)
                    if not any(t["id"] == 2 for t in themes["themes"]):
                        raise ValueError("The confirmed photo theme is missing")
                    return {"device_kind": "sd_pro", "themes": themes["themes"], "theme_interval": themes["interval"],
                            "files": [{"name": f["name"], "enabled": f["enabled"]} for f in photos["files"]],
                            "photo_interval": photos["interval"]}
        except (ValueError, KeyError):
            raise
        except Exception:
            pass

        self._kind = "geekmagic"
        theme = 3
        try:
            _, raw = self.request("/app.json")
            theme = json.loads(raw).get("theme", 3)
        except Exception:
            pass
        autoplay = 0
        interval = 5
        try:
            _, raw = self.request("/album.json")
            data = json.loads(raw)
            autoplay = data.get("autoplay", 0)
            interval = data.get("i_i", 5)
        except Exception:
            pass
        img = ""
        try:
            _, raw = self.request("/img.json")
            img = json.loads(raw).get("img", "")
        except Exception:
            pass
        return {
            "device_kind": "geekmagic",
            "theme": theme,
            "autoplay": autoplay,
            "i_i": interval,
            "img": img,
            "themes": [{"id": 3, "enabled": True}],
            "files": [{"name": FILES[0], "enabled": True}],
            "photo_interval": interval,
            "theme_interval": interval,
        }

    def upload(self, name, image):
        limit, mime = LIMITS.get(name, (0, ""))
        if not image or len(image) > limit:
            raise ValueError("Unexpected display image")
        boundary = "TokenTV" + uuid.uuid4().hex
        body = (f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{name}"\r\n'
                f'Content-Type: {mime}\r\n\r\n').encode() + image + f"\r\n--{boundary}--\r\n".encode()
        path = "/doUpload?dir=/image/" if (self._kind or self._detect()) == "geekmagic" else "/photo/upload"
        status, _ = self.request(path, body,
                                 {"Content-Type": "multipart/form-data; boundary=" + boundary})
        return {"file": name, "status": status, "bytes": len(image)}

    def toggle(self, category, key, value, enabled):
        self.request("/" + category + "/toggle?" + urlencode({key: value, "state": int(enabled)}, quote_via=quote))

    def activate(self, original, name=FILES[0]):
        if (self._kind or (original and original.get("device_kind"))) == "geekmagic":
            self.request("/set?i_i=5&autoplay=0")
            self.request(f"/set?img=/image/{name}")
            self.request("/set?theme=3")
            return
        # Show only our current file, then select the photo theme; retain old files.
        current = self.capture()
        enabled = {f['name']: f['enabled'] for f in current['files']}
        if not enabled.get(name):
            self.toggle("photo", "name", name, True)
        for f in current['files']:
            if f["name"] != name and f['enabled']:
                self.toggle("photo", "name", f["name"], False)
        if current['photo_interval'] != 10:
            self.request("/photo/interval?val=10")
        if not any(t['id'] == 2 and t['enabled'] for t in current['themes']):
            self.toggle("theme", "id", 2, True)
        for t in current["themes"]:
            if t["id"] != 2 and t['enabled']:
                self.toggle("theme", "id", t["id"], False)

    def restore(self, original):
        if original.get("device_kind") == "geekmagic":
            theme = original.get("theme", 1)
            self.request(f"/set?theme={theme}")
            if "autoplay" in original:
                interval = original.get("i_i", 5)
                self.request(f"/set?i_i={interval}&autoplay={original['autoplay']}")
            if original.get("img"):
                self.request(f"/set?img={original['img']}")
            return
        for f in original["files"]:
            self.toggle("photo", "name", f["name"], f["enabled"])
        _, raw = self.request("/photo/list")
        present = {f["name"] for f in json.loads(raw)["files"]}
        for name in FILES + LEGACY_FILES:
            if name in present and not any(f["name"] == name for f in original["files"]):
                self.toggle("photo", "name", name, False)
        self.request("/photo/interval?val=" + str(original["photo_interval"]))
        self.request("/theme/interval?val=" + str(original["theme_interval"]))
        for enabled in (True, False):
            for t in original["themes"]:
                if t["enabled"] == enabled:
                    self.toggle("theme", "id", t["id"], enabled)

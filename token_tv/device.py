"""Stock display APIs with a dedicated TokenTV photo album."""
import gzip
import http.client
import json
import uuid
from html.parser import HTMLParser
from urllib.error import HTTPError
from urllib.parse import quote, urlencode, urlparse
from urllib.request import Request, urlopen


FILES = ("tokentv.jpg", "tokentv.gif")
# The album page accepts GIFs up to 240×240; keep both well under the free space.
LIMITS = {"tokentv.jpg": (60000, "image/jpeg"), "tokentv.gif": (400000, "image/gif")}
LEGACY_FILES = ("tokentv-c.jpg", "tokentv-m.jpg")


class ImageFiles(HTMLParser):
    def __init__(self):
        super().__init__()
        self.paths = {}

    def handle_starttag(self, tag, attrs):
        if tag == 'a':
            href = dict(attrs).get('href', '')
            if href.startswith('/image/'):
                name = href[len('/image/'):].lstrip('/')
                if name and '/' not in name and name not in ('.', '..'):
                    self.paths['/image/' + name] = href


def device_address(text, allow_bare=False):
    """Validate the HTTP address understood by the stock firmware."""
    if not isinstance(text, str):
        raise ValueError("A plain HTTP clock address is required")
    url = text.strip()
    if allow_bare and not url.startswith(('http://', 'https://')):
        url = 'http://' + url
    parsed = urlparse(url)
    if (parsed.scheme != 'http' or not parsed.hostname or parsed.username is not None
            or parsed.password is not None or parsed.query or parsed.fragment
            or parsed.path not in ('', '/')):
        raise ValueError("A plain HTTP clock address is required, e.g. http://192.168.0.50")
    try:
        if parsed.port == 0:
            raise ValueError
    except ValueError:
        raise ValueError("The clock address has an invalid port") from None
    return url.rstrip('/')


class PhotoDisplay:
    def __init__(self, base_url):
        self.base_url = device_address(base_url)
        self._kind = None

    def request(self, path, data=None, headers=None):
        with urlopen(Request(self.base_url + path, data=data, headers=headers or {}), timeout=20) as response:
            body = response.read()
            if body[:2] == b'\x1f\x8b':
                body = gzip.decompress(body)
            return response.status, body

    def change(self, path):
        _, body = self.request(path)
        if body.strip().upper() in (b'FAIL', b'FAILED', b'ERROR'):
            raise ValueError('Display rejected ' + path.split('?')[0])

    def image_paths(self):
        _, body = self.request('/filelist?dir=/image/')
        parser = ImageFiles()
        parser.feed(body.decode('utf-8'))
        return parser.paths

    def image_files(self):
        return set(self.image_paths())

    def is_alive(self, timeout=2.0):
        """Check if the display's HTTP server is reachable and responding."""
        try:
            with urlopen(Request(self.base_url + "/"), timeout=timeout) as response:
                return response.status in (200, 204, 301, 302, 404)
        except Exception:
            return False

    def _detect(self):
        if self._kind is None:
            self.capture()
        return self._kind

    def capture(self):
        """Read original settings; PRO records only settings the firmware exposes."""
        try:
            _, raw = self.request("/theme/list")
        except HTTPError as error:
            if error.code != 404:
                raise
            error.close()
        else:
            themes = json.loads(raw)
            if not isinstance(themes, dict) or not isinstance(themes.get('themes'), list):
                raise ValueError("Unrecognized display firmware")
            _, praw = self.request("/photo/list")
            photos = json.loads(praw)
            if not isinstance(photos, dict) or not isinstance(photos.get('files'), list):
                raise ValueError("Invalid photo state")
            if (any(not isinstance(t, dict) or type(t.get('id')) is not int or type(t.get('enabled')) is not bool
                    for t in themes['themes'])
                    or any(not isinstance(f, dict) or not isinstance(f.get('name'), str)
                           or type(f.get('enabled')) is not bool for f in photos['files'])
                    or any(type(data.get('interval')) is not int or data['interval'] <= 0
                           for data in (themes, photos))):
                raise ValueError("Incomplete display settings")
            if not any(t["id"] == 2 for t in themes["themes"]):
                raise ValueError("The confirmed photo theme is missing")
            original = {"device_kind": "sd_pro", "themes": themes["themes"], "theme_interval": themes["interval"],
                        "files": [{"name": f["name"], "enabled": f["enabled"]} for f in photos["files"]],
                        "photo_interval": photos["interval"]}
            self._kind = "sd_pro"
            return original

        def read(path):
            _, raw = self.request(path)
            data = json.loads(raw)
            if not isinstance(data, dict):
                raise ValueError("Invalid display state")
            return data

        try:
            version = read('/v.json')
        except HTTPError as error:
            if error.code != 404:
                raise
            error.close()
            version = {}
        if version.get('m') == 'GeekMagic SmallTV-PRO':
            original = {'device_kind': 'geekmagic_pro'}
            try:
                app = read('/.sys/app.json')
            except HTTPError as error:
                if error.code != 404:
                    raise
                error.close()
            else:
                theme = app.get('theme')
                if isinstance(theme, str) and theme.isdecimal():
                    theme = int(theme)
                if type(theme) is not int or theme < 0:
                    raise ValueError('Invalid PRO theme')
                original['theme'] = theme
            # Album settings may not exist until saved. No invented defaults.
            self._kind = 'geekmagic_pro'
            return original

        app, album, image = read('/app.json'), read('/album.json'), read('/img.json')
        original = {"device_kind": "geekmagic", "theme": app['theme'],
                    "autoplay": album['autoplay'], "i_i": album['i_i'], "img": image['img']}
        if (any(type(original[k]) is not int for k in ('theme', 'autoplay', 'i_i'))
                or original['theme'] < 0 or original['autoplay'] not in (0, 1) or original['i_i'] <= 0
                or not isinstance(original['img'], str)):
            raise ValueError("Invalid display settings")
        self._kind = "geekmagic"
        return original

    def upload(self, name, image):
        limit, mime = LIMITS.get(name, (0, ""))
        if not image or len(image) > limit:
            raise ValueError("Unexpected display image")
        boundary = "TokenTV" + uuid.uuid4().hex
        body = (f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{name}"\r\n'
                f'Content-Type: {mime}\r\n\r\n').encode() + image + f"\r\n--{boundary}--\r\n".encode()
        kind = self._kind or self._detect()
        path = "/doUpload?dir=/image/" if kind in ('geekmagic', 'geekmagic_pro') else "/photo/upload"
        try:
            status, reply = self.request(path, body,
                                        {"Content-Type": "multipart/form-data; boundary=" + boundary})
            if reply.strip().upper() in (b'FAIL', b'FAILED', b'ERROR'):
                raise ValueError('Display rejected image upload')
        except (OSError, http.client.HTTPException):
            if kind != 'geekmagic_pro':
                raise
            # Some PRO firmwares close the connection after storing the file.
            _, stored = self.request('/image/' + name)
            if stored != image:
                raise ValueError('PRO upload could not be verified')
            status = 200
        else:
            if kind == 'geekmagic_pro':
                _, stored = self.request('/image/' + name)
                if stored != image:
                    raise ValueError('PRO upload could not be verified')
        return {"file": name, "status": status, "bytes": len(image)}

    def toggle(self, category, key, value, enabled):
        self.request("/" + category + "/toggle?" + urlencode({key: value, "state": int(enabled)}, quote_via=quote))

    def activate(self, original, name=FILES[0]):
        kind = self._kind or (original and original.get('device_kind')) or self._detect()
        if kind in ('geekmagic', 'geekmagic_pro'):
            files = self.image_paths()
            selected = '/image/' + name
            if selected not in files:
                raise ValueError('Uploaded TokenTV image is missing from the album')
            for path in sorted(set(files) - {selected}):
                # PRO can return FAIL even after deleting the file. Verify the
                # resulting album rather than trusting the response text.
                self.request('/delete?' + urlencode({'file': files[path]}, quote_via=quote))
            if self.image_files() != {selected}:
                raise ValueError('Display album cleanup failed')
            if kind == 'geekmagic_pro':
                self.change('/set?i_i=1&gif_loop=1&autoplay=1')
                self.change('/set?theme=4')
            else:
                self.change('/set?i_i=5&autoplay=0')
                self.change(f'/set?img={selected}')
                self.change('/set?theme=3')
            return
        # Each display keeps only the currently selected TokenTV image.
        current = self.capture()
        enabled = {f['name']: f['enabled'] for f in current['files']}
        if name not in enabled:
            raise ValueError('Uploaded TokenTV image is missing from the album')
        if not enabled.get(name):
            self.toggle("photo", "name", name, True)
        for f in current['files']:
            if f['name'] != name:
                self.change('/photo/delete?' + urlencode({'name': f['name']}, quote_via=quote))
        if {f['name'] for f in self.capture()['files']} != {name}:
            raise ValueError('Display album cleanup failed')
        if current['photo_interval'] != 10:
            self.request("/photo/interval?val=10")
        if not any(t['id'] == 2 and t['enabled'] for t in current['themes']):
            self.toggle("theme", "id", 2, True)
        for t in current["themes"]:
            if t["id"] != 2 and t['enabled']:
                self.toggle("theme", "id", t["id"], False)

    def restore(self, original):
        if original.get('device_kind') == 'geekmagic_pro':
            if 'theme' not in original:
                raise ValueError('Original PRO theme was not readable')
            self.change('/set?theme=' + str(original['theme']))
            return
        if original.get("device_kind") == "geekmagic":
            theme = original.get("theme", 1)
            self.request(f"/set?theme={theme}")
            if "autoplay" in original:
                interval = original.get("i_i", 5)
                self.request(f"/set?i_i={interval}&autoplay={original['autoplay']}")
            return
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

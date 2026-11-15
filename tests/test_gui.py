#!/usr/bin/env python3
"""Tests for the configurator page's server (p4gui.py), through its HTTP API.

    python3 tests/test_gui.py

A folder stands in for the stick (allow_folder).  Needs Pillow.
"""
import http.client
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)
import p4radio as P  # noqa: E402

try:
    from PIL import Image
    import p4gui as G
except ImportError:
    Image = None

from test_stick import FwParser  # noqa: E402


def jpeg(colour, size=304, **kw):
    b = io.BytesIO()
    Image.new("RGB", (size, size), colour).save(b, "JPEG", quality=85, subsampling=2, **kw)
    return b.getvalue()


def png(colour, size=(200, 120)):
    b = io.BytesIO()
    Image.new("RGB", size, colour).save(b, "PNG")
    return b.getvalue()


class FakeIcecast:
    """tests/fake_icecast.py on a free port."""
    def __init__(self):
        import socket
        with socket.socket() as so:
            so.bind(("127.0.0.1", 0))
            port = so.getsockname()[1]
        self.proc = subprocess.Popen([sys.executable, os.path.join(HERE, "fake_icecast.py"), str(port)])
        self.url = f"http://127.0.0.1:{port}"
        for _ in range(50):
            try:
                socket.create_connection(("127.0.0.1", port), 0.2).close()
                return
            except OSError:
                time.sleep(0.1)

    def stop(self):
        self.proc.kill()
        self.proc.wait()


@unittest.skipIf(Image is None, "needs Pillow")
class TestGui(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="p4gui-")
        self.stick = os.path.join(self.tmp, "stick")
        self.radio = os.path.join(self.stick, "radio")
        os.makedirs(self.radio)
        # a stick like the real one: bare URLs, "Name|url", eq = Flat, ready logos
        with open(os.path.join(self.radio, "stations.txt"), "w") as f:
            f.write("https://a.example/one\nhttp://b.example/two\nThree FM|https://c.example/three\n")
        with open(os.path.join(self.radio, "config.txt"), "w") as f:
            f.write("# settings\ntimezone = GMT0BST,M3.5.0/1,M10.5.0    # UK\neq       = Flat \n")
        with open(os.path.join(self.radio, "wifi.txt"), "w") as f:
            f.write("homenet\nsecret123\n")
        self.logos = {1: jpeg((200, 0, 0)), 3: jpeg((0, 0, 200))}
        for n, data in self.logos.items():
            with open(os.path.join(self.radio, f"{n}.jpg"), "wb") as f:
                f.write(data)

        started = threading.Event()

        def ready(app, httpd):
            self.app, self.httpd = app, httpd
            started.set()
        self.thread = threading.Thread(
            target=G.serve, args=(os.path.join(self.tmp, "project"),),
            kwargs=dict(open_browser=False, allow_folder=True, ready=ready), daemon=True)
        _stdout, sys.stdout = sys.stdout, io.StringIO()
        try:
            self.thread.start()
            started.wait(5)
        finally:
            sys.stdout = _stdout

    def tearDown(self):
        self.httpd.shutdown()
        shutil.rmtree(self.tmp)

    def call(self, path, body=None, raw=None, token=True, host=None):
        c = http.client.HTTPConnection("127.0.0.1", self.app.port, timeout=10)
        headers = {"Host": host or f"127.0.0.1:{self.app.port}"}
        if token:
            headers["X-Token"] = self.app.token
        if raw is not None:
            c.request("POST", path, raw, headers)
        elif body is not None:
            headers["Content-Type"] = "application/json"
            c.request("POST", path, json.dumps(body), headers)
        else:
            c.request("GET", path, headers=headers)
        r = c.getresponse()
        data = r.read()
        c.close()
        return r.status, (json.loads(data) if r.getheader("Content-Type", "").startswith("application/json")
                          else data)

    def wait_job(self):
        for _ in range(100):
            _, j = self.call("/api/job")
            if j["state"] != "running":
                return j
            time.sleep(0.05)
        self.fail("job did not finish")

    def write(self, dry, allow_drop=False):
        st, j = self.call("/api/write", {"mount": self.stick, "dry_run": dry, "allow_drop": allow_drop})
        self.assertEqual(st, 200, j)
        j = self.wait_job()
        self.assertEqual(j["state"], "done", j)
        return j["result"]

    # ------------------------------------------------------------------

    def test_page_and_guards(self):
        st, page = self.call("/", token=False)
        self.assertEqual(st, 200)
        self.assertIn(self.app.token.encode(), page)
        self.assertNotIn(b"__TOKEN__", page)
        self.assertEqual(self.call("/api/state", token=False)[0], 403)
        self.assertEqual(self.call("/api/state", host="evil.example:80")[0], 403)   # DNS rebinding
        self.assertEqual(self.call("/api/nothing")[0], 404)

    def test_load_edit_write_round_trip(self):
        st, s = self.call("/api/stick/load", {"mount": self.stick})
        self.assertEqual((st, s["loaded"]), (200, 3))
        self.assertEqual([r["url"] for r in s["rows"]],
                         ["https://a.example/one", "http://b.example/two", "https://c.example/three"])
        self.assertEqual([r["logo"] for r in s["rows"]], [True, False, True])
        self.assertEqual(s["settings"], {"timezone": "GMT0BST,M3.5.0/1,M10.5.0", "eq": "flat"})

        # written back unchanged: logos keep their bytes, wifi.txt is left alone
        res = self.write(dry=True)
        self.assertEqual(sorted(res["changed"]), ["config.txt (changed)", "stations.txt (changed)"])
        self.write(dry=False)
        for n, data in self.logos.items():
            with open(os.path.join(self.radio, f"{n}.jpg"), "rb") as f:
                self.assertEqual(f.read(), data)
        with open(os.path.join(self.radio, "wifi.txt")) as f:
            self.assertEqual(f.read(), "homenet\nsecret123\n")
        self.assertTrue(os.path.exists(os.path.join(self.stick, "p4radio", "library.csv")))
        self.assertEqual(self.write(dry=True)["changed"], [])

        # edit: move three to the top, switch two off, rename one, add a station
        ids = {r["url"]: r["id"] for r in s["rows"]}
        a, b, c = ids["https://a.example/one"], ids["http://b.example/two"], ids["https://c.example/three"]
        st, s = self.call("/api/rows", {"rows": [{"id": c}, {"id": a}, {"id": b, "on": False}]})
        self.assertEqual([(r["number"], r["on"]) for r in s["rows"]], [(1, True), (2, True), (None, False)])
        self.call("/api/rows/update", {"id": a, "name": "Alpha | Radio"})
        st, s = self.call("/api/rows/add", {"name": "New", "url": "HTTPS://d.example/new"})
        self.assertEqual(st, 200)
        st, err = self.call("/api/rows/add", {"name": "Bad", "url": "d.example"})
        self.assertEqual(st, 400)
        self.assertIn("http", err["error"])
        st, err = self.call("/api/rows/add", {"name": "Again", "url": "https://d.example/new"})
        self.assertIn("already", err["error"])

        res = self.write(dry=False, allow_drop=True)          # "two" is switched off
        self.assertEqual(res["stations"], 3)
        fw = FwParser.run(self.radio)
        self.assertEqual(fw["S"], [("Three FM", "https://c.example/three"),
                                   ("Alpha / Radio", "https://a.example/one"),
                                   ("New", "https://d.example/new")])
        self.assertEqual(fw["R"], [])
        # logos followed their stations: c's blue logo is now 1.jpg, a's red one 2.jpg
        with open(os.path.join(self.radio, "1.jpg"), "rb") as f:
            self.assertEqual(f.read(), self.logos[3])
        with open(os.path.join(self.radio, "2.jpg"), "rb") as f:
            self.assertEqual(f.read(), self.logos[1])
        self.assertFalse(os.path.exists(os.path.join(self.radio, "3.jpg")))

    def test_logos(self):
        _, s = self.call("/api/stick/load", {"mount": self.stick})
        two = s["rows"][1]["id"]
        self.assertEqual(self.call(f"/logo/{two}")[0], 404)
        st, s = self.call(f"/api/logo/upload?id={two}", raw=png((0, 160, 0)))
        self.assertEqual(st, 200)
        st, data = self.call(f"/logo/{two}")
        self.assertEqual(st, 200)
        with Image.open(io.BytesIO(data)) as im:
            self.assertEqual((im.size, im.format), ((304, 304), "JPEG"))
            self.assertTrue(P.logo_ready(data, 304))
        st, err = self.call(f"/api/logo/upload?id={two}", raw=b"not a picture")
        self.assertEqual(st, 400)
        progressive = jpeg((9, 9, 9), progressive=True)
        self.assertFalse(P.logo_ready(progressive, 304))   # re-encoded, not passed through
        st, s = self.call(f"/api/logo/upload?id={two}", raw=progressive)
        self.assertTrue(P.logo_ready(self.call(f"/logo/{two}")[1], 304))
        st, s = self.call("/api/logo/clear", {"id": two})
        self.assertFalse(s["rows"][1]["logo"])
        st, err = self.call("/api/logo/url", {"id": two, "url": "file:///etc/passwd"})
        self.assertEqual(st, 400)

    def test_remove_and_empty_write(self):
        _, s = self.call("/api/stick/load", {"mount": self.stick})
        for r in s["rows"]:
            _, s2 = self.call("/api/rows/remove", {"id": r["id"]})
        self.assertEqual(s2["rows"], [])
        self.call("/api/write", {"mount": self.stick, "dry_run": True})
        j = self.wait_job()
        self.assertEqual(j["state"], "error")
        self.assertIn("no stations", j["error"])

    def test_dropping_stations_needs_confirming(self):
        # an empty project, one new station: writing would wipe the stick's three
        self.call("/api/rows/add", {"name": "New", "url": "https://d.example/new"})
        st, plan = self.call("/api/plan?mount=" + self.stick)
        self.assertEqual(plan["stick_stations"], 3)
        self.assertEqual([d["url"] for d in plan["drop"]],
                         ["https://a.example/one", "http://b.example/two", "https://c.example/three"])
        st, err = self.call("/api/write", {"mount": self.stick})
        self.assertEqual(st, 400)
        self.assertIn("remove 3 station", err["error"])
        with open(os.path.join(self.radio, "stations.txt")) as f:
            self.assertIn("a.example", f.read())                 # untouched
        self.assertEqual(self.write(dry=True)["stations"], 1)   # a check is always allowed
        st, j = self.call("/api/write", {"mount": self.stick, "allow_drop": True})
        self.assertEqual(self.wait_job()["state"], "done")
        self.assertEqual(self.call("/api/plan?mount=" + self.stick)[1]["drop"], [])

        # loaded first, nothing is dropped; switching one off drops just that one
        _, s = self.call("/api/stick/load", {"mount": self.stick})
        self.assertEqual(self.call("/api/plan?mount=" + self.stick)[1]["drop"], [])
        self.call("/api/rows", {"rows": [{"id": s["rows"][0]["id"], "on": False}]})
        self.assertEqual(len(self.call("/api/plan?mount=" + self.stick)[1]["drop"]), 1)

    # -- step 3: playlists, testing, preview, search ----------------------

    def test_playlist_files(self):
        m3u = ("#EXTM3U\n#EXTINF:-1,Jazz Café\nhttps://j.example/live\n"
               "#EXTINF:-1,Bad\nftp://nope\nhttp://plain.example/s\n").encode()
        st, s = self.call("/api/playlist", raw=m3u)
        self.assertEqual((st, s["added"]), (200, 2))
        self.assertEqual([(r["name"], r["url"]) for r in s["rows"]],
                         [("Jazz Café", "https://j.example/live"), ("", "http://plain.example/s")])
        pls = b"[playlist]\nFile1=https://j.example/live\nTitle1=Jazz\nFile2=http://p.example/x\nTitle2=P\nNumberOfEntries=2\n"
        st, s = self.call("/api/playlist", raw=pls)
        self.assertEqual(s["added"], 1)
        self.assertIn("already in the list", s["problems"][0])
        self.assertEqual(self.call("/api/playlist", raw=b"hello")[0], 400)
        # the one-line file station websites hand out
        self.assertEqual(P.playlist_entries("https://play.ilovemusic.de/ilm_iloveradio/"),
                         [("", "https://play.ilovemusic.de/ilm_iloveradio/")])

    def test_testing_and_preview(self):
        fake = FakeIcecast()
        try:
            base = fake.url
            _, s = self.call("/api/playlist", raw=f"{base}/mp3\n{base}/ogg\n{base}/live.m3u8\n".encode())
            ids = [r["id"] for r in s["rows"]]
            for _ in range(200):                         # tests started on adding
                _, s = self.call("/api/state")
                if not any(r["testing"] for r in s["rows"]):
                    break
                time.sleep(0.1)
            got = {r["url"].rsplit("/", 1)[1]: r for r in s["rows"]}
            self.assertEqual(got["mp3"]["status"], "OK")
            self.assertEqual(got["mp3"]["codec"], "MP3")
            self.assertEqual(got["mp3"]["name"], "")     # names are not filled in from the stream
            self.assertTrue(got["mp3"]["icy_name"])
            self.assertEqual(got["ogg"]["status"], "FAIL")
            self.assertIn("HLS", got["live.m3u8"]["notes"])
            st, s = self.call("/api/test", {"ids": ids[:1]})
            self.assertEqual(s["started"], 1)

            # preview: MP3 streams through, Opus is refused with a reason
            c = http.client.HTTPConnection("127.0.0.1", self.app.port, timeout=10)
            q = urllib.parse.urlencode({"t": self.app.token, "url": f"{base}/mp3"})
            c.request("GET", f"/preview?{q}", headers={"Host": f"127.0.0.1:{self.app.port}"})
            r = c.getresponse()
            self.assertEqual((r.status, r.getheader("Content-Type")), (200, "audio/mpeg"))
            self.assertGreater(len(r.read(20000)), 10000)
            c.close()
            q = urllib.parse.urlencode({"t": self.app.token, "url": f"{base}/ogg"})
            self.assertEqual(self.call(f"/preview?{q}", token=False)[0], 502)
            _, e = self.call("/api/preview_error?" + urllib.parse.urlencode({"url": f"{base}/ogg"}))
            self.assertIn("not supported", e["error"])
            q = urllib.parse.urlencode({"t": "wrong", "url": f"{base}/mp3"})
            self.assertEqual(self.call(f"/preview?{q}", token=False)[0], 403)
        finally:
            fake.stop()

    def test_search(self):
        canned = [
            {"name": " Good FM ", "url": "http://g.example/s", "codec": "MP3", "bitrate": 128,
             "countrycode": "GB", "tags": "pop,hits", "favicon": "", "hls": 0},
            {"name": "Opus FM", "url": "http://o.example/s", "codec": "OGG", "hls": 0},
            {"name": "HLS FM", "url": "http://h.example/s.m3u8", "codec": "AAC", "hls": 1},
            {"name": "Mine", "url": "https://a.example/one", "codec": "AAC+", "hls": 0},
        ]
        seen = {}
        real = G.P.rb_search
        G.P.rb_search = lambda params: (seen.update(params), canned)[1]
        try:
            self.call("/api/stick/load", {"mount": self.stick})
            st, j = self.call("/api/search?tag=Jazz&country=GB&name=x")
        finally:
            G.P.rb_search = real
        self.assertEqual(st, 200, j)
        self.assertEqual((seen["tag"], seen["countrycode"], seen["hidebroken"]), ("jazz", "GB", "true"))
        self.assertEqual([(r["name"], r["in_list"]) for r in j["results"]], [("Good FM", False), ("Mine", True)])
        self.assertEqual(j["skipped"], 2)
        st, s = self.call("/api/rows/add", {"name": "Good FM", "url": "http://g.example/s", "genre": "pop",
                                            "country": "GB", "logo_url": ""})
        self.assertEqual(st, 200)

    # -- step 4: WiFi and settings ------------------------------------------

    def test_wifi_and_settings(self):
        self.call("/api/stick/load", {"mount": self.stick})
        # bad input is refused in plain words, and nothing changes
        for body, words in (({"mode": "set", "ssid": "", "password": "secret123"}, "name is empty"),
                            ({"mode": "set", "ssid": "Home", "password": "short"}, "8 to 63"),
                            ({"mode": "odd"}, "unknown")):
            st, err = self.call("/api/wifi", body)
            self.assertEqual(st, 400)
            self.assertIn(words, err["error"])
        _, s = self.call("/api/state")
        self.assertEqual(s["wifi"]["mode"], "keep")

        st, s = self.call("/api/wifi", {"mode": "set", "ssid": "New Net", "password": "pass word 9"})
        self.assertEqual(s["wifi"], {"mode": "set", "ssid": "New Net", "has_password": True})
        self.assertNotIn("pass word 9", json.dumps(s))                 # never sent back
        with open(os.path.join(self.app.project.dir, "p4radio.json")) as f:
            self.assertNotIn("pass word 9", f.read())                 # never saved on this computer

        st, err = self.call("/api/settings", {"tz_name": "Nowhere/Atlantis"})
        self.assertEqual(st, 400)
        st, err = self.call("/api/settings", {"brightness": 5})
        self.assertIn("20 to 100", err["error"])
        if os.path.exists("/usr/share/zoneinfo/Asia/Kolkata"):
            st, s = self.call("/api/settings", {"tz_name": "Asia/Kolkata", "eq": "voice", "brightness": 60})
            self.assertEqual(s["settings"], {"timezone": "IST-5:30", "tz_name": "Asia/Kolkata",
                                             "eq": "voice", "brightness": 60})
            self.assertEqual(self.call("/api/tz?name=Asia/Kolkata")[1]["posix"], "IST-5:30")
        self.write(dry=False)
        fw = FwParser.run(self.radio)
        self.assertEqual(fw["W"], ("New Net", "pass word 9"))
        if os.path.exists("/usr/share/zoneinfo/Asia/Kolkata"):
            self.assertEqual(fw["C"], {"timezone": "IST-5:30", "eq": "voice", "brightness": "60"})
            self.assertEqual((fw["TZ"], fw["B"], fw["CR"]), ("ok", 60, []))

        # a POSIX string from a stick is kept; "leave as it is" leaves lines out
        st, s = self.call("/api/settings", {"timezone": "GMT0BST,M3.5.0/1,M10.5.0"})
        self.assertEqual(s["settings"], {"timezone": "GMT0BST,M3.5.0/1,M10.5.0"})
        self.call("/api/settings", {})
        self.call("/api/wifi", {"mode": "remove"})
        self.write(dry=False)
        self.assertFalse(os.path.exists(os.path.join(self.radio, "wifi.txt")))
        with open(os.path.join(self.radio, "config.txt")) as f:
            self.assertIn("timezone = IST-5:30", f.read())             # no settings: config.txt left alone

        # open network
        self.call("/api/wifi", {"mode": "set", "ssid": "Cafe", "password": ""})
        self.write(dry=False)
        self.assertEqual(FwParser.run(self.radio)["W"], ("Cafe", ""))

    def test_nearby_wifi_parsing(self):
        out = ("no:oki2g4:2412 MHz:87\nno:guest:5180 MHz:74\nyes:AX6000:5180 MHz:65\n"
               "no:Both:2437 MHz:40\nno:Both:5500 MHz:60\nno:a\\:b:2462 MHz:10\nno::2412 MHz:50\n")
        real_run, real_which = G.subprocess.run, G.shutil.which
        G.subprocess.run = lambda *a, **k: type("R", (), {"stdout": out})()
        G.shutil.which = lambda name: "/usr/bin/nmcli"
        try:
            n = G.nearby_wifi()
        finally:
            G.subprocess.run, G.shutil.which = real_run, real_which
        self.assertEqual(n["current"], "AX6000")
        self.assertEqual({x["ssid"]: x["band"] for x in n["networks"]},
                         {"oki2g4": "2.4", "guest": "5", "AX6000": "5", "Both": "both", "a:b": "2.4"})

    def test_list_sticks_does_not_crash(self):
        for s in G.list_sticks():
            self.assertIn("usable", s)


if __name__ == "__main__":
    unittest.main(verbosity=2)

#!/usr/bin/env python3
"""Tests for the stick writer.

    python3 tests/test_stick.py            (from the repo root, or anywhere)

The firmware check compiles the radio's own parser (p4-radio main/station_parse.c)
and runs it over the written files.  It looks for the firmware in ../p4-radio
next to this repo, or in $P4_RADIO_FW, and is skipped when gcc or the firmware
is missing.  Logo tests need Pillow and are skipped without it.
"""
import io
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import p4radio as P  # noqa: E402

try:
    from PIL import Image
except ImportError:
    Image = None

FW = os.environ.get("P4_RADIO_FW", os.path.join(os.path.dirname(HERE), "..", "p4-radio"))
FW_SRC = os.path.join(FW, "main", "station_parse.c")


def row(name, url, status="OK", keep="y", logo=""):
    return P.new_row(name, url, status=status, keep=keep, logo=logo)


def quiet(*a, **k):
    pass


class FwParser:
    """The firmware's parser, built once per test run."""
    exe = None
    build_dir = None

    @classmethod
    def get(cls):
        if cls.exe is None:
            if not (shutil.which("gcc") and os.path.exists(FW_SRC)):
                raise unittest.SkipTest(f"needs gcc and the firmware at {FW}")
            cls.build_dir = tempfile.TemporaryDirectory(prefix="p4fw-")
            exe = os.path.join(cls.build_dir.name, "fw_check")
            subprocess.run(["gcc", "-Wall", "-include", os.path.join(HERE, "fw_shim.h"),
                            "-Dstrlcpy=test_strlcpy", "-I", os.path.join(FW, "main"),
                            os.path.join(HERE, "fw_check.c"), FW_SRC, "-o", exe], check=True)
            cls.exe = exe
        return cls.exe

    @classmethod
    def run(cls, radio_dir):
        out = subprocess.run([cls.get(), radio_dir], capture_output=True, text=True,
                             check=True).stdout
        res = {"S": [], "R": [], "W": None, "C": {}, "CR": [], "TZ": None, "B": None}
        for ln in out.splitlines():
            tag, _, rest = ln.partition(" ")
            if tag == "S":
                res["S"].append(tuple(rest.split("\t", 1)))
            elif tag in ("R", "CR"):
                res[tag].append(rest)
            elif tag == "W":
                res["W"] = tuple(rest.split("\t", 1))
            elif tag == "W!":
                res["W"] = False
            elif tag == "C":
                k, v = rest.split("\t", 1)
                res["C"][k] = v
            elif tag == "TZ":
                res["TZ"] = rest
            elif tag == "B":
                res["B"] = int(rest)
        return res


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="p4stick-")
        self.stick = os.path.join(self.tmp, "stick")
        os.makedirs(self.stick)
        self.radio = os.path.join(self.stick, "radio")

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def write(self, rows, **kw):
        kw.setdefault("log", quiet)
        return P.write_stick(self.stick, rows, **kw)

    def ls(self):
        return sorted(os.listdir(self.radio))


class TestCleaning(unittest.TestCase):
    def test_names(self):
        self.assertEqual(P.fw_name("Rock | Pop")[0], "Rock / Pop")
        self.assertEqual(P.fw_name("#1 Hits")[0], "1 Hits")
        self.assertEqual(P.fw_name("  a\tb\nc ")[0], "a b c")
        name, note = P.fw_name("é" * 30)                 # 60 bytes of UTF-8
        self.assertEqual(name, "é" * 23)                 # 46 bytes, never half a character
        self.assertTrue(note)

    def test_urls(self):
        self.assertEqual(P.fw_url(" HTTPS://x.com/a "), "https://x.com/a")
        self.assertEqual(P.fw_url("http://x.com/a|b"), "http://x.com/a%7Cb")
        for bad in ("ftp://x", "x.com/stream", "http://x.com/a b", "http://x/" + "a" * 250):
            with self.assertRaises(ValueError):
                P.fw_url(bad)

    def test_wifi(self):
        self.assertEqual(P.wifi_text("Home", "secret123"), "Home\nsecret123\n")
        self.assertEqual(P.wifi_text("Cafe", ""), "Cafe\n")
        for ssid, pw in (("", "secret123"), ("#net", "secret123"), ("x" * 33, ""),
                         ("Home", "short"), ("Home", "x" * 64), ("Home", "a\nb")):
            with self.assertRaises(ValueError):
                P.wifi_text(ssid, pw)

    def test_timezone(self):
        self.assertTrue(P.tz_valid("GMT0BST,M3.5.0/1,M10.5.0"))
        self.assertTrue(P.tz_valid("<+0530>-5:30"))
        self.assertFalse(P.tz_valid("Europe/London"))
        self.assertFalse(P.tz_valid("XY1"))
        if os.path.exists("/usr/share/zoneinfo/Europe/London"):
            self.assertEqual(P.tz_posix("Europe/London"), "GMT0BST,M3.5.0/1,M10.5.0")
            self.assertTrue(P.tz_valid(P.tz_posix("Asia/Kolkata")))
        with self.assertRaises(ValueError):
            P.tz_posix("Nowhere/Atlantis")

    def test_config(self):
        self.assertIsNone(P.config_text({}))
        t = P.config_text({"timezone": "CET-1CEST,M3.5.0,M10.5.0/3", "eq": "bass", "brightness": 80})
        self.assertIn("eq = bass\n", t)
        for bad in ({"eq": "rock"}, {"brightness": 10}, {"timezone": "London"}):
            with self.assertRaises(ValueError):
                P.config_text(bad)

    def test_logo_numbers(self):
        for name, n in (("1.jpg", 1), ("13.JPE", 13), ("01.jpeg", 1), ("255.jpg", 255),
                        ("256.jpg", 0), ("0.jpg", 0), ("1.png", 0), ("cover.jpg", 0),
                        ("12345.jpg", 0), ("1.jpg.p4tmp", 0)):
            self.assertEqual(P.fw_logo_number(name), n, name)


class TestStick(Base):
    def test_layout_and_firmware_reads_it(self):
        rows = [row("Classic FM", "https://media-ice.musicradio.com/ClassicFMMP3"),
                row("Rock | Pop", "http://x.com/rock"),
                row("No URL", "x.com/oops"),                    # left out
                row("Jazz", "HTTP://x.com/jazz?a=1|2"),
                row("", "http://x.com/unnamed")]
        res = self.write(rows, settings={"timezone": "GMT0BST,M3.5.0/1,M10.5.0", "eq": "voice",
                                         "brightness": 55},
                         wifi=("My Net", "pass word 1"))
        self.assertEqual(res["stations"], 4)
        self.assertEqual(self.ls(), ["config.txt", "stations.txt", "wifi.txt"])

        fw = FwParser.run(self.radio)
        self.assertEqual(fw["R"], [])
        self.assertEqual(fw["S"], [("Classic FM", "https://media-ice.musicradio.com/ClassicFMMP3"),
                                   ("Rock / Pop", "http://x.com/rock"),
                                   ("Jazz", "http://x.com/jazz?a=1%7C2"),
                                   ("", "http://x.com/unnamed")])
        self.assertEqual(fw["W"], ("My Net", "pass word 1"))
        self.assertEqual(fw["C"], {"timezone": "GMT0BST,M3.5.0/1,M10.5.0", "eq": "voice",
                                   "brightness": "55"})
        self.assertEqual((fw["TZ"], fw["B"], fw["CR"]), ("ok", 55, []))

    def test_firmware_takes_every_name_and_url_we_allow(self):
        rows = [row("€" * 20, "http://x.com/" + "a" * 242),     # 60-byte name cut, 255-byte URL
                row("#hash first", "http://x.com/h"),
                row("x" * 47, "http://x.com/47")]
        self.write(rows)
        fw = FwParser.run(self.radio)
        self.assertEqual(fw["R"], [])
        self.assertEqual(len(fw["S"]), 3)
        self.assertEqual(fw["S"][1][0], "hash first")

    def test_station_limit(self):
        rows = [row(f"s{i}", f"http://x.com/{i}") for i in range(300)]
        res = self.write(rows)
        self.assertEqual(res["stations"], 255)
        self.assertEqual(len(FwParser.run(self.radio)["S"]), 255)

    def test_second_write_changes_nothing(self):
        rows = [row("A", "http://x.com/a")]
        self.assertTrue(self.write(rows, settings={"eq": "flat"})["changed"])
        self.assertEqual(self.write(rows, settings={"eq": "flat"})["changed"], [])

    def test_wifi_left_alone_or_removed(self):
        rows = [row("A", "http://x.com/a")]
        self.write(rows, wifi=("Home", "secret123"))
        self.write(rows)                                        # no wifi given: kept
        self.assertIn("wifi.txt", self.ls())
        self.write(rows, wifi_remove=True)
        self.assertNotIn("wifi.txt", self.ls())

    def test_dry_run_writes_nothing(self):
        res = self.write([row("A", "http://x.com/a")], wifi=("Home", "secret123"), dry_run=True)
        self.assertFalse(os.path.exists(self.radio))
        self.assertIn("stations.txt (new)", res["changed"])

    def test_project_copied(self):
        lib = os.path.join(self.tmp, "library.csv")
        P.save_lib(lib, [row("A", "http://x.com/a")])
        self.write([row("A", "http://x.com/a")], project=(lib, os.path.join(self.tmp, "missing.json")))
        self.assertEqual(os.listdir(os.path.join(self.stick, "p4radio")), ["library.csv"])

    def test_interrupted_temp_files_removed(self):
        os.makedirs(self.radio)
        open(os.path.join(self.radio, "stations.txt.p4tmp"), "w").close()
        self.write([row("A", "http://x.com/a")])
        self.assertEqual(self.ls(), ["stations.txt"])


@unittest.skipIf(Image is None, "needs Pillow")
class TestLogos(Base):
    def picture(self, name, size=(400, 200), colour=(200, 30, 30)):
        p = os.path.join(self.tmp, name)
        Image.new("RGB", size, colour).save(p)
        return p

    def test_logo_files(self):
        red = self.picture("red.png")
        tiny = self.picture("tiny.png", (16, 16))
        rows = [row("A", "http://x.com/a", logo=red),
                row("B", "http://x.com/b"),                     # no logo
                row("Bad", "nope", logo=red),                   # left out: shifts the numbers
                row("C", "http://x.com/c", logo=red),
                row("D", "http://x.com/d", logo=tiny)]          # too small: no logo
        logos = P.write_logos(rows, 304, "#101418", log=quiet)
        self.assertEqual(sorted(logos), [1, 3, 4])
        os.makedirs(self.radio)
        for stale in ("1.JPEG", "01.jpg", "7.jpg", "cover.jpg", "notes.txt"):
            open(os.path.join(self.radio, stale), "w").close()
        res = self.write(rows, logos=logos)
        self.assertEqual(res["logos"], 2)
        # C was row 4 but is station 3 once "Bad" is left out
        self.assertEqual(self.ls(), ["1.jpg", "3.jpg", "cover.jpg", "notes.txt", "stations.txt"])
        self.assertTrue(any("cover.jpg" in n for n in res["notes"]))
        with Image.open(os.path.join(self.radio, "3.jpg")) as im:
            self.assertEqual((im.format, im.size, im.mode), ("JPEG", (304, 304), "RGB"))
            self.assertFalse(im.info.get("progressive"))
            self.assertEqual(im.layer[0][1:3], (2, 2))          # luma 2x2 = 4:2:0
        self.assertEqual(FwParser.run(self.radio)["S"][2][0], "C")
        # unchanged pictures are not rewritten
        self.assertEqual(self.write(rows, logos=P.write_logos(rows, 304, "#101418", log=quiet))
                         ["changed"], [])

    def test_no_logos_at_all_warns(self):
        res = self.write([row("A", "http://x.com/a")], logos={})
        self.assertTrue(any("keeps the logos" in n for n in res["notes"]))

    def test_build_writes_radio_names(self):
        lib = os.path.join(self.tmp, "library.csv")
        P.save_lib(lib, [row("A", "http://x.com/a", logo=self.picture("p.png"))])
        out = os.path.join(self.tmp, "card")
        sys.argv = ["p4radio", "-L", lib, "-C", os.path.join(self.tmp, "c.json"),
                    "build", "--out", out, "--logos"]
        with redirect():
            P.main()
        self.assertEqual(sorted(os.listdir(out)), ["1.jpg", "stations.txt"])


class TestSafety(Base):
    def test_refuses_folder_that_is_not_a_drive(self):
        if P._mount_of(self.stick) is None:
            self.skipTest("no /proc/mounts")
        with self.assertRaises(SystemExit) as e:
            P.check_stick(self.stick)
        self.assertIn("--force", str(e.exception))
        P.check_stick(self.stick, force=True)

    def test_refuses_fixed_fat_partition(self):
        m = P._mount_of("/boot/efi")
        if not m or m[1] != "/boot/efi" or m[2] != "vfat":
            self.skipTest("no FAT /boot/efi here")
        with self.assertRaises(SystemExit) as e:
            P.check_stick("/boot/efi")
        self.assertIn("not a removable drive", str(e.exception))


class redirect:
    def __enter__(self):
        self.old, sys.stdout = sys.stdout, io.StringIO()

    def __exit__(self, *a):
        sys.stdout = self.old


if __name__ == "__main__":
    unittest.main(verbosity=2)

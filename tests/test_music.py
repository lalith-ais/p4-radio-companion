#!/usr/bin/env python3
"""Tests for copying music to the stick (p4radio.music_*), and the page's music API.

    python3 tests/test_music.py
"""
import io
import os
import shutil
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)
import p4radio as P  # noqa: E402

try:
    from PIL import Image
except ImportError:
    Image = None


def mp3_frame():
    return bytes([0xFF, 0xFB, 0x90, 0x44]) + b"\0" * 413      # 128 kbps 44.1 kHz


def jpeg(size=(300, 300), **kw):
    b = io.BytesIO()
    Image.new("RGB", size, (200, 40, 40)).save(b, "JPEG", quality=80, **kw)
    return b.getvalue()


def png(size=(300, 300)):
    b = io.BytesIO()
    Image.new("RGB", size, (40, 40, 200)).save(b, "PNG")
    return b.getvalue()


def syncsafe(n):
    return bytes([(n >> 21) & 0x7F, (n >> 14) & 0x7F, (n >> 7) & 0x7F, n & 0x7F])


def id3(pic, ver=3, enc=0, desc=b"Cover"):
    """An ID3v2 tag holding one picture frame."""
    if ver == 2:
        body = bytes([enc]) + b"JPG" + b"\x03" + desc + b"\0" + pic
        frame = b"PIC" + len(body).to_bytes(3, "big") + body
    else:
        term = b"\0\0" if enc in (1, 2) else b"\0"
        body = bytes([enc]) + b"image/jpeg\0" + b"\x03" + desc + term + pic
        size = syncsafe(len(body)) if ver == 4 else len(body).to_bytes(4, "big")
        frame = b"APIC" + size + b"\0\0" + body
    title = b"TIT2" + (6 if ver != 2 else 0).to_bytes(4, "big") + b"\0\0" + b"\0Song!" if ver != 2 else b""
    tag = title + frame
    return b"ID3" + bytes([ver, 0, 0]) + syncsafe(len(tag)) + tag


def song(tag=b""):
    return tag + mp3_frame() * 20


class TestNames(unittest.TestCase):
    def test_fat_names(self):
        self.assertEqual(P.fat_name('AC/DC: "Live?"'), "AC_DC_ _Live__")
        self.assertEqual(P.fat_name("Track. "), "Track")
        self.assertEqual(P.fat_name("con.mp3"), "_con.mp3")
        self.assertEqual(P.fat_name("Beyoncé – Halo.mp3"), "Beyoncé – Halo.mp3")
        long = P.fat_name("x" * 300 + ".mp3")
        self.assertEqual((len(long), long[-4:]), (255, ".mp3"))


@unittest.skipIf(Image is None, "needs Pillow")
class TestPictures(unittest.TestCase):
    def test_jpeg_problem(self):
        self.assertIsNone(P.jpeg_problem(jpeg()))
        self.assertIsNone(P.jpeg_problem(jpeg(subsampling=0)))          # 4:4:4
        self.assertEqual(P.jpeg_problem(jpeg(progressive=True)), "progressive JPEG")
        self.assertEqual(P.jpeg_problem(png()), "PNG picture")
        self.assertIn("1600", P.jpeg_problem(jpeg((1700, 100))))
        self.assertEqual(P.jpeg_problem(jpeg(), size=3 * 1024 * 1024), "picture over 2 MB")

    def test_mp3_art(self):
        self.assertIsNone(P.mp3_art_problem(song()))                     # no tag
        self.assertIsNone(P.mp3_art_problem(song(id3(jpeg()))))
        for ver in (2, 3, 4):
            with self.subTest(ver=ver):
                self.assertEqual(P.mp3_art_problem(song(id3(jpeg(progressive=True), ver))), "progressive JPEG")
        self.assertEqual(P.mp3_art_problem(song(id3(png()))), "PNG picture")
        # UTF-16 description: odd-length text must not end the search early
        desc = "Cöver".encode("utf-16")
        self.assertEqual(P.mp3_art_problem(song(id3(jpeg(progressive=True), 3, 1, desc))), "progressive JPEG")
        self.assertIsNone(P.mp3_art_problem(song(id3(jpeg(), 3, 1, desc))))


@unittest.skipIf(Image is None, "needs Pillow")
class TestPlanAndCopy(unittest.TestCase):
    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="p4music-"))   # macOS: /var is /private/var
        self.src = os.path.join(self.tmp, "Albums")
        self.stick = os.path.join(self.tmp, "stick")
        os.makedirs(self.stick)

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def put(self, rel, data):
        p = os.path.join(self.src, *rel.split("/"))
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "wb") as f:
            f.write(data)
        return p

    def stick_files(self):
        out = []
        for d, _, files in os.walk(os.path.join(self.stick, "music")):
            for f in files:
                out.append(os.path.relpath(os.path.join(d, f), os.path.join(self.stick, "music")).replace(os.sep, "/"))
        return sorted(out)

    def test_plan_copy_again_and_remove(self):
        self.put("Abba/Gold/01 Dancing Queen.mp3", song(id3(jpeg())))
        self.put("Abba/Gold/02 Waterloo.MP3", song(id3(jpeg(progressive=True))))
        self.put("Abba/Gold/cover.jpg", jpeg())                          # fine: copied as is
        self.put("Abba/Live/folder.png", png())                          # becomes folder.jpg
        self.put("Abba/Live/front.jpg", jpeg(progressive=True))          # converted
        self.put("Abba/Live/AC:DC?.mp3", song())                         # renamed
        self.put("Abba/Live/notes.txt", b"x")                            # ignored
        self.put("Abba/Live/hidden/.secret.mp3", song())                 # hidden: ignored
        self.put("Other/a.flac", b"f")
        self.put("Other/b.FLAC", b"f")
        self.put("Other/c.m4a", b"m")
        deep = "/".join(f"d{i}" for i in range(9))                       # Albums + 9 = 10 levels
        self.put(f"{deep}/too deep.mp3", song())
        self.put("Long/" + "n" * 200 + ".mp3", song())

        plan = P.music_plan([self.src], self.stick)
        by = {i["rel"]: i for i in plan["items"]}
        self.assertIn("Albums/Abba/Gold/01 Dancing Queen.mp3", by)
        self.assertEqual(by["Albums/Abba/Gold/cover.jpg"]["action"], "copy")
        self.assertEqual(by["Albums/Abba/Live/folder.jpg"]["action"], "cover")
        self.assertEqual(by["Albums/Abba/Live/front.jpg"]["action"], "cover")
        self.assertIn("Albums/Abba/Live/AC_DC_.mp3", by)
        self.assertEqual(plan["left_out"], {".flac": 2, ".m4a": 1})
        self.assertEqual([w for _, w in plan["skipped"]], ["more than 8 folders deep"])
        longrel = [r for r in by if r.startswith("Albums/Long/")][0]
        self.assertLessEqual(len(longrel.encode()), P.FW_MUSIC_PATH_MAX)
        self.assertTrue(longrel.endswith(".mp3"))
        self.assertTrue(plan["fits"])

        res = P.music_copy(plan, self.stick)
        self.assertEqual((res["copied"], res["covers"], res["art_problems"]), (7, 2, 1))   # 4 songs + 3 pictures
        files = self.stick_files()
        self.assertNotIn("notes.txt", " ".join(files))
        self.assertFalse(any(f.endswith(".p4tmp") for f in files))
        with open(os.path.join(self.stick, "music", "Albums", "Abba", "Live", "front.jpg"), "rb") as f:
            self.assertIsNone(P.jpeg_problem(f.read()))
        with open(os.path.join(self.stick, "music", "Albums", "Abba", "Gold", "cover.jpg"), "rb") as f:
            self.assertEqual(f.read(), jpeg())                          # passed through unchanged

        again = P.music_plan([self.src], self.stick)                     # nothing left to do
        self.assertEqual({i["action"] for i in again["items"]}, {"same"})
        time.sleep(0.01)
        self.put("Abba/Gold/01 Dancing Queen.mp3", song(id3(jpeg())) + mp3_frame())   # changed
        again = P.music_plan([self.src], self.stick)
        self.assertEqual([i["rel"] for i in again["items"] if i["action"] == "replace"],
                         ["Albums/Abba/Gold/01 Dancing Queen.mp3"])

        have = P.music_summary(self.stick)
        self.assertEqual((have["songs"], [f["name"] for f in have["folders"]]), (4, ["Albums"]))
        with self.assertRaises(ValueError):
            P.music_remove(self.stick, "../stick")
        P.music_remove(self.stick, "Albums")
        self.assertEqual(P.music_summary(self.stick)["songs"], 0)

    def test_destinations_and_collisions(self):
        root = os.path.join(self.tmp, "Music")
        for rel in ("A/x.mp3", "b.mp3"):
            p = os.path.join(root, *rel.split("/"))
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "wb") as f:
                f.write(song())
        self.put("A/X.mp3", song() + b"1")                               # same name, other case
        plan = P.music_plan([{"path": root, "dest": ""}, os.path.join(self.src, "A")], self.stick)
        self.assertEqual(sorted(i["rel"] for i in plan["items"]), ["A/X (2).mp3", "A/x.mp3", "b.mp3"])
        plan = P.music_plan([root], self.stick)                          # default: the folder's own name
        self.assertEqual(sorted(i["rel"] for i in plan["items"]), ["Music/A/x.mp3", "Music/b.mp3"])
        plan = P.music_plan([{"path": root, "dest": "Mine/Old: hits"}], self.stick)
        self.assertEqual(sorted(i["rel"] for i in plan["items"]), ["Mine/Old_ hits/A/x.mp3", "Mine/Old_ hits/b.mp3"])

    def test_songs_already_elsewhere_on_the_stick(self):
        self.put("x.mp3", song())
        self.put("y.mp3", song() * 2)
        os.makedirs(os.path.join(self.stick, "music", "favourites"))
        shutil.copy(os.path.join(self.src, "x.mp3"), os.path.join(self.stick, "music", "favourites", "X.MP3"))
        with open(os.path.join(self.stick, "music", "favourites", "y.mp3"), "wb") as f:
            f.write(song())                                              # same name, other size: a different song
        plan = P.music_plan([self.src], self.stick)
        acts = {i["rel"]: i["action"] for i in plan["items"]}
        self.assertEqual(acts, {"Albums/x.mp3": "elsewhere", "Albums/y.mp3": "copy"})
        self.assertEqual(plan["elsewhere"], {"favourites": 1})
        self.assertEqual(P.music_copy(plan, self.stick)["copied"], 1)
        plan = P.music_plan([{"path": self.src, "dest": "favourites"}], self.stick)
        self.assertEqual({i["rel"]: i["action"] for i in plan["items"]},
                         {"favourites/x.mp3": "same", "favourites/y.mp3": "replace"})

    def test_no_room_and_cancel(self):
        for n in range(5):
            self.put(f"S/{n}.mp3", song() * 50)
        plan = P.music_plan([self.src], self.stick)
        real = shutil.disk_usage
        P.shutil.disk_usage = lambda p: type("U", (), {"free": 1000, "total": 1, "used": 0})()
        try:
            small = P.music_plan([self.src], self.stick)
        finally:
            P.shutil.disk_usage = real
        self.assertFalse(small["fits"])
        self.assertIn("not enough room on the stick", small["warnings"])
        with self.assertRaises(SystemExit):
            P.music_copy(small, self.stick)
        calls = []
        res = P.music_copy(plan, self.stick, lambda d, t, r: calls.append(r), lambda: len(calls) > 3)
        self.assertTrue(res["cancelled"])
        self.assertLess(res["copied"], 5)
        self.assertFalse(any(f.endswith(".p4tmp") for f in self.stick_files()))


@unittest.skipIf(Image is None, "needs Pillow")
class TestMusicApi(unittest.TestCase):
    def setUp(self):
        from test_gui import TestGui
        self.g = TestGui("test_page_and_guards")
        self.g.setUp()
        self.src = os.path.join(self.g.tmp, "Tunes")
        os.makedirs(os.path.join(self.src, "Album"))
        for n in range(3):
            with open(os.path.join(self.src, "Album", f"{n}.mp3"), "wb") as f:
                f.write(song() * 10)

    def tearDown(self):
        self.g.tearDown()

    def test_music_api(self):
        call, stick = self.g.call, self.g.stick
        st, fs = call("/api/fs?path=" + os.path.join(self.src, "Album"))
        self.assertEqual((st, fs["mp3"], fs["parent"]), (200, 3, self.src))
        self.assertEqual(call("/api/fs?path=/no/such/place")[0], 400)
        st, m = call("/api/music?mount=" + stick)
        self.assertEqual((m["songs"], m["sources"]), (0, []))
        self.assertEqual(call("/api/music/plan", {"mount": stick})[0], 400)  # nothing chosen yet
        st, r = call("/api/music/sources", {"sources": [{"path": self.src}, {"path": self.src, "dest": "x"}]})
        self.assertEqual(r["sources"], [{"path": self.src, "dest": "Tunes"}])           # one entry per folder
        st, plan = call("/api/music/plan", {"mount": stick})
        self.assertEqual((plan["count"], plan["fits"]), ({"copy": 3}, True))
        call("/api/music/copy", {"mount": stick})
        for _ in range(100):
            _, j = call("/api/music/job")
            if j["state"] != "running":
                break
            time.sleep(0.05)
        self.assertEqual((j["state"], j["result"]["copied"]), ("done", 3))
        st, m = call("/api/music?mount=" + stick)
        self.assertEqual((m["songs"], m["folders"][0]["name"]), (3, "Tunes"))
        call("/api/music/sources", {"sources": [{"path": self.src, "dest": "Elsewhere"}]})
        st, plan = call("/api/music/plan", {"mount": stick})
        self.assertEqual((plan["count"], plan["elsewhere"]), ({"elsewhere": 3}, {"Tunes/Album": 3}))
        st, m = call("/api/music/remove", {"mount": stick, "name": "Tunes"})
        self.assertEqual(m["songs"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)

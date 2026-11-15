#!/usr/bin/env python3
"""probe() against tests/fake_icecast.py: every kind of stream it serves.

    python3 tests/test_probe.py
"""
import concurrent.futures as cf
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)
import p4radio as P  # noqa: E402
from test_gui import FakeIcecast  # noqa: E402

EXPECT = {                 # path: (status, codec, a note that must be there)
    "/mp3": ("OK", "MP3", None),
    "/icysplit": ("OK", "MP3", None),        # metadata split across reads used to stall the probe
    "/icy": ("OK", "MP3", None),
    "/aacp": ("OK", "HE-AAC", None),
    "/aacv2": ("OK", "HE-AACv2", None),
    "/aaclc": ("OK", "AAC-LC", None),
    "/redir": ("OK", "HE-AAC", None),
    "/listen.pls": ("OK", "MP3", "via pls playlist"),
    "/slow": ("WARN", "MP3", "throughput only"),
    "/live.m3u8": ("FAIL", "", "HLS"),
    "/ogg": ("FAIL", "Opus/Ogg", "not supported"),
    "/nothing": ("FAIL", "", "HTTP 404"),
}


class TestProbe(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fake = FakeIcecast()
        with cf.ThreadPoolExecutor(len(EXPECT)) as ex:
            cls.res = dict(zip(EXPECT, ex.map(lambda p: P.probe(cls.fake.url + p, 6), EXPECT)))

    @classmethod
    def tearDownClass(cls):
        cls.fake.stop()

    def test_every_stream(self):
        for path, (status, codec, note) in EXPECT.items():
            with self.subTest(path):
                r = self.res[path]
                self.assertEqual((r["status"], r["codec"]), (status, codec), r["notes"])
                if note:
                    self.assertTrue(any(note in n for n in r["notes"]), r["notes"])

    def test_icy_metadata(self):
        for path in ("/mp3", "/icysplit"):
            with self.subTest(path):
                r = self.res[path]
                self.assertEqual(r["title"], "Artist - Song")
                self.assertGreaterEqual(float(r["speed"].rstrip("x")), 0.92)


if __name__ == "__main__":
    unittest.main(verbosity=2)

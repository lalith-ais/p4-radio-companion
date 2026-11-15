#!/usr/bin/env python3
"""
p4radio - Linux companion tool for the ESP32-P4 internet radio.

  Find stations, probe them the way the radio will (redirects, .m3u/.pls,
  ICY, MP3 vs ADTS-AAC sniffing, sustained throughput), audition them in mpv,
  keep a master library, and build the stations.txt + logos for the radio.

Workflow:
    p4radio.py import  old/stations.txt         # bring in the existing list
    p4radio.py search  --tag jazz --country GB  # radio-browser.info lookup
    p4radio.py add     <url> --name "My Station"
    p4radio.py test                             # probe all -> station_log.md
    p4radio.py listen                           # audition with mpv, keep/drop
    p4radio.py build   --out card/              # stations.txt + N.jpg logos in a folder
    p4radio.py stick   /media/me/STICK          # write radio/ on the radio's USB stick
    p4radio.py report  /media/me/STICK          # what the radio said after importing it
    p4radio.py music   /media/me/STICK ~/Music/Album   # copy MP3 folders to music/
    p4radio.py gui                              # the same in a browser page (p4gui.py)

Standard library only.  Pillow is needed only for logos (`build --logos`, `stick`)
(pip install pillow, inside your venv).
"""

import argparse
import concurrent.futures as cf
import csv
import datetime as dt
import io
import json
import os
import re
import shutil
import socket
import ssl
import subprocess
import sys
import time
import urllib.parse
import urllib.request

VERSION = "0.1"
UA = f"p4radio-companion/{VERSION}"
LIB_DEFAULT = "library.csv"
CFG_DEFAULT = "p4radio.json"

FIELDS = [
    "keep", "name", "url", "genre", "country", "logo", "notes",
    # filled in by `test`
    "status", "codec", "kbps", "rate", "ch", "tls", "final_url",
    "icy_name", "connect_ms", "speed", "tested",
]

# Decode-time CPU measured on the P4 at 360 MHz (esp_audio_codec 2.5)
P4_CPU = {"MP3": "~5.5%", "AAC-LC": "~5.3%", "HE-AAC": "~11%", "HE-AACv2": "~15%"}
SUPPORTED = ("MP3", "AAC-LC", "HE-AAC", "HE-AACv2", "AAC")
NAME_WARN = 24        # longer names get busy on the 800x480 player screen

# Limits of the radio's import (p4-radio main/station_parse.h, storage.c, fx.c)
FW_NAME_MAX = 47          # bytes of UTF-8
FW_URL_MAX = 255
FW_STATIONS_MAX = 255
FW_SSID_MAX = 32
FW_PASS_MAX = 63
FW_TZ_MAX = 63
FW_LOGO_MAX = 256 * 1024
EQ_PRESETS = ("flat", "bass", "treble", "acoustic", "live", "voice", "classical", "loudness")


def fw_logo_number(name):
    """Station number the radio reads from a logo file name, or 0
    (same rules as parse_logo_name() in the firmware's storage.c)."""
    m = re.fullmatch(r"(\d{1,4})\.(jpg|jpeg|jpe)", name, re.I)
    n = int(m.group(1)) if m else 0
    return n if 1 <= n <= FW_STATIONS_MAX else 0


# --------------------------------------------------------------------------
# config / library
# --------------------------------------------------------------------------

def load_cfg(path):
    cfg = {"line": "{name}|{url}"}
    try:
        with open(path) as f:
            cfg.update(json.load(f))
    except FileNotFoundError:
        pass
    return cfg


def save_cfg(path, cfg):
    with open(path, "w") as f:
        json.dump(cfg, f, indent=2)


def load_lib(path):
    if not os.path.exists(path):
        return []
    with open(path, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    for r in rows:
        for k in FIELDS:
            r.setdefault(k, "")
            if r[k] is None:
                r[k] = ""
    return rows


def save_lib(path, rows):
    tmp = path + ".tmp"
    with open(tmp, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)
    os.replace(tmp, path)


def find_url(rows, url):
    for r in rows:
        if r["url"].strip() == url.strip():
            return r
    return None


def new_row(name, url, **kw):
    r = {k: "" for k in FIELDS}
    r.update(keep="y", name=name.strip(), url=url.strip())
    r.update({k: v for k, v in kw.items() if v})
    return r


def select(rows, spec):
    """spec: None/'all' -> all, '3' or '1,4,7-9' -> 1-based indices, else name substring."""
    if not spec or spec == "all":
        return list(enumerate(rows, 1))
    if re.fullmatch(r"[\d,\- ]+", spec):
        want = set()
        for part in spec.replace(" ", "").split(","):
            if "-" in part:
                a, b = part.split("-")
                want.update(range(int(a), int(b) + 1))
            elif part:
                want.add(int(part))
        return [(i, r) for i, r in enumerate(rows, 1) if i in want]
    s = spec.lower()
    return [(i, r) for i, r in enumerate(rows, 1) if s in r["name"].lower()]


# --------------------------------------------------------------------------
# minimal HTTP client (handles "ICY 200 OK", redirects, playlists)
# --------------------------------------------------------------------------

class ProbeError(Exception):
    pass


def _connect(url, timeout, icy=True):
    u = urllib.parse.urlsplit(url)
    if u.scheme not in ("http", "https"):
        raise ProbeError(f"unsupported scheme {u.scheme!r}")
    host = u.hostname
    port = u.port or (443 if u.scheme == "https" else 80)
    path = u.path or "/"
    if u.query:
        path += "?" + u.query
    t0 = time.monotonic()
    try:
        sock = socket.create_connection((host, port), timeout=timeout)
    except socket.gaierror as e:
        raise ProbeError(f"DNS: {e}")
    except OSError as e:
        raise ProbeError(f"connect: {e}")
    if u.scheme == "https":
        ctx = ssl.create_default_context()
        try:
            sock = ctx.wrap_socket(sock, server_hostname=host)
        except ssl.SSLCertVerificationError as e:
            sock.close()
            raise ProbeError(f"TLS cert: {e.verify_message}")
        except (ssl.SSLError, OSError) as e:
            sock.close()
            raise ProbeError(f"TLS: {e}")
    hosthdr = host if (u.port is None) else f"{host}:{port}"
    req = (f"GET {path} HTTP/1.0\r\nHost: {hosthdr}\r\nUser-Agent: {UA}\r\n"
           f"Accept: */*\r\n{'Icy-MetaData: 1' + chr(13) + chr(10) if icy else ''}Connection: close\r\n\r\n")
    sock.sendall(req.encode())
    # read header block
    buf = b""
    while b"\r\n\r\n" not in buf and b"\n\n" not in buf:
        try:
            chunk = sock.recv(4096)
        except socket.timeout:
            sock.close()
            raise ProbeError("timeout waiting for response headers")
        if not chunk:
            break
        buf += chunk
        if len(buf) > 65536:
            break
    connect_ms = int((time.monotonic() - t0) * 1000)
    sep = b"\r\n\r\n" if b"\r\n\r\n" in buf else b"\n\n"
    if sep not in buf:
        sock.close()
        raise ProbeError("no HTTP header received")
    head, rest = buf.split(sep, 1)
    lines = head.decode("latin-1").splitlines()
    status_line = lines[0] if lines else ""
    m = re.match(r"(HTTP/\d\.\d|ICY)\s+(\d{3})", status_line)
    if not m:
        sock.close()
        raise ProbeError(f"bad status line {status_line[:60]!r}")
    code = int(m.group(2))
    hdrs = {}
    for ln in lines[1:]:
        if ":" in ln:
            k, v = ln.split(":", 1)
            hdrs[k.strip().lower()] = v.strip()
    return sock, code, hdrs, rest, connect_ms, u.scheme == "https"


def _read_some(sock, first, limit, timeout=5):
    data = first
    sock.settimeout(timeout)
    while len(data) < limit:
        try:
            c = sock.recv(8192)
        except socket.timeout:
            break
        if not c:
            break
        data += c
    return data[:limit]


PLAYLIST_CT = ("mpegurl", "scpls", "pls+xml", "x-ms-asf", "x-ms-wax", "xspf")


def _parse_playlist(text):
    """Return (first stream url, kind) from m3u/pls/asx/xspf text."""
    t = text.lstrip("﻿").strip()
    if "#EXT-X-" in t:
        return None, "HLS"
    if t.lower().startswith("[playlist]"):
        for ln in t.splitlines():
            m = re.match(r"\s*File\d+\s*=\s*(\S+)", ln, re.I)
            if m:
                return m.group(1), "pls"
        return None, "pls"
    m = re.search(r'<ref\s+href\s*=\s*"([^"]+)"', t, re.I)
    if m:
        return m.group(1), "asx"
    m = re.search(r"<location>\s*([^<\s]+)\s*</location>", t, re.I)
    if m:
        return m.group(1), "xspf"
    for ln in t.splitlines():
        ln = ln.strip()
        if ln and not ln.startswith("#") and re.match(r"https?://", ln, re.I):
            return ln, "m3u"
    return None, "m3u"


# --------------------------------------------------------------------------
# audio sniffing
# --------------------------------------------------------------------------

MP3_BR = {
    1: [0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320],   # MPEG1 L3
    2: [0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160],       # MPEG2/2.5 L3
}
MP3_SR = {3: [44100, 48000, 32000], 2: [22050, 24000, 16000], 0: [11025, 12000, 8000]}
ADTS_SR = [96000, 88200, 64000, 48000, 44100, 32000, 24000, 22050,
           16000, 12000, 11025, 8000, 7350]


def _mp3_hdr(b, i):
    if i + 4 > len(b) or b[i] != 0xFF or (b[i + 1] & 0xE0) != 0xE0:
        return None
    ver = (b[i + 1] >> 3) & 3
    layer = (b[i + 1] >> 1) & 3
    bri = b[i + 2] >> 4
    sri = (b[i + 2] >> 2) & 3
    pad = (b[i + 2] >> 1) & 1
    if ver == 1 or layer != 1 or bri in (0, 15) or sri == 3:   # Layer III only
        return None
    br = MP3_BR[1 if ver == 3 else 2][bri]
    sr = MP3_SR[ver][sri]
    ch = 1 if (b[i + 3] >> 6) == 3 else 2
    flen = (144 if ver == 3 else 72) * br * 1000 // sr + pad
    return dict(codec="MP3", kbps=br, rate=sr, ch=ch, flen=flen)


def _adts_hdr(b, i):
    if i + 7 > len(b) or b[i] != 0xFF or (b[i + 1] & 0xF6) != 0xF0:
        return None
    prof = (b[i + 2] >> 6) & 3
    sfi = (b[i + 2] >> 2) & 0xF
    ch = ((b[i + 2] & 1) << 2) | (b[i + 3] >> 6)
    flen = ((b[i + 3] & 3) << 11) | (b[i + 4] << 3) | (b[i + 5] >> 5)
    if sfi >= len(ADTS_SR) or flen < 7:
        return None
    return dict(codec="AAC", profile=prof, rate=ADTS_SR[sfi], ch=ch, flen=flen)


def sniff(b):
    """Identify the stream format the way the radio's sniffer would."""
    if b[:4] == b"OggS":
        if b"OpusHead" in b[:512]:
            return {"codec": "Opus/Ogg"}
        if b"\x01vorbis" in b[:512]:
            return {"codec": "Vorbis/Ogg"}
        if b"\x7fFLAC" in b[:512]:
            return {"codec": "FLAC/Ogg"}
        return {"codec": "Ogg"}
    if b[:4] == b"fLaC":
        return {"codec": "FLAC"}
    if b[4:8] == b"ftyp":
        return {"codec": "MP4/fMP4"}
    if b[:3] == b"ID3":                       # skip ID3v2 tag
        sz = (b[6] << 21) | (b[7] << 14) | (b[8] << 7) | b[9]
        b = b[10 + sz:]
    lim = min(len(b) - 8, 64 * 1024)
    for i in range(max(lim, 0)):
        if b[i] != 0xFF:
            continue
        for parse in (_mp3_hdr, _adts_hdr):
            h = parse(b, i)
            if not h:
                continue
            # require 3 consecutive consistent frames
            j, ok = i, 0
            for _ in range(3):
                hh = parse(b, j)
                if not hh or hh["rate"] != h["rate"]:
                    break
                ok += 1
                j += hh["flen"]
            if ok == 3:
                return h
    return None


def classify_aac(h, nominal_kbps):
    """ADTS headers carry the core-coder rate; implicit SBR halves it."""
    if h["profile"] != 1:
        return {0: "AAC-Main", 2: "AAC-SSR", 3: "AAC-LTP"}[h["profile"]], h["rate"], h["ch"]
    if h["rate"] <= 24000:
        # SBR doubles the output rate; PS turns mono core into stereo
        if h["ch"] == 1 and (not nominal_kbps or nominal_kbps <= 64):
            return "HE-AACv2", h["rate"] * 2, 2
        return "HE-AAC", h["rate"] * 2, h["ch"]
    if nominal_kbps and nominal_kbps <= 64 and h["ch"] == 2:
        return "HE-AAC", h["rate"], h["ch"]      # explicit-signalled guess
    return "AAC-LC", h["rate"], h["ch"]


# --------------------------------------------------------------------------
# probe
# --------------------------------------------------------------------------

def open_stream(url, timeout=8.0, icy=True, hops=None, notes=None):
    """Connects the way the radio does: follows redirects and .m3u/.pls/.asx/.xspf
    wrappers (8 hops at most).  Raises ProbeError.
    -> (sock, code, headers, first bytes, connect ms, tls, final url)."""
    hops = [] if hops is None else hops
    notes = [] if notes is None else notes
    cur = url.strip()
    for hop in range(8):
        sock, code, hdrs, rest, cms, tls = _connect(cur, timeout, icy)
        hops.append(f"{code} {cur}")
        if code in (301, 302, 303, 307, 308):
            sock.close()
            loc = hdrs.get("location")
            if not loc:
                raise ProbeError(f"HTTP {code} without Location")
            cur = urllib.parse.urljoin(cur, loc)
            continue
        if code != 200:
            sock.close()
            raise ProbeError(f"HTTP {code}")
        ct = hdrs.get("content-type", "").lower()
        path = urllib.parse.urlsplit(cur).path.lower()
        looks_pl = (any(p in ct for p in PLAYLIST_CT)
                    or path.endswith((".m3u", ".m3u8", ".pls", ".asx", ".xspf")))
        if not looks_pl and (ct.startswith("text/") or not ct):
            peek = _read_some(sock, rest, 512, timeout=2)
            rest = peek
            txt = peek.decode("latin-1", "replace").lstrip()
            looks_pl = (txt.startswith(("#EXTM3U", "[playlist]", "http://", "https://", "<asx"))
                        or txt.lower().startswith("[playlist]"))
        if looks_pl:
            body = _read_some(sock, rest, 64 * 1024, timeout=3)
            sock.close()
            nxt, kind = _parse_playlist(body.decode("utf-8", "replace"))
            if kind == "HLS":
                raise ProbeError("HLS (.m3u8 segments) - not supported by the radio")
            if not nxt:
                raise ProbeError(f"empty {kind} playlist")
            notes.append(f"via {kind} playlist")
            cur = urllib.parse.urljoin(cur, nxt)
            continue
        return sock, code, hdrs, rest, cms, tls, cur
    raise ProbeError("too many redirects / playlist hops")


def playlist_entries(text):
    """All (name, url) entries of an .m3u / .pls file's text (names may be '')."""
    t = text.lstrip("\ufeff").strip()
    out = []
    if t.lower().startswith("[playlist]"):
        files, titles = {}, {}
        for ln in t.splitlines():
            m = re.match(r"\s*(File|Title)(\d+)\s*=\s*(.*\S)", ln, re.I)
            if m:
                (files if m.group(1).lower() == "file" else titles)[int(m.group(2))] = m.group(3)
        return [(titles.get(k, ""), files[k]) for k in sorted(files)
                if re.match(r"https?://", files[k], re.I)]
    name = ""
    for ln in t.splitlines():
        ln = ln.strip()
        if ln.upper().startswith("#EXTINF"):
            name = ln.split(",", 1)[1].strip() if "," in ln else ""
        elif ln and not ln.startswith("#"):
            if re.match(r"https?://", ln, re.I):
                out.append((name, ln))
            name = ""
    return out


def probe(url, seconds=8.0, timeout=8.0):
    res = dict(status="FAIL", codec="", kbps="", rate="", ch="", tls="",
               final_url="", icy_name="", icy_genre="", connect_ms="", speed="",
               title="", notes=[], hops=[])
    cur = url.strip()
    if cur != url:
        res["notes"].append("URL had surrounding whitespace")
    if " " in cur:
        res["notes"].append("URL contains a space")
        return res
    try:
        sock, code, hdrs, rest, cms, tls, cur = open_stream(cur, timeout, True, res["hops"], res["notes"])
        res.update(final_url=cur if cur != url.strip() else "",
                   tls="y" if tls else "n", connect_ms=cms,
                   icy_name=hdrs.get("icy-name", ""), icy_genre=hdrs.get("icy-genre", ""))
        ct = hdrs.get("content-type", "")
        nominal = None
        m = re.match(r"\d+", hdrs.get("icy-br", ""))
        if m:
            nominal = int(m.group())
        metaint = int(hdrs.get("icy-metaint", "0") or 0)

        # stream for `seconds`, de-interleave ICY metadata, time it
        sock.settimeout(timeout)
        audio = bytearray()
        meta_txt = []
        pending = bytearray(rest)
        to_meta = metaint
        t_start = time.monotonic()
        marks = []                       # (t, total audio bytes)
        need_more = False                # a metadata block is only partly in `pending`
        while True:
            now = time.monotonic()
            if now - t_start >= seconds:
                break
            if not pending or need_more:
                need_more = False
                try:
                    c = sock.recv(16384)
                except socket.timeout:
                    res["notes"].append("stream stalled")
                    break
                if not c:
                    res["notes"].append("server closed stream")
                    break
                pending += c
            while pending:
                if metaint and to_meta == 0:
                    ln = pending[0] * 16
                    if len(pending) < 1 + ln:
                        need_more = True     # wait for the rest of it from the socket
                        break
                    if ln:
                        meta_txt.append(bytes(pending[1:1 + ln]).rstrip(b"\0").decode("utf-8", "replace"))
                    del pending[:1 + ln]
                    to_meta = metaint
                    continue
                take = len(pending) if not metaint else min(len(pending), to_meta)
                audio += pending[:take]
                del pending[:take]
                if metaint:
                    to_meta -= take
            marks.append((time.monotonic(), len(audio)))
            if metaint and to_meta == 0 and pending == b"":
                pass
        sock.close()
        elapsed = time.monotonic() - t_start

        if meta_txt:
            m = re.search(r"StreamTitle='(.*?)';", meta_txt[-1])
            if m:
                res["title"] = m.group(1)

        h = sniff(bytes(audio[:256 * 1024]))
        if not h:
            if not audio:
                raise ProbeError("no audio data received")
            raise ProbeError(f"unrecognised stream (content-type {ct or '?'})")
        if h["codec"] == "MP3":
            res.update(codec="MP3", rate=h["rate"], ch=h["ch"])
            nominal = nominal or h["kbps"]
        elif h["codec"] == "AAC":
            name, rate, ch = classify_aac(h, nominal)
            res.update(codec=name, rate=rate, ch=ch)
            if "aacp" in ct.lower() and name == "AAC-LC":
                res["notes"].append("content-type says aacp (HE) but ADTS looks LC")
        else:
            res["codec"] = h["codec"]
            res["notes"].append(f"{h['codec']} not supported (MP3/AAC only)")
            res["status"] = "FAIL"
            return res

        # sustained rate: ignore the server's connect burst (first ~2 s)
        meas = None
        if elapsed >= 4 and marks:
            base = next(((t, n) for t, n in marks if t - t_start >= 2.0), None)
            if base and marks[-1][0] - base[0] > 1.0:
                meas = (marks[-1][1] - base[1]) * 8 / 1000 / (marks[-1][0] - base[0])
        if meas is None and elapsed > 0:
            meas = len(audio) * 8 / 1000 / elapsed
        if nominal:
            res["kbps"] = nominal
            ratio = meas / nominal if nominal else 0
            res["speed"] = f"{ratio:.2f}x"
        else:
            res["kbps"] = int(round(meas))
            res["notes"].append("bitrate estimated from throughput")

        # verdict
        problems, warns = [], []
        if res["codec"] not in SUPPORTED:
            problems.append(f"{res['codec']} not supported (MP3/AAC only)")
        if res["rate"] and res["rate"] not in (32000, 44100, 48000, 22050, 24000, 16000):
            warns.append(f"unusual sample rate {res['rate']}")
        if nominal and meas < 0.92 * nominal and elapsed >= 4:
            warns.append(f"throughput only {meas:.0f} of {nominal} kbps")
        res["notes"] += problems + warns
        res["status"] = "FAIL" if problems else ("WARN" if warns else "OK")
    except ProbeError as e:
        res["notes"].append(str(e))
    except Exception as e:                          # keep a batch run going
        res["notes"].append(f"{type(e).__name__}: {e}")
    return res


def apply_probe(row, res, fill_name=True):
    for k in ("status", "codec", "kbps", "rate", "ch", "tls", "final_url",
              "icy_name", "connect_ms", "speed"):
        row[k] = str(res.get(k, ""))
    row["tested"] = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    auto = "; ".join(res["notes"])
    row["notes"] = auto
    if not row.get("genre") and res.get("icy_genre"):
        row["genre"] = res["icy_genre"][:40]
    if fill_name and not row.get("name") and res.get("icy_name"):
        row["name"] = res["icy_name"]


def describe(res):
    bits = [res["status"]]
    if res["codec"] and res["rate"]:
        bits.append(f"{res['codec']} {res['kbps']}k {res['rate']}Hz {'stereo' if str(res['ch']) == '2' else 'mono' if res['ch'] else ''}".strip())
    elif res["codec"]:
        bits.append(res["codec"])
    if res["tls"] == "y":
        bits.append("HTTPS")
    if res["speed"]:
        bits.append(f"speed {res['speed']}")
    if res["connect_ms"] != "":
        bits.append(f"{res['connect_ms']} ms")
    s = "  ".join(bits)
    if res["notes"]:
        s += "\n      " + "; ".join(res["notes"])
    if res.get("title"):
        s += f"\n      now: {res['title']}"
    return s


# --------------------------------------------------------------------------
# station log
# --------------------------------------------------------------------------

def write_log(rows, path):
    now = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    n = len(rows)
    cnt = {s: sum(1 for r in rows if r["status"] == s) for s in ("OK", "WARN", "FAIL")}
    kept = [r for r in rows if r["keep"].lower().startswith("y") and r["status"] != "FAIL"]
    out = io.StringIO()
    out.write(f"# P4 radio - station log\n\nGenerated {now} by p4radio {VERSION}.  ")
    out.write(f"{n} stations: {cnt['OK']} OK, {cnt['WARN']} warning, {cnt['FAIL']} failed; "
              f"{len(kept)} will go into stations.txt.\n\n")
    out.write("| # | Keep | Station | Status | Codec | kbps | Rate | Ch | TLS | Speed | Conn ms | P4 CPU | Notes |\n")
    out.write("|---|---|---|---|---|---|---|---|---|---|---|---|---|\n")
    for i, r in enumerate(rows, 1):
        nm = r["name"].replace("|", "/")
        if len(nm) > NAME_WARN:
            nm += " ⚠"
        notes = r["notes"].replace("|", "/")
        out.write(f"| {i} | {r['keep']} | {nm} | {r['status'] or '-'} | {r['codec']} | {r['kbps']} | "
                  f"{r['rate']} | {r['ch']} | {r['tls']} | {r['speed']} | {r['connect_ms']} | "
                  f"{P4_CPU.get(r['codec'], '')} | {notes} |\n")
    out.write("\n## Codec mix of kept stations\n\n")
    mix = {}
    for r in kept:
        mix[r["codec"] or "?"] = mix.get(r["codec"] or "?", 0) + 1
    for k, v in sorted(mix.items(), key=lambda x: -x[1]):
        out.write(f"- {k}: {v}\n")
    out.write(f"\n⚠ = name longer than {NAME_WARN} characters (busy on the player screen).  "
              "Speed = sustained throughput / nominal bitrate after the connect burst; "
              "below ~0.92x the radio's buffer will drain.  P4 CPU = decode-time load "
              "measured on the bench at 360 MHz.\n\n## URLs\n\n")
    for i, r in enumerate(rows, 1):
        out.write(f"{i}. {r['name']} - <{r['url']}>")
        if r["final_url"]:
            out.write(f" → <{r['final_url']}>")
        out.write("\n")
    with open(path, "w", encoding="utf-8") as f:
        f.write(out.getvalue())
    return cnt


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------

def cmd_import(a):
    rows = load_lib(a.library)
    cfg = load_cfg(a.config)
    if os.path.isdir(a.file):                   # a stick (or its radio/ folder)
        root = os.path.dirname(a.file.rstrip("/")) if os.path.basename(a.file.rstrip("/")) == "radio" \
            else a.file
        logo_dir = os.path.join(os.path.dirname(os.path.abspath(a.library)), "logos")
        rows, info, added = import_stick(root, rows, logo_dir)
        save_lib(a.library, rows)
        if info["settings"]:
            cfg["settings"] = info["settings"]
            save_cfg(a.config, cfg)
        print(f"imported {added} stations and {len(info['logos'])} logos from {root} "
              f"({len(rows)} total); settings: {info['settings'] or 'none'}")
        print("they are untested: run `test`, or `stick --include-untested` to write them back as they are")
        return
    added, layout = 0, None
    with open(a.file, encoding="utf-8", errors="replace") as f:
        for ln in f:
            raw = ln.rstrip("\r\n")
            if not raw.strip() or raw.lstrip().startswith(("#", ";")):
                continue
            m = re.search(r"https?://\S+", raw)
            if not m:
                print(f"  skip (no URL): {raw[:70]}")
                continue
            url = m.group().rstrip(",;|\t")
            before, after = raw[:m.start()], raw[m.end():]
            if layout is None:
                if before.strip():
                    sep = re.search(r"[|,;\t=]\s*$", before)
                    layout = "{name}" + (sep.group().strip() if sep and sep.group().strip() else "\t" if sep else " ") + "{url}"
                elif after.strip():
                    sep = re.match(r"\s*[|,;\t=]", after)
                    layout = "{url}" + (sep.group().strip() if sep and sep.group().strip() else "\t" if sep else " ") + "{name}"
                else:
                    layout = "{url}"
            name = (before + " " + after).strip(" \t|,;=")
            if find_url(rows, url):
                continue
            rows.append(new_row(name, url))
            added += 1
    save_lib(a.library, rows)
    if layout and not a.keep_format:
        cfg["line"] = layout
        save_cfg(a.config, cfg)
    print(f"imported {added} stations into {a.library} ({len(rows)} total)")
    print(f"line layout detected: {layout!r}  (used by `build`, change in {a.config})")


def rb_servers():
    try:
        infos = socket.getaddrinfo("all.api.radio-browser.info", 443, proto=socket.IPPROTO_TCP)
        names = set()
        for info in infos:
            try:
                names.add(socket.gethostbyaddr(info[4][0])[0])
            except OSError:
                pass
        if names:
            return sorted(names)
    except OSError:
        pass
    return ["de1.api.radio-browser.info", "de2.api.radio-browser.info", "fi1.api.radio-browser.info"]


def rb_get(path, params=None):
    q = urllib.parse.urlencode({k: v for k, v in (params or {}).items() if v not in (None, "")})
    last = None
    for host in rb_servers():
        try:
            req = urllib.request.Request(f"https://{host}/json/{path}" + (f"?{q}" if q else ""),
                                         headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.load(r)
        except Exception as e:
            last = e
    raise SystemExit(f"radio-browser.info not reachable: {last}")


def rb_search(params):
    return rb_get("stations/search", params)


def rb_countries():
    """[(code, name, station count)], most stations first."""
    out = [(c.get("iso_3166_1", ""), c.get("name", ""), c.get("stationcount", 0))
           for c in rb_get("countries")]
    return sorted((c for c in out if c[0] and c[2]), key=lambda c: -c[2])


def cmd_search(a):
    q = dict(name=a.name, tag=a.tag, countrycode=a.country, codec=a.codec,
             language=a.language, bitrateMin=a.min_kbps,
             hidebroken="true", order="clickcount", reverse="true")
    if a.limit:
        res = rb_search(dict(q, limit=a.limit))
    else:                                   # 0 = everything; server caps unpaged replies at 1000
        res, page = [], 1000
        while True:
            chunk = rb_search(dict(q, limit=page, offset=len(res)))
            res += chunk
            if len(chunk) < page:
                break
    if not res:
        print("nothing found")
        return
    for i, s in enumerate(res, 1):
        https = "S" if s.get("url_resolved", "").startswith("https") else " "
        hls = " HLS" if s.get("hls") else ""
        print(f"{i:3d}. {s['name'].strip()[:40]:40s} {s.get('codec',''):5s} {s.get('bitrate',0):4}k {https} "
              f"{s.get('countrycode',''):2s} {s.get('tags','')[:28]}{hls}")
    pick = a.add
    if pick is None and sys.stdin.isatty():
        pick = input("\nadd which? (e.g. 1,3,5-7 / all / Enter for none): ").strip()
    if not pick:
        return
    rows = load_lib(a.library)
    idx = range(1, len(res) + 1) if pick == "all" else [i for i, _ in select([None] * len(res), pick)]
    n = 0
    for i in idx:
        s = res[i - 1]
        url = s.get("url") or s.get("url_resolved")
        if find_url(rows, url):
            continue
        rows.append(new_row(s["name"], url, genre=(s.get("tags") or "").split(",")[0],
                            country=s.get("countrycode", ""), logo=s.get("favicon", "")))
        n += 1
    save_lib(a.library, rows)
    print(f"added {n}; run `test` to probe them")


def cmd_add(a):
    rows = load_lib(a.library)
    if find_url(rows, a.url):
        raise SystemExit("already in library")
    print(f"probing {a.url} ...")
    res = probe(a.url, a.seconds)
    print("  " + describe(res))
    r = new_row(a.name or res.get("icy_name") or "", a.url, genre=a.genre, logo=a.logo)
    apply_probe(r, res)
    if res["status"] == "FAIL" and not a.force:
        raise SystemExit("not added (use --force to add anyway)")
    rows.append(r)
    save_lib(a.library, rows)
    print(f"added as #{len(rows)} '{r['name']}'")


def cmd_probe(a):
    res = probe(a.url, a.seconds)
    for h in res["hops"]:
        print(f"  -> {h}")
    print("  " + describe(res))
    if res.get("icy_name"):
        print(f"      icy-name: {res['icy_name']}   genre: {res['icy_genre']}")


def cmd_test(a):
    rows = load_lib(a.library)
    todo = select(rows, a.which)
    if a.untested:
        todo = [(i, r) for i, r in todo if not r["tested"]]
    if not todo:
        print("nothing to test")
        return
    print(f"testing {len(todo)} stations, {a.seconds:.0f} s each, {a.jobs} at a time ...")
    with cf.ThreadPoolExecutor(max_workers=a.jobs) as ex:
        futs = {ex.submit(probe, r["url"], a.seconds): (i, r) for i, r in todo}
        for f in cf.as_completed(futs):
            i, r = futs[f]
            res = f.result()
            apply_probe(r, res)
            print(f"{i:3d}. {r['name'][:34]:34s} {describe(res)}")
    save_lib(a.library, rows)
    cnt = write_log(rows, a.log)
    os.makedirs("logs", exist_ok=True)
    shutil.copy(a.log, os.path.join("logs", dt.datetime.now().strftime("station_log_%Y%m%d_%H%M.md")))
    print(f"\n{cnt['OK']} OK, {cnt['WARN']} WARN, {cnt['FAIL']} FAIL  ->  {a.log}")


def cmd_list(a):
    rows = load_lib(a.library)
    for i, r in enumerate(rows, 1):
        flag = "⚠" if len(r["name"]) > NAME_WARN else " "
        print(f"{i:3d}. [{r['keep'] or '-'}] {r['name'][:36]:36s}{flag} {r['status'] or '----':4s} "
              f"{r['codec']:8s} {(r['kbps'] + 'k') if r['kbps'] else '':>5}  {r['url'][:60]}")


def cmd_set(a):
    rows = load_lib(a.library)
    sel = select(rows, a.which)
    for i, r in sel:
        if a.keep is not None:
            r["keep"] = a.keep
        if a.name is not None:
            r["name"] = a.name
        if a.genre is not None:
            r["genre"] = a.genre
        if a.logo is not None:
            r["logo"] = a.logo
        if a.url is not None:
            r["url"] = a.url
            r["tested"] = ""
    save_lib(a.library, rows)
    print(f"updated {len(sel)} row(s)")


def cmd_move(a):
    rows = load_lib(a.library)
    if not (1 <= a.src <= len(rows) and 1 <= a.dst <= len(rows)):
        raise SystemExit("index out of range")
    r = rows.pop(a.src - 1)
    rows.insert(a.dst - 1, r)
    save_lib(a.library, rows)
    print(f"moved '{r['name']}' to #{a.dst}")


def cmd_rm(a):
    rows = load_lib(a.library)
    gone = {id(r) for _, r in select(rows, a.which)}
    keep = [r for r in rows if id(r) not in gone]
    save_lib(a.library, keep)
    print(f"removed {len(rows) - len(keep)}")


def cmd_listen(a):
    player = shutil.which("mpv")
    if not player:
        raise SystemExit("mpv not found (sudo apt install mpv)")
    rows = load_lib(a.library)
    sel = select(rows, a.which)
    if a.undecided:
        sel = [(i, r) for i, r in sel if r["keep"] not in ("y", "n")]
    print("keys: [k]eep  [d]rop  [n]ext  [r]eplay  [e]dit name  [q]uit\n")
    try:
        for i, r in sel:
            while True:
                print(f"#{i} {r['name']}  ({r['codec']} {r['kbps']}k {r['status']})  keep={r['keep'] or '-'}")
                p = subprocess.Popen([player, "--no-video", "--really-quiet",
                                      "--term-playing-msg=  ${media-title}", r["url"]],
                                     stdin=subprocess.DEVNULL)
                try:
                    ans = input("  > ").strip().lower()[:1]
                finally:
                    p.terminate()
                    try:
                        p.wait(3)
                    except subprocess.TimeoutExpired:
                        p.kill()
                if ans == "r":
                    continue
                if ans == "e":
                    r["name"] = input("  new name: ").strip() or r["name"]
                    continue
                break
            if ans == "k":
                r["keep"] = "y"
            elif ans == "d":
                r["keep"] = "n"
            elif ans == "q":
                break
    except (KeyboardInterrupt, EOFError):
        print()
    save_lib(a.library, rows)
    print("saved")


def logo_ready(data, size):
    """True if data is already a logo the radio takes as it is: a size x size
    baseline JPEG, 3-component 4:2:0, under 256 KB."""
    if len(data) >= FW_LOGO_MAX or data[:2] != b"\xff\xd8":
        return False
    try:
        from PIL import Image
        im = Image.open(io.BytesIO(data))
        return (im.format == "JPEG" and im.size == (size, size) and im.mode == "RGB"
                and not im.info.get("progressive") and not im.info.get("progression")
                and [layer[1:3] for layer in im.layer] == [(2, 2), (1, 1), (1, 1)])
    except Exception:
        return False


def render_logo(src, size, bg):
    """Logo from a file path or URL -> baseline 4:2:0 JPEG bytes, size x size."""
    try:
        from PIL import Image
    except ImportError:
        raise SystemExit("Pillow needed for logos:  pip install pillow")
    if re.match(r"https?://", src, re.I):
        req = urllib.request.Request(src, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=10) as r:
            data = r.read(4 * 1024 * 1024)
    else:
        with open(os.path.expanduser(src), "rb") as f:
            data = f.read()
    if logo_ready(data, size):
        return data                              # already what the radio wants: keep the bytes
    im = Image.open(io.BytesIO(data))
    if getattr(im, "n_frames", 1) > 1:          # .ico: pick the largest
        best = max(range(im.n_frames), key=lambda k: (im.seek(k), im.size[0])[1])
        im.seek(best)
    im = im.convert("RGBA")
    if im.width < 48:
        raise ValueError(f"source only {im.width}px - too small")
    canvas = Image.new("RGB", (size, size), bg)
    scale = min(size / im.width, size / im.height)
    if scale > 1 and im.width < size // 2:
        scale = min(scale, 2.0)                  # don't blow up tiny favicons into mush
    w, h = max(1, int(im.width * scale)), max(1, int(im.height * scale))
    im = im.resize((w, h), Image.LANCZOS)
    canvas.paste(im, ((size - w) // 2, (size - h) // 2), im)
    # baseline JPEG, 4:2:0 - what the P4 hardware decoder wants
    for quality in (90, 80, 70, 60):
        buf = io.BytesIO()
        canvas.save(buf, "JPEG", quality=quality, optimize=False, progressive=False, subsampling=2)
        if buf.tell() < FW_LOGO_MAX:
            return buf.getvalue()
    raise ValueError("logo still over 256 KB at quality 60")


def pick_rows(rows, include_untested=False, include_failed=False):
    """The stations that go to the radio: keep=y and tested OK/WARN, in library order."""
    allow = {"OK", "WARN", ""} if include_untested else {"OK", "WARN"}
    if include_failed:
        allow |= {"FAIL"}
    return [r for r in rows if r["keep"].lower().startswith("y") and r["status"] in allow]


def write_logos(out_rows, size, bg, jobs=6, log=print, render=None):
    """Renders the logos -> {station number: jpeg bytes}.  Stations without a
    usable logo are left out (the radio shows the initials tile)."""
    bgc = tuple(int(bg.lstrip("#")[k:k + 2], 16) for k in (0, 2, 4))
    want = [(n, r) for n, r in enumerate(out_rows, 1) if r["logo"]]
    for n, r in enumerate(out_rows, 1):
        if not r["logo"]:
            log(f"  {n:3d}. {r['name'][:30]:30s} no logo (radio shows initials)")
    out = {}
    with cf.ThreadPoolExecutor(jobs) as ex:
        futs = {ex.submit(render or render_logo, r["logo"], size, bgc): (n, r) for n, r in want}
        for fu in cf.as_completed(futs):
            n, r = futs[fu]
            try:
                out[n] = fu.result()
            except SystemExit:
                raise
            except Exception as e:
                log(f"  {n:3d}. {r['name'][:30]:30s} logo failed: {e}")
    return out


def cmd_build(a):
    rows = load_lib(a.library)
    cfg = load_cfg(a.config)
    line = a.line or cfg["line"]
    out_rows = pick_rows(rows, a.include_untested, a.include_failed)
    if not out_rows:
        raise SystemExit("no stations to write (run `test` first, or --include-untested)")
    os.makedirs(a.out, exist_ok=True)
    path = os.path.join(a.out, "stations.txt")
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        if a.header:
            f.write(f"# P4 radio stations - built {dt.datetime.now():%Y-%m-%d %H:%M} by p4radio\n")
        for r in out_rows:
            url = r["final_url"] if (a.resolved and r["final_url"]) else r["url"]
            f.write(line.format(name=r["name"].replace("|", "/").strip(), url=url.strip(),
                                genre=r["genre"], codec=r["codec"]) + "\n")
    print(f"wrote {len(out_rows)} stations -> {path}")
    long_names = [r["name"] for r in out_rows if len(r["name"]) > NAME_WARN]
    if long_names:
        print(f"  note: {len(long_names)} name(s) over {NAME_WARN} chars: " + ", ".join(long_names[:5]))

    if a.logos:
        # Same place and names as on the radio's stick: N.jpg next to stations.txt.
        for f in os.listdir(a.out):
            if fw_logo_number(f):
                os.remove(os.path.join(a.out, f))
        logos = write_logos(out_rows, a.size, a.bg)
        for n, data in logos.items():
            with open(os.path.join(a.out, f"{n}.jpg"), "wb") as f:
                f.write(data)
        print(f"wrote {len(logos)} logos ({a.size}x{a.size} baseline JPEG) -> {a.out}/N.jpg")


# --------------------------------------------------------------------------
# the radio's USB stick
# --------------------------------------------------------------------------
#
# What the firmware reads (p4-radio main/station_parse.c, storage.c, usb_service.c):
#   radio/stations.txt  "name | url" per line, '#' = comment
#   radio/N.jpg         logo of station N
#   radio/config.txt    key = value
#   radio/wifi.txt      SSID, then password
# and it writes radio/report.txt (import) and radio/update.txt (firmware update).

def fw_bytes(s):
    return len(s.encode("utf-8"))


def _cut_utf8(s, limit):
    """Longest prefix of s that is at most limit bytes of UTF-8."""
    while fw_bytes(s) > limit:
        s = s[:-1]
    return s


def fw_name(name):
    """Station name the radio will accept -> (name, note or '')."""
    note = ""
    clean = "".join(ch if ch.isprintable() else " " for ch in name)
    clean = re.sub(r"\s+", " ", clean.replace("|", "/")).strip()
    clean = clean.lstrip("#").strip()          # a leading '#' would make the line a comment
    if fw_bytes(clean) > FW_NAME_MAX:
        clean = _cut_utf8(clean, FW_NAME_MAX).rstrip()
        note = f"name cut to {FW_NAME_MAX} bytes: {clean!r}"
    return clean, note


def fw_url(url):
    """URL the radio will accept, or raise ValueError with the reason."""
    u = url.strip()
    m = re.match(r"(https?)://", u, re.I)
    if not m:
        raise ValueError("not an http:// or https:// URL")
    u = m.group(1).lower() + u[len(m.group(1)):]   # the radio only knows lower-case schemes
    if re.search(r"\s", u):
        raise ValueError("URL contains spaces")
    u = u.replace("|", "%7C")                        # '|' separates name from URL
    if fw_bytes(u) > FW_URL_MAX:
        raise ValueError(f"URL longer than {FW_URL_MAX} bytes")
    return u


def stations_text(out_rows):
    """-> (stations.txt text, rows written, [problems]).  Rows the radio would
    reject are left out, so line N always matches logo N."""
    lines, kept, notes = ["# P4 Radio stations, written by p4radio"], [], []
    for r in out_rows:
        try:
            url = fw_url(r["url"])
        except ValueError as e:
            notes.append(f"left out {r['name']!r}: {e}")
            continue
        if len(kept) >= FW_STATIONS_MAX:
            notes.append(f"left out {r['name']!r}: the radio holds at most {FW_STATIONS_MAX} stations")
            continue
        name, note = fw_name(r["name"])
        if note:
            notes.append(note)
        lines.append(f"{name} | {url}" if name else url)
        kept.append(r)
    return "\n".join(lines) + "\n", kept, notes


def wifi_text(ssid, password):
    """wifi.txt contents, or raise ValueError with a plain reason."""
    if not ssid or not ssid.strip():
        raise ValueError("the WiFi network name is empty")
    if "\n" in ssid or "\r" in ssid or "\n" in password or "\r" in password:
        raise ValueError("line breaks are not allowed in the WiFi name or password")
    if ssid.lstrip().startswith("#"):
        raise ValueError("the radio cannot use a WiFi network name that starts with '#'")
    if fw_bytes(ssid) > FW_SSID_MAX:
        raise ValueError(f"the WiFi network name is longer than {FW_SSID_MAX} bytes")
    if password and not 8 <= fw_bytes(password) <= FW_PASS_MAX:
        raise ValueError(f"WiFi passwords are 8 to {FW_PASS_MAX} characters "
                         "(leave it empty for an open network)")
    return f"{ssid}\n{password}\n" if password else f"{ssid}\n"


def tz_valid(tz):
    """Same test as the firmware's config_timezone_valid()."""
    if not tz or len(tz) > FW_TZ_MAX or any(not ("!" <= c <= "~") for c in tz):
        return False
    m = re.match(r"<[^>]{3,}>" if tz.startswith("<") else r"[A-Za-z]{3,}", tz)
    if not m:
        return False
    rest = tz[m.end():]
    return bool(re.match(r"[+-]?\d", rest))


def tz_posix(value):
    """POSIX TZ string from a POSIX string, a zone name like Europe/London, or
    'local' (this computer's zone).  Raises ValueError."""
    if tz_valid(value) and "/" not in value:
        return value
    if value == "local":
        paths = ["/etc/localtime"]
    else:
        try:
            import zoneinfo
            dirs = list(zoneinfo.TZPATH)
        except ImportError:
            dirs = ["/usr/share/zoneinfo"]
        paths = [os.path.join(d, value) for d in dirs]
    for p in paths:
        try:
            with open(p, "rb") as f:
                data = f.read()
        except OSError:
            continue
        # TZif v2+ ends with "\n<POSIX TZ>\n"
        if data[:4] == b"TZif" and data[4:5] >= b"2":
            footer = data.rstrip(b"\n").rsplit(b"\n", 1)[-1].decode("ascii", "replace")
            if tz_valid(footer):
                return footer
    raise ValueError(f"unknown timezone {value!r} (use e.g. Europe/London, or a POSIX string "
                     "like GMT0BST,M3.5.0/1,M10.5.0)")


def config_text(settings):
    """config.txt from {'timezone','eq','brightness'} (any may be missing)."""
    lines = ["# P4 Radio settings, written by p4radio"]
    tz = settings.get("timezone")
    if tz:
        if not tz_valid(tz):
            raise ValueError(f"timezone {tz!r} is not a POSIX TZ string")
        lines.append(f"timezone = {tz}")
    eq = settings.get("eq")
    if eq:
        if eq not in EQ_PRESETS:
            raise ValueError(f"eq must be one of: {' '.join(EQ_PRESETS)}")
        lines.append(f"eq = {eq}")
    br = settings.get("brightness")
    if br not in (None, ""):
        if not 20 <= int(br) <= 100:
            raise ValueError("brightness must be 20 to 100")
        lines.append(f"brightness = {int(br)}")
    return "\n".join(lines) + "\n" if len(lines) > 1 else None


def _mount_of(path):
    """(device, mount point, fs type) from /proc/mounts for the mount holding path."""
    path = os.path.realpath(path)
    best = None
    try:
        with open("/proc/mounts") as f:
            for ln in f:
                dev, mnt, fs = ln.split()[:3]
                mnt = mnt.replace("\\040", " ")
                if path == mnt or path.startswith(mnt.rstrip("/") + "/"):
                    if best is None or len(mnt) > len(best[1]):
                        best = (dev, mnt, fs)
    except OSError:
        pass
    return best


def _removable(dev):
    """True if the block device (or its disk) is removable or hot-plugged (Linux)."""
    if not shutil.which("lsblk"):
        return None
    try:
        out = subprocess.run(["lsblk", "-sno", "RM,HOTPLUG", dev], capture_output=True,
                             text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    return "1" in out.split()


def check_stick(root, force=False):
    """Refuses anything that is not the top of a FAT32 USB stick.  force=True
    allows a plain folder (for tests, or to prepare files to copy by hand)."""
    if not os.path.isdir(root):
        raise SystemExit(f"{root} is not a folder (is the stick plugged in and mounted?)")
    if force:
        return
    m = _mount_of(root)
    if m is None:              # not Linux: nothing to check against yet
        return
    dev, mnt, fs = m
    if os.path.realpath(root) != mnt:
        raise SystemExit(f"{root} is not the top of a drive (that is {mnt}); "
                         "use --force to write into a plain folder")
    if fs not in ("vfat", "msdos"):
        raise SystemExit(f"{mnt} is {fs}, the radio needs FAT32. Copy anything you need off it, "
                         "then reformat it as FAT32 (sticks of 64 GB and up usually come as exFAT)")
    if _removable(dev) is False:
        raise SystemExit(f"{mnt} ({dev}) is not a removable drive; refusing to write to it")


def _write_if_changed(path, data, dry_run):
    """-> 'new', 'changed' or 'same'.  Writes to a temp name, then renames."""
    try:
        with open(path, "rb") as f:
            if f.read() == data:
                return "same"
        state = "changed"
    except FileNotFoundError:
        state = "new"
    if not dry_run:
        tmp = path + ".p4tmp"
        with open(tmp, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    return state


def write_stick(root, out_rows, settings=None, wifi=None, wifi_remove=False, logos=None,
                project=(), dry_run=False, log=print):
    """Writes the radio/ folder of a stick.

    out_rows   stations in radio order (see pick_rows)
    settings   {'timezone','eq','brightness'} for config.txt (None: leave config.txt)
    wifi       (ssid, password) to write wifi.txt; None leaves it as it is
    logos      {station number: jpeg bytes} from write_logos, None leaves logos as they are
    project    local files copied to p4radio/ on the stick (library, config)
    Returns {'stations': n, 'logos': n, 'changed': [...], 'notes': [...]}.
    """
    rdir = os.path.join(root, "radio")
    if not dry_run:
        os.makedirs(rdir, exist_ok=True)
    changed, notes = [], []
    for f in (os.listdir(rdir) if os.path.isdir(rdir) else []):
        if f.endswith(".p4tmp") and not dry_run:      # left by an interrupted write
            os.remove(os.path.join(rdir, f))

    def put(name, data):
        st = _write_if_changed(os.path.join(rdir, name), data, dry_run)
        if st != "same":
            changed.append(f"{name} ({st})")
        return st

    text, kept, snotes = stations_text(out_rows)
    notes += snotes
    if not kept:
        raise SystemExit("no stations the radio can use")
    put("stations.txt", text.encode("utf-8"))

    if settings is not None:
        ct = config_text(settings)
        if ct:
            put("config.txt", ct.encode("utf-8"))

    wpath = os.path.join(rdir, "wifi.txt")
    if wifi is not None:
        put("wifi.txt", wifi_text(*wifi).encode("utf-8"))
        notes.append("wifi.txt holds the WiFi password in plain text")
    elif wifi_remove and os.path.exists(wpath):
        if not dry_run:
            os.remove(wpath)
        changed.append("wifi.txt (removed)")

    n_logos = None
    if logos is not None:
        # Logos follow station positions; rows left out by stations_text shift them.
        pos = {id(r): n for n, r in enumerate(out_rows, 1)}
        by_station = {}
        for n, r in enumerate(kept, 1):
            data = logos.get(pos[id(r)])
            if data is not None:
                by_station[n] = data
        want = {f"{n}.jpg" for n in by_station}
        existing = os.listdir(rdir) if os.path.isdir(rdir) else []
        for f in existing:
            # stale numbers, and 3.jpeg / 3.JPE next to a new 3.jpg (the radio
            # would call that a second logo for station 3)
            if fw_logo_number(f) and f not in want:
                if not dry_run:
                    os.remove(os.path.join(rdir, f))
                changed.append(f"{f} (removed)")
        for n in sorted(by_station):
            put(f"{n}.jpg", by_station[n])
        n_logos = len(by_station)
        if not by_station:
            notes.append("no logos on the stick: the radio keeps the logos it already has")

    for f in (os.listdir(rdir) if os.path.isdir(rdir) else []):
        if f.lower().endswith((".jpg", ".jpeg", ".jpe")) and not f.startswith(".") \
                and not fw_logo_number(f):
            notes.append(f"radio/{f} is not a station logo name; the radio will list it in report.txt")

    if project:
        pdir = os.path.join(root, "p4radio")
        if not dry_run:
            os.makedirs(pdir, exist_ok=True)
        for src in project:
            if os.path.exists(src):
                with open(src, "rb") as f:
                    _write_if_changed(os.path.join(pdir, os.path.basename(src)), f.read(), dry_run)

    if not dry_run and hasattr(os, "sync"):
        os.sync()
    return {"stations": len(kept), "logos": n_logos, "changed": changed, "notes": notes}


def _read_text(path, limit=256 * 1024):
    try:
        with open(path, "rb") as f:
            t = f.read(limit).decode("utf-8", "replace")
    except OSError:
        return None
    return t[1:] if t.startswith("\ufeff") else t


def read_stick(root):
    """What is on a stick, read with the radio's rules.
    -> {'stations': [(name, url)], 'logos': {n: path}, 'settings': {...},
        'wifi_ssid': str or None, 'report': text or None, 'project': bool}"""
    rdir = os.path.join(root, "radio")
    out = {"stations": [], "logos": {}, "settings": {}, "wifi_ssid": None, "report": None,
           "project": os.path.exists(os.path.join(root, "p4radio", "library.csv"))}
    t = _read_text(os.path.join(rdir, "stations.txt"))
    for ln in (t or "").splitlines():
        ln = ln.strip()
        if not ln or ln.startswith("#"):
            continue
        name, bar, url = ln.partition("|")
        if not bar:
            name, url = "", ln
        name, url = name.strip(), url.strip()
        if url.startswith(("http://", "https://")) and fw_bytes(url) <= FW_URL_MAX \
                and fw_bytes(name) <= FW_NAME_MAX and len(out["stations"]) < FW_STATIONS_MAX:
            out["stations"].append((name, url))
    if os.path.isdir(rdir):
        for f in sorted(os.listdir(rdir), key=lambda f: (not f.lower().endswith(".jpg"), f)):
            n = fw_logo_number(f)
            if n and not f.startswith(".") and n not in out["logos"]:
                out["logos"][n] = os.path.join(rdir, f)
    t = _read_text(os.path.join(rdir, "config.txt"))
    for ln in (t or "").splitlines():
        key, eq, value = ln.split("#", 1)[0].partition("=")
        key, value = key.strip().lower(), value.strip()
        if not eq:
            continue
        if key == "timezone" and tz_valid(value):
            out["settings"]["timezone"] = value
        elif key == "eq" and value.lower() in EQ_PRESETS:
            out["settings"]["eq"] = value.lower()
        elif key == "brightness" and value.isdigit() and 20 <= int(value) <= 100:
            out["settings"]["brightness"] = int(value)
    t = _read_text(os.path.join(rdir, "wifi.txt"))
    for ln in (t or "").splitlines():
        if ln.strip() and not ln.strip().startswith("#"):
            out["wifi_ssid"] = ln.rstrip("\r")
            break
    out["report"] = _read_text(os.path.join(rdir, "report.txt"))
    return out


def import_stick(root, rows, logo_dir, replace=False):
    """Adds the stick's stations (in its order) to rows, copying its logos into
    logo_dir.  replace=True starts from an empty list.  -> (rows, info, added)."""
    info = read_stick(root)
    if replace:
        rows = []
    os.makedirs(logo_dir, exist_ok=True)
    added = 0
    for n, (name, url) in enumerate(info["stations"], 1):
        logo = ""
        if n in info["logos"]:
            with open(info["logos"][n], "rb") as f:
                logo = store_logo(f.read(), logo_dir)
        r = find_url(rows, url)
        if r is None:
            rows.append(new_row(name, url, logo=logo))
            added += 1
        elif logo and not r["logo"]:
            r["logo"] = logo
    return rows, info, added


def store_logo(data, logo_dir):
    """Keeps a picture in logo_dir under a name made from its contents -> path."""
    import hashlib
    kind = "jpg" if data[:2] == b"\xff\xd8" else "png" if data[:4] == b"\x89PNG" else "img"
    path = os.path.join(logo_dir, f"{hashlib.sha1(data).hexdigest()[:16]}.{kind}")
    if not os.path.exists(path):
        os.makedirs(logo_dir, exist_ok=True)
        with open(path, "wb") as f:
            f.write(data)
    return os.path.abspath(path)


# --------------------------------------------------------------------------
# music on the stick (the radio reads music/ in place; p4-radio main/library.c)
# --------------------------------------------------------------------------

FW_MUSIC_PATH_MAX = 191        # bytes of a path relative to music/ (LIBRARY_PATH_MAX 192)
FW_MUSIC_DEPTH = 8             # folders below music/
FW_MUSIC_TRACKS = 4000
FW_MUSIC_FOLDERS = 1000
FW_COVER_MAX = 2 * 1024 * 1024
FW_COVER_DIM = 1600
MUSIC_COVERS = ("cover", "folder", "front")          # .jpg next to the songs; first found wins
OTHER_AUDIO = (".flac", ".m4a", ".aac", ".ogg", ".oga", ".opus", ".wav", ".wma", ".aif",
               ".aiff", ".ape", ".alac", ".mp4", ".mpc", ".wv", ".dsf")
_FAT_BAD = re.compile(r'[\x00-\x1f"*/:<>?\\|\x7f]')
_FAT_RESERVED = {"CON", "PRN", "AUX", "NUL"} | {f"COM{i}" for i in range(1, 10)} \
    | {f"LPT{i}" for i in range(1, 10)}


def fat_name(name):
    """A file or folder name FAT32 accepts: no "*/:<>?\\| or control characters,
    no trailing dot or space, no reserved device names, at most 255 characters."""
    n = _FAT_BAD.sub("_", name).rstrip(" .") or "_"
    if n.split(".")[0].upper() in _FAT_RESERVED:
        n = "_" + n
    if len(n) > 255:
        stem, dot, ext = n.rpartition(".")
        n = (stem[:254 - len(ext)] + "." + ext) if dot and len(ext) <= 5 else n[:255]
    return n


def jpeg_problem(d, size=None):
    """Why the radio's hardware decoder won't show this JPEG, or None
    (as jpeg_unsupported() in the firmware's art_service.c, plus its size limits)."""
    if (size if size is not None else len(d)) > FW_COVER_MAX:
        return "picture over 2 MB"
    if d[:4] == b"\x89PNG":
        return "PNG picture"
    if len(d) < 4 or d[:2] != b"\xff\xd8":
        return "not a JPEG"
    i = 2
    while i + 4 <= len(d):
        if d[i] != 0xFF:
            return "damaged JPEG"
        m = d[i + 1]
        if m == 0xFF:
            i += 1
            continue
        ln = (d[i + 2] << 8) | d[i + 3]
        if m in (0xC2, 0xC6, 0xCA, 0xCE):
            return "progressive JPEG"
        if m in (0xC0, 0xC1):
            if i + 12 > len(d):
                return None                      # cut short here: can't tell, assume fine
            h, w = (d[i + 5] << 8) | d[i + 6], (d[i + 7] << 8) | d[i + 8]
            nf = d[i + 9]
            if nf == 3 and d[i + 11] not in (0x11, 0x21, 0x22):
                return "unsupported JPEG sampling"
            if nf not in (1, 3):
                return "unsupported JPEG colour format"
            if w > FW_COVER_DIM or h > FW_COVER_DIM:
                return f"picture larger than {FW_COVER_DIM} pixels"
            return None
        if (0xC3 <= m <= 0xCF and m not in (0xC4, 0xC8, 0xCC)) or m == 0xDA:
            return "unsupported JPEG type"
        i += 2 + ln
    return None                                  # header not in the bytes we have


def mp3_art_problem(head):
    """Why the radio can't show the picture in this MP3's ID3v2 tag, or None
    (no tag, no picture, or a usable one).  head: the file's first bytes."""
    if head[:3] != b"ID3" or len(head) < 10:
        return None
    ver, flags = head[3], head[5]
    if flags & 0x80:                             # unsynchronised tag: rare, don't guess
        return None
    syncsafe = lambda b: (b[0] << 21) | (b[1] << 14) | (b[2] << 7) | b[3]
    end = 10 + syncsafe(head[6:10])
    pos = 10
    if flags & 0x40 and ver >= 3:                # extended header
        pos += syncsafe(head[10:14]) if ver == 4 else 4 + int.from_bytes(head[10:14], "big")
    idlen, hdrlen = (3, 6) if ver == 2 else (4, 10)
    while pos + hdrlen <= min(end, len(head)):
        fid = head[pos:pos + idlen]
        if fid[:1] == b"\0":
            break
        if ver == 2:
            fsize = int.from_bytes(head[pos + 3:pos + 6], "big")
        elif ver == 4:
            fsize = syncsafe(head[pos + 4:pos + 8])
        else:
            fsize = int.from_bytes(head[pos + 4:pos + 8], "big")
        body = pos + hdrlen
        if fid in (b"APIC", b"PIC"):
            data = head[body:body + fsize]
            enc = data[0] if data else 0
            if fid == b"PIC":
                i = 5                            # encoding, 3-char format, picture type
            else:
                i = data.find(b"\0", 1) + 2      # encoding, mime\0, picture type
            term = b"\0\0" if enc in (1, 2) else b"\0"
            j = data.find(term, i)
            while enc in (1, 2) and j >= 0 and (j - i) % 2:
                j = data.find(term, j + 1)       # UTF-16: the terminator is on a 2-byte boundary
            if j < 0:
                return None
            return jpeg_problem(data[j + len(term):], fsize - (j + len(term)))
        pos = body + fsize
    return None


def _cover_ok(path):
    try:
        with open(path, "rb") as f:
            head = f.read(256 * 1024)
        return jpeg_problem(head, os.path.getsize(path)) is None
    except OSError:
        return False


def _cover_jpeg(path):
    """A folder picture made into one the radio shows: baseline JPEG <= 1000 px."""
    from PIL import Image
    with Image.open(path) as im:
        im = im.convert("RGB")
        im.thumbnail((1000, 1000), Image.LANCZOS)
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=88, progressive=False, subsampling=2)
        return buf.getvalue()


def music_source(src):
    """(path, folder on the stick) from a path or a {'path','dest'} dict.  The
    default folder is the source folder's own name; '' = straight into music/."""
    if isinstance(src, dict):
        path = os.path.abspath(os.path.expanduser(src["path"]))
        dest = src.get("dest")
    else:
        path, dest = os.path.abspath(os.path.expanduser(src)), None
    if dest is None:
        dest = "" if os.path.isfile(path) else os.path.basename(path.rstrip("/\\"))
    dest = "/".join(fat_name(x) for x in str(dest).replace("\\", "/").split("/") if x.strip(" ."))
    return path, dest


def music_plan(sources, root):
    """What copying these folders/files to root/music would do.
    sources: paths, or {'path', 'dest'} dicts (see music_source).
    -> {'items': [{src, rel, size, action}], 'tracks_new', 'bytes', 'free',
        'left_out': {ext: n}, 'renamed': [(src, rel)], 'skipped': [(src, why)], 'warnings': [...]}
    action: 'copy' (new), 'replace' (different), 'same', 'cover' (picture converted),
    'elsewhere' (the same song is already in another folder on the stick: not copied)."""
    mdir = os.path.join(root, "music")
    items, renamed, skipped, left_out, warnings = [], [], [], {}, []
    taken = {}                                   # lower-case rel -> src (FAT ignores case)
    on_stick = {}                                # (lower-case name, size) -> folder, songs already there
    if os.path.isdir(mdir):
        for d, dirs, files in os.walk(mdir):
            dirs[:] = [x for x in dirs if not x.startswith(".")]
            for f in files:
                if f.lower().endswith(".mp3") and not f.startswith("."):
                    try:
                        size = os.path.getsize(os.path.join(d, f))
                    except OSError:
                        continue
                    on_stick.setdefault((f.lower(), size), os.path.relpath(d, mdir).replace(os.sep, "/"))

    def add(src, parts, kind):
        rel_parts = [fat_name(p) for p in parts]
        if len(rel_parts) - 1 > FW_MUSIC_DEPTH:
            skipped.append((src, f"more than {FW_MUSIC_DEPTH} folders deep"))
            return
        rel = "/".join(rel_parts)
        if fw_bytes(rel) > FW_MUSIC_PATH_MAX:   # shorten the file name, keep the extension
            stem, _, ext = rel_parts[-1].rpartition(".")
            room = FW_MUSIC_PATH_MAX - fw_bytes("/".join(rel_parts[:-1])) - len(ext) - 2
            if room < 8:
                skipped.append((src, "folder names too long for the radio"))
                return
            rel_parts[-1] = _cut_utf8(stem, room).rstrip(" .") + "." + ext
            rel = "/".join(rel_parts)
        base, n = rel, 2
        while rel.lower() in taken and taken[rel.lower()] != src:
            stem, _, ext = base.rpartition(".")
            rel = f"{stem} ({n}).{ext}"
            n += 1
        taken[rel.lower()] = src
        if rel_parts != list(parts) or rel != "/".join(rel_parts):
            renamed.append((src, rel))
        dest = os.path.join(mdir, *rel.split("/"))
        st = os.stat(src)
        action = "copy"
        if kind == "cover" and not _cover_ok(src):
            action = "cover"
        try:
            dt_ = os.stat(dest)
            if action == "cover":
                action = "same" if dt_.st_mtime >= st.st_mtime - 2 else "cover"
            elif dt_.st_size == st.st_size and abs(dt_.st_mtime - st.st_mtime) <= 2:
                action = "same"
            else:
                action = "replace"
        except OSError:
            where = on_stick.get((rel.rsplit("/", 1)[-1].lower(), st.st_size)) if kind == "track" else None
            if where is not None:
                # in the folder it is going to (another case of the name: FAT ignores case) or elsewhere
                here = rel.rsplit("/", 1)[0] if "/" in rel else "."
                action = "same" if where.lower() == here.lower() else "elsewhere"
        item = {"src": src, "rel": rel, "size": st.st_size, "action": action, "kind": kind}
        if action == "elsewhere":
            item["where"] = "" if where == "." else where
        items.append(item)

    def consider(path, parts):
        name = parts[-1]
        low = name.lower()
        stem, _, ext = low.rpartition(".")
        if low.endswith(".mp3"):
            add(path, parts, "track")
        elif stem in MUSIC_COVERS and ext in ("jpg", "jpeg", "png"):
            # keep the radio's file name (cover.jpg ...); a PNG becomes a JPEG
            parts = parts[:-1] + [f"{stem}.jpg"]
            add(path, parts, "cover")
        elif "." + ext in OTHER_AUDIO:
            left_out["." + ext] = left_out.get("." + ext, 0) + 1

    for source in sources:
        src, dest = music_source(source)
        top = dest.split("/") if dest else []
        if os.path.isfile(src):
            consider(src, top + [os.path.basename(src)])
            continue
        if not os.path.isdir(src):
            skipped.append((src, "not found"))
            continue
        for d, dirs, files in os.walk(src):
            dirs[:] = sorted(x for x in dirs if not x.startswith("."))
            sub = os.path.relpath(d, src)
            sub_parts = [] if sub == "." else sub.split(os.sep)
            for f in sorted(files):
                if not f.startswith("."):
                    consider(os.path.join(d, f), top + sub_parts + [f])

    todo = [i for i in items if i["action"] not in ("same", "elsewhere")]
    elsewhere = {}
    for i in items:
        if i["action"] == "elsewhere":
            elsewhere[i["where"]] = elsewhere.get(i["where"], 0) + 1
    replaced = 0
    for i in todo:
        if i["action"] == "replace":
            try:
                replaced += os.path.getsize(os.path.join(mdir, *i["rel"].split("/")))
            except OSError:
                pass
    need = sum(i["size"] for i in todo) - replaced
    try:
        free = shutil.disk_usage(root).free
    except OSError:
        free = 0
    have = music_summary(root)
    tracks_new = sum(1 for i in todo if i["kind"] == "track" and i["action"] == "copy")
    if have["songs"] + tracks_new > FW_MUSIC_TRACKS:
        warnings.append(f"the radio lists at most {FW_MUSIC_TRACKS} songs; "
                        f"{have['songs'] + tracks_new - FW_MUSIC_TRACKS} would not be shown")
    if need > free - 1024 * 1024:
        warnings.append("not enough room on the stick")
    return {"items": items, "tracks_new": tracks_new, "bytes": max(need, 0), "free": free,
            "elsewhere": elsewhere,
            "left_out": left_out, "renamed": renamed, "skipped": skipped, "warnings": warnings,
            "fits": need <= free - 1024 * 1024}


def music_copy(plan, root, progress=None, cancelled=lambda: False):
    """Carries out music_plan().  progress(done_bytes, total_bytes, rel) is called
    as it goes.  -> {'copied': n, 'covers': n, 'art_problems': n, 'cancelled': bool}"""
    if not plan["fits"]:
        raise SystemExit("not enough room on the stick for this music")
    mdir = os.path.join(root, "music")
    todo = [i for i in plan["items"] if i["action"] not in ("same", "elsewhere")]
    total = sum(i["size"] for i in todo)
    done, out = 0, {"copied": 0, "covers": 0, "art_problems": 0, "cancelled": False}
    for it in todo:
        if cancelled():
            out["cancelled"] = True
            break
        dest = os.path.join(mdir, *it["rel"].split("/"))
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        tmp = dest + ".p4tmp"
        if progress:
            progress(done, total, it["rel"])
        try:
            if it["action"] == "cover":
                with open(tmp, "wb") as f:
                    f.write(_cover_jpeg(it["src"]))
                    os.fsync(f.fileno())
                out["covers"] += 1
                done += it["size"]
            else:
                with open(it["src"], "rb") as fi, open(tmp, "wb") as fo:
                    first = True
                    while True:
                        chunk = fi.read(2 * 1024 * 1024 if first else 1024 * 1024)
                        if not chunk:
                            break
                        if first and it["kind"] == "track" and mp3_art_problem(chunk):
                            out["art_problems"] += 1
                        first = False
                        fo.write(chunk)
                        done += len(chunk)
                        if progress:
                            progress(done, total, it["rel"])
                        if cancelled():
                            break
                    fo.flush()
                    os.fsync(fo.fileno())
                if cancelled():
                    os.remove(tmp)
                    out["cancelled"] = True
                    break
            os.replace(tmp, dest)
            if it["action"] != "cover":
                st = os.stat(it["src"])
                os.utime(dest, (st.st_atime, st.st_mtime))   # so the next plan sees "same"
            out["copied"] += 1
        except BaseException:
            if os.path.exists(tmp):
                os.remove(tmp)
            raise
    if hasattr(os, "sync"):
        os.sync()
    if progress:
        progress(done, total, "")
    return out


def music_summary(root):
    """Music on the stick: {'songs', 'bytes', 'folders': [{name, songs, bytes}]} (top level)."""
    mdir = os.path.join(root, "music")
    out = {"songs": 0, "bytes": 0, "folders": []}
    if not os.path.isdir(mdir):
        return out
    loose = {"name": "", "songs": 0, "bytes": 0}
    for e in sorted(os.listdir(mdir), key=str.lower):
        p = os.path.join(mdir, e)
        if e.startswith("."):
            continue
        if os.path.isdir(p):
            n = b = 0
            for d, dirs, files in os.walk(p):
                dirs[:] = [x for x in dirs if not x.startswith(".")]
                for f in files:
                    if f.lower().endswith(".mp3") and not f.startswith("."):
                        n += 1
                    try:
                        b += os.path.getsize(os.path.join(d, f))
                    except OSError:
                        pass
            out["folders"].append({"name": e, "songs": n, "bytes": b})
        elif e.lower().endswith(".mp3"):
            loose["songs"] += 1
            loose["bytes"] += os.path.getsize(p)
    if loose["songs"]:
        out["folders"].insert(0, loose)
    out["songs"] = sum(f["songs"] for f in out["folders"])
    out["bytes"] = sum(f["bytes"] for f in out["folders"])
    return out


def music_remove(root, name):
    """Deletes one top-level folder of music/ (name '' = the loose songs in music/)."""
    mdir = os.path.realpath(os.path.join(root, "music"))
    if name == "":
        for e in os.listdir(mdir):
            if e.lower().endswith(".mp3") and os.path.isfile(os.path.join(mdir, e)):
                os.remove(os.path.join(mdir, e))
        return
    target = os.path.realpath(os.path.join(mdir, name))
    if os.path.dirname(target) != mdir or not os.path.isdir(target):
        raise ValueError("no such music folder on the stick")
    shutil.rmtree(target)
    if hasattr(os, "sync"):
        os.sync()


def _mb(n):
    return f"{n / 1e6:.0f} MB" if n < 1e9 else f"{n / 1e9:.2f} GB"


def cmd_music(a):
    check_stick(a.mount, a.force)
    if a.remove is not None:
        music_remove(a.mount, a.remove)
        print(f"removed music/{a.remove}")
    if not a.sources:
        have = music_summary(a.mount)
        for f in have["folders"]:
            print(f"  {f['name'] or '(loose songs)':40s} {f['songs']:5d} songs  {_mb(f['bytes'])}")
        print(f"{have['songs']} songs, {_mb(have['bytes'])} on the stick")
        return
    sources = [{"path": x, "dest": a.to} if a.to is not None else x for x in a.sources]
    plan = music_plan(sources, a.mount)
    count = {}
    for i in plan["items"]:
        count[i["action"]] = count.get(i["action"], 0) + 1
    print(f"{count.get('copy', 0)} new, {count.get('replace', 0)} changed, {count.get('cover', 0)} pictures "
          f"to convert, {count.get('same', 0)} already on the stick; {_mb(plan['bytes'])} to copy, "
          f"{_mb(plan['free'])} free")
    for where, n in sorted(plan["elsewhere"].items()):
        print(f"  {n} song(s) already on the stick in {'music/' + where if where else 'music/'}: not copied again")
    for ext, n in sorted(plan["left_out"].items()):
        print(f"  left out: {n} {ext} file(s) (the radio plays MP3 only)")
    for src, why in plan["skipped"]:
        print(f"  skipped: {src}: {why}")
    for src, rel in plan["renamed"][:20]:
        print(f"  renamed for the stick: {os.path.basename(src)} -> {rel}")
    for w in plan["warnings"]:
        print(f"  warning: {w}")
    if a.dry_run or not plan["fits"]:
        return
    last = [0.0]

    def prog(done, total, rel):
        if time.monotonic() - last[0] > 1 or not rel:
            last[0] = time.monotonic()
            print(f"\r  {100 * done // max(total, 1):3d}%  {rel[-60:]:60s}", end="", flush=True)
    res = music_copy(plan, a.mount, prog)
    print(f"\ncopied {res['copied']} files ({res['covers']} pictures converted)"
          + (f"; {res['art_problems']} song(s) carry a picture the radio can't show" if res["art_problems"] else ""))


def eject(root):
    """Unmounts and powers off the stick.  -> message for the user."""
    m = _mount_of(root)
    if m and shutil.which("udisksctl"):
        dev = m[0]
        r = subprocess.run(["udisksctl", "unmount", "-b", dev], capture_output=True, text=True)
        if r.returncode != 0:
            return f"could not unmount {dev}: {r.stderr.strip()}"
        subprocess.run(["udisksctl", "power-off", "-b", dev], capture_output=True, text=True)
        return "stick ejected, you can unplug it"
    if sys.platform == "darwin" and shutil.which("diskutil"):
        r = subprocess.run(["diskutil", "eject", root], capture_output=True, text=True)
        return "stick ejected, you can unplug it" if r.returncode == 0 else r.stderr.strip()
    return "eject the stick from your file manager before unplugging it"


def cmd_stick(a):
    rows = load_lib(a.library)
    cfg = load_cfg(a.config)
    check_stick(a.mount, a.force)

    settings = dict(cfg.get("settings", {}))
    if a.timezone:
        try:
            settings["timezone"] = tz_posix(a.timezone)
        except ValueError as e:
            raise SystemExit(str(e))
    if a.eq:
        settings["eq"] = a.eq
    if a.brightness is not None:
        settings["brightness"] = a.brightness
    try:
        config_text(settings)
    except ValueError as e:
        raise SystemExit(str(e))
    if settings != cfg.get("settings", {}) and not a.dry_run:
        cfg["settings"] = settings
        save_cfg(a.config, cfg)

    wifi = None
    if a.wifi:
        pw = a.password
        if pw is None:
            import getpass
            pw = getpass.getpass(f"password for {a.wifi!r} (empty for an open network): ")
        wifi = (a.wifi, pw)
        try:
            wifi_text(*wifi)
        except ValueError as e:
            raise SystemExit(str(e))

    out_rows = pick_rows(rows, a.include_untested, a.include_failed)
    if not out_rows:
        raise SystemExit("no stations to write (run `test` first, or --include-untested)")
    logos = None if a.no_logos else write_logos(out_rows, a.size, a.bg)

    res = write_stick(a.mount, out_rows, settings=settings, wifi=wifi, wifi_remove=a.no_wifi,
                      logos=logos, project=(a.library, a.config), dry_run=a.dry_run)
    what = "would write" if a.dry_run else "wrote"
    print(f"{what} {res['stations']} stations" +
          (f", {res['logos']} logos" if res["logos"] is not None else "") +
          f" -> {os.path.join(a.mount, 'radio')}")
    for c in res["changed"]:
        print(f"  {c}")
    if not res["changed"]:
        print("  nothing changed: the stick already matches")
    for n in res["notes"]:
        print(f"  note: {n}")
    if not a.dry_run:
        print("Plug the stick into the radio: it imports the files and restarts once.\n"
              "Afterwards `p4radio.py report <stick>` shows what the radio stored.")
        if a.eject:
            print(eject(a.mount))


def cmd_report(a):
    found = False
    for name in ("report.txt", "update.txt"):
        p = os.path.join(a.mount, "radio", name)
        if os.path.exists(p):
            found = True
            when = dt.datetime.fromtimestamp(os.path.getmtime(p))
            print(f"== radio/{name}  ({when:%Y-%m-%d %H:%M}) ==")
            with open(p, encoding="utf-8", errors="replace") as f:
                print(f.read().rstrip())
            print()
    if not found:
        print("no report on this stick yet: the radio writes radio/report.txt when it imports the stick")


# --------------------------------------------------------------------------

def main():
    if sys.argv[1:2] == ["gui"]:                # the configurator page (p4gui.py)
        import p4gui
        return p4gui.main(sys.argv[2:])
    p = argparse.ArgumentParser(prog="p4radio", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("-L", "--library", default=LIB_DEFAULT, help="master library CSV (default library.csv)")
    p.add_argument("-C", "--config", default=CFG_DEFAULT)
    sp = p.add_subparsers(dest="cmd", required=True)

    s = sp.add_parser("import", help="import a stations.txt (any 'name<sep>url' layout), or a whole stick")
    s.add_argument("file")
    s.add_argument("--keep-format", action="store_true", help="don't overwrite the saved line layout")
    s.set_defaults(fn=cmd_import)

    s = sp.add_parser("search", help="search radio-browser.info")
    s.add_argument("--name"); s.add_argument("--tag"); s.add_argument("--country", help="ISO code, e.g. GB")
    s.add_argument("--codec", help="MP3 / AAC / AAC+"); s.add_argument("--language")
    s.add_argument("--min-kbps", type=int)
    s.add_argument("--limit", type=int, default=25, help="max results (default 25, 0 = all matches)")
    s.add_argument("--add", help="indices to add without prompting (e.g. 1,3 or all)")
    s.set_defaults(fn=cmd_search)

    s = sp.add_parser("add", help="probe a URL and add it")
    s.add_argument("url"); s.add_argument("--name"); s.add_argument("--genre"); s.add_argument("--logo")
    s.add_argument("--seconds", type=float, default=8); s.add_argument("--force", action="store_true")
    s.set_defaults(fn=cmd_add)

    s = sp.add_parser("probe", help="probe one URL without adding it")
    s.add_argument("url"); s.add_argument("--seconds", type=float, default=8)
    s.set_defaults(fn=cmd_probe)

    s = sp.add_parser("test", help="probe library stations and write station_log.md")
    s.add_argument("which", nargs="?", help="all (default), 3, 1,4-6, or a name fragment")
    s.add_argument("--seconds", type=float, default=8); s.add_argument("-j", "--jobs", type=int, default=6)
    s.add_argument("--untested", action="store_true", help="only rows never tested")
    s.add_argument("--log", default="station_log.md")
    s.set_defaults(fn=cmd_test)

    s = sp.add_parser("list", help="show the library"); s.set_defaults(fn=cmd_list)

    s = sp.add_parser("set", help="edit fields of selected rows")
    s.add_argument("which"); s.add_argument("--keep", choices=["y", "n"])
    s.add_argument("--name"); s.add_argument("--genre"); s.add_argument("--logo"); s.add_argument("--url")
    s.set_defaults(fn=cmd_set)

    s = sp.add_parser("move", help="reorder: move row SRC to position DST")
    s.add_argument("src", type=int); s.add_argument("dst", type=int); s.set_defaults(fn=cmd_move)

    s = sp.add_parser("rm", help="remove rows"); s.add_argument("which"); s.set_defaults(fn=cmd_rm)

    s = sp.add_parser("listen", help="audition stations in mpv and mark keep/drop")
    s.add_argument("which", nargs="?"); s.add_argument("--undecided", action="store_true")
    s.set_defaults(fn=cmd_listen)

    s = sp.add_parser("build", help="write stations.txt (+ N.jpg logos) into a folder")
    s.add_argument("--out", default="card")
    s.add_argument("--line", help="line template, e.g. '{name}|{url}' (default from config)")
    s.add_argument("--logos", action="store_true", help="also make N.jpg logos (needs Pillow)")
    s.add_argument("--size", type=int, default=304, help="logo size, multiple of 16 (default 304)")
    s.add_argument("--bg", default="#000000", help="logo background colour")
    s.add_argument("--resolved", action="store_true", help="write final (post-redirect) URLs")
    s.add_argument("--header", action="store_true", help="write a # comment header line")
    s.add_argument("--include-untested", action="store_true")
    s.add_argument("--include-failed", action="store_true")
    s.set_defaults(fn=cmd_build)

    s = sp.add_parser("stick", help="write the radio/ folder on the radio's USB stick")
    s.add_argument("mount", help="where the stick is mounted, e.g. /media/me/STICK")
    s.add_argument("--wifi", metavar="SSID", help="write wifi.txt for this network (asks for the password)")
    s.add_argument("--password", help="WiFi password (instead of being asked; empty = open network)")
    s.add_argument("--no-wifi", action="store_true", help="remove wifi.txt from the stick")
    s.add_argument("--timezone", help="Europe/London, local, or a POSIX TZ string (remembered)")
    s.add_argument("--eq", choices=EQ_PRESETS, help="EQ preset (remembered)")
    s.add_argument("--brightness", type=int, help="screen brightness 20-100 (remembered)")
    s.add_argument("--no-logos", action="store_true", help="leave the logos on the stick alone")
    s.add_argument("--size", type=int, default=304, help="logo size, multiple of 16 (default 304)")
    s.add_argument("--bg", default="#000000", help="logo background colour")
    s.add_argument("--include-untested", action="store_true")
    s.add_argument("--include-failed", action="store_true")
    s.add_argument("--dry-run", action="store_true", help="show what would change, write nothing")
    s.add_argument("--force", action="store_true", help="allow a plain folder instead of a stick")
    s.add_argument("--eject", action="store_true", help="eject the stick when done")
    s.set_defaults(fn=cmd_stick)

    s = sp.add_parser("music", help="copy MP3 folders to music/ on the stick, or list/remove them")
    s.add_argument("mount")
    s.add_argument("sources", nargs="*", help="folders or MP3 files on this computer")
    s.add_argument("--to", metavar="FOLDER", help="folder in music/ to copy into (default: each source's name; '' = music/ itself)")
    s.add_argument("--remove", metavar="FOLDER", help="delete this top-level folder of music/ first")
    s.add_argument("--dry-run", action="store_true")
    s.add_argument("--force", action="store_true", help="allow a plain folder instead of a stick")
    s.set_defaults(fn=cmd_music)

    s = sp.add_parser("report", help="show the radio's report.txt / update.txt from a stick")
    s.add_argument("mount")
    s.set_defaults(fn=cmd_report)

    a = p.parse_args()
    if getattr(a, "size", 16) % 16:
        raise SystemExit("--size must be a multiple of 16")
    a.fn(a)


if __name__ == "__main__":
    main()

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
    p4radio.py build   --out card/              # stations.txt + logos/N.jpeg

Standard library only.  Pillow is needed only for `build --logos`
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


def _connect(url, timeout):
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
           f"Accept: */*\r\nIcy-MetaData: 1\r\nConnection: close\r\n\r\n")
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
        for hop in range(8):
            sock, code, hdrs, rest, cms, tls = _connect(cur, timeout)
            res["hops"].append(f"{code} {cur}")
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
                res["notes"].append(f"via {kind} playlist")
                cur = urllib.parse.urljoin(cur, nxt)
                continue
            break
        else:
            raise ProbeError("too many redirects / playlist hops")

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
        while True:
            now = time.monotonic()
            if now - t_start >= seconds:
                break
            if not pending:
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


def apply_probe(row, res):
    for k in ("status", "codec", "kbps", "rate", "ch", "tls", "final_url",
              "icy_name", "connect_ms", "speed"):
        row[k] = str(res.get(k, ""))
    row["tested"] = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    auto = "; ".join(res["notes"])
    row["notes"] = auto
    if not row.get("genre") and res.get("icy_genre"):
        row["genre"] = res["icy_genre"][:40]
    if not row.get("name") and res.get("icy_name"):
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


def rb_search(params):
    q = urllib.parse.urlencode({k: v for k, v in params.items() if v not in (None, "")})
    last = None
    for host in rb_servers():
        try:
            req = urllib.request.Request(f"https://{host}/json/stations/search?{q}",
                                         headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.load(r)
        except Exception as e:
            last = e
    raise SystemExit(f"radio-browser.info not reachable: {last}")


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


def make_logo(src, dst, size, bg):
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
    canvas.save(dst, "JPEG", quality=90, optimize=False, progressive=False, subsampling=2)


def cmd_build(a):
    rows = load_lib(a.library)
    cfg = load_cfg(a.config)
    line = a.line or cfg["line"]
    allow = {"OK", "WARN", ""} if a.include_untested else {"OK", "WARN"}
    if a.include_failed:
        allow |= {"FAIL"}
    out_rows = [r for r in rows if r["keep"].lower().startswith("y") and r["status"] in allow]
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
        ldir = os.path.join(a.out, "logos")
        os.makedirs(ldir, exist_ok=True)
        for f in os.listdir(ldir):
            if re.fullmatch(r"\d+\.jpe?g", f):
                os.remove(os.path.join(ldir, f))
        bg = tuple(int(a.bg.lstrip("#")[k:k + 2], 16) for k in (0, 2, 4))
        ok = 0
        for n, r in enumerate(out_rows, 1):
            if not r["logo"]:
                print(f"  {n:2d}. {r['name'][:30]:30s} no logo (radio shows initials)")
                continue
            try:
                make_logo(r["logo"], os.path.join(ldir, f"{n}.jpeg"), a.size, bg)
                ok += 1
            except SystemExit:
                raise
            except Exception as e:
                print(f"  {n:2d}. {r['name'][:30]:30s} logo failed: {e}")
        print(f"wrote {ok} logos ({a.size}x{a.size} baseline JPEG) -> {ldir}/")


# --------------------------------------------------------------------------

def main():
    p = argparse.ArgumentParser(prog="p4radio", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("-L", "--library", default=LIB_DEFAULT, help="master library CSV (default library.csv)")
    p.add_argument("-C", "--config", default=CFG_DEFAULT)
    sp = p.add_subparsers(dest="cmd", required=True)

    s = sp.add_parser("import", help="import an existing stations.txt (any 'name<sep>url' layout)")
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

    s = sp.add_parser("build", help="write stations.txt (+ logos/N.jpeg) for the radio")
    s.add_argument("--out", default="card")
    s.add_argument("--line", help="line template, e.g. '{name}|{url}' (default from config)")
    s.add_argument("--logos", action="store_true", help="also make logos/N.jpeg (needs Pillow)")
    s.add_argument("--size", type=int, default=304, help="logo size, multiple of 16 (default 304)")
    s.add_argument("--bg", default="#000000", help="logo background colour")
    s.add_argument("--resolved", action="store_true", help="write final (post-redirect) URLs")
    s.add_argument("--header", action="store_true", help="write a # comment header line")
    s.add_argument("--include-untested", action="store_true")
    s.add_argument("--include-failed", action="store_true")
    s.set_defaults(fn=cmd_build)

    a = p.parse_args()
    if getattr(a, "size", 16) % 16:
        raise SystemExit("--size must be a multiple of 16")
    a.fn(a)


if __name__ == "__main__":
    main()

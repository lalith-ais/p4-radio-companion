#!/usr/bin/env python3
"""Fake Icecast/Shoutcast server for testing p4radio.py."""
import socketserver, sys, threading, time, http.server

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8765


def mp3_frame(kbps=128, sr=44100, mono=False):
    bri = [0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320].index(kbps)
    sri = [44100, 48000, 32000].index(sr)
    hdr = bytes([0xFF, 0xFB, (bri << 4) | (sri << 2), 0xC4 if mono else 0x44])
    flen = 144 * kbps * 1000 // sr
    return hdr + b"\x00" * (flen - 4)


def adts_frame(sr_idx=6, ch=2, flen=200):     # 24 kHz core = HE-AAC
    b = bytearray(flen)
    b[0] = 0xFF; b[1] = 0xF1
    b[2] = (1 << 6) | (sr_idx << 2) | (ch >> 2)
    b[3] = ((ch & 3) << 6) | ((flen >> 11) & 3)
    b[4] = (flen >> 3) & 0xFF
    b[5] = ((flen & 7) << 5) | 0x1F
    b[6] = 0xFC
    return bytes(b)


class H(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"
    def log_message(self, *a): pass

    def stream(self, frame, kbps, metaint=0, rate_scale=1.0, title=b"Artist - Song"):
        burst = 64 * 1024
        sent, t0, cnt = 0, time.monotonic(), 0
        bps = kbps * 1000 / 8 * rate_scale
        try:
            while time.monotonic() - t0 < 30:
                data = frame * 4
                for byte_chunk in [data]:
                    out = bytearray()
                    for b in byte_chunk:
                        pass
                    if metaint:
                        for i in range(len(data)):
                            out.append(data[i]); cnt += 1
                            if cnt == metaint:
                                meta = b"StreamTitle='" + title + b"';"
                                n = (len(meta) + 15) // 16
                                out.append(n); out += meta.ljust(n * 16, b"\0"); cnt = 0
                    else:
                        out = bytearray(data)
                    self.wfile.write(out)
                sent += len(data)
                if sent > burst:
                    due = t0 + (sent - burst) / bps
                    d = due - time.monotonic()
                    if d > 0: time.sleep(d)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_GET(self):
        p = self.path
        if p == "/mp3":
            self.send_response(200)
            self.send_header("Content-Type", "audio/mpeg"); self.send_header("icy-name", "Fake MP3 FM")
            self.send_header("icy-genre", "Rock"); self.send_header("icy-br", "128")
            self.send_header("icy-metaint", "8192"); self.end_headers()
            self.stream(mp3_frame(), 128, metaint=8192)
        elif p == "/aacp":
            self.send_response(200)
            self.send_header("Content-Type", "audio/aacp"); self.send_header("icy-br", "64"); self.end_headers()
            f = adts_frame(6, 2, 200); self.stream(f, 64)
        elif p == "/aacv2":
            self.send_response(200)
            self.send_header("Content-Type", "audio/aacp"); self.send_header("icy-br", "48"); self.end_headers()
            self.stream(adts_frame(6, 1, 150), 48)
        elif p == "/aaclc":
            self.send_response(200)
            self.send_header("Content-Type", "audio/aac"); self.send_header("icy-br", "320"); self.end_headers()
            self.stream(adts_frame(4, 2, 900), 320)
        elif p == "/icy":
            self.wfile.write(b"ICY 200 OK\r\nicy-name:Old Shoutcast\r\nicy-br:128\r\ncontent-type:audio/mpeg\r\n\r\n")
            self.stream(mp3_frame(), 128)
        elif p == "/slow":
            self.send_response(200); self.send_header("Content-Type", "audio/mpeg")
            self.send_header("icy-br", "128"); self.end_headers()
            self.stream(mp3_frame(), 128, rate_scale=0.7)
        elif p == "/listen.pls":
            body = f"[playlist]\nNumberOfEntries=1\nFile1=http://127.0.0.1:{PORT}/mp3\n".encode()
            self.send_response(200); self.send_header("Content-Type", "audio/x-scpls"); self.end_headers()
            self.wfile.write(body)
        elif p == "/redir":
            self.send_response(302); self.send_header("Location", "/aacp"); self.end_headers()
        elif p == "/live.m3u8":
            self.send_response(200); self.send_header("Content-Type", "application/vnd.apple.mpegurl"); self.end_headers()
            self.wfile.write(b"#EXTM3U\n#EXT-X-VERSION:3\n#EXT-X-TARGETDURATION:6\nseg1.ts\n")
        elif p == "/ogg":
            self.send_response(200); self.send_header("Content-Type", "audio/ogg"); self.end_headers()
            self.wfile.write(b"OggS" + b"\0" * 24 + b"OpusHead" + b"\0" * 2000)
        else:
            self.send_response(404); self.end_headers()


class S(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


if __name__ == "__main__":
    S(("127.0.0.1", PORT), H).serve_forever()

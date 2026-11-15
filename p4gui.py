#!/usr/bin/env python3
"""
P4 Radio configurator: a page in your web browser for setting up the radio's
USB stick.  Choose the stick, arrange the stations and their logos, write.

    python3 p4gui.py                 # opens the browser
    python3 p4gui.py --project DIR   # where the station list is kept (default ~/P4Radio)
    python3 p4gui.py --no-browser --port 8642

The page talks only to this program, on 127.0.0.1.  Everything about stations,
logos and the stick's files is done by p4radio.py.
"""

import argparse
import hashlib
import http.server
import json
import os
import plistlib
import secrets
import shutil
import subprocess
import sys
import threading
import urllib.parse
import urllib.request
import webbrowser

import p4radio as P

HERE = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
PAGE = os.path.join(HERE, "gui", "index.html")
LOGO_SIZE = 304
UPLOAD_MAX = 8 * 1024 * 1024


# --------------------------------------------------------------------------
# finding sticks
# --------------------------------------------------------------------------

def _human(n):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1000 or unit == "TB":
            return f"{n:.0f} {unit}" if unit in ("B", "KB") else f"{n:.1f} {unit}"
        n /= 1000


def _flag(v):
    return v in (True, 1, "1", "true")


def _volumes_linux():
    try:
        out = subprocess.run(["lsblk", "-J", "-b", "-o",
                              "NAME,PATH,SIZE,FSTYPE,LABEL,RM,HOTPLUG,MOUNTPOINT,TYPE"],
                             capture_output=True, text=True, timeout=5).stdout
        tree = json.loads(out)["blockdevices"]
    except (OSError, ValueError, KeyError, subprocess.SubprocessError):
        return []
    vols = []
    for disk in tree:
        if disk.get("type") != "disk" or not (_flag(disk.get("rm")) or _flag(disk.get("hotplug"))):
            continue
        parts = disk.get("children") or [disk]
        for p in parts:
            if not p.get("fstype") and p is not disk:
                continue
            vols.append({"dev": p.get("path") or "/dev/" + p["name"], "size": int(p.get("size") or 0),
                         "fs": p.get("fstype") or "", "label": p.get("label") or "",
                         "mount": p.get("mountpoint") or ""})
    return vols


def _volumes_mac():                                   # not yet tried on a Mac
    vols = []
    for name in sorted(os.listdir("/Volumes")):
        mnt = os.path.join("/Volumes", name)
        if os.path.realpath(mnt) == "/":
            continue
        try:
            info = plistlib.loads(subprocess.run(["diskutil", "info", "-plist", mnt],
                                                 capture_output=True, timeout=5).stdout)
        except Exception:
            continue
        if not (info.get("Removable") or info.get("RemovableMedia") or info.get("Ejectable")):
            continue
        fs = info.get("FilesystemType", "")
        vols.append({"dev": info.get("DeviceNode", ""), "size": int(info.get("TotalSize", 0)),
                     "fs": "vfat" if fs == "msdos" else fs, "label": name, "mount": mnt})
    return vols


def _volumes_windows():                               # not yet tried on Windows
    import ctypes
    k32 = ctypes.windll.kernel32
    vols = []
    mask = k32.GetLogicalDrives()
    for i in range(26):
        if not mask & (1 << i):
            continue
        root = f"{chr(65 + i)}:\\"
        if k32.GetDriveTypeW(root) != 2:              # DRIVE_REMOVABLE
            continue
        label, fs = ctypes.create_unicode_buffer(261), ctypes.create_unicode_buffer(261)
        ok = k32.GetVolumeInformationW(root, label, 261, None, None, None, fs, 261)
        size = shutil.disk_usage(root).total if ok else 0
        vols.append({"dev": root, "size": size, "fs": "vfat" if fs.value == "FAT32" else fs.value.lower(),
                     "label": label.value, "mount": root if ok else ""})
    return vols


def list_sticks():
    if sys.platform.startswith("linux"):
        vols = _volumes_linux()
    elif sys.platform == "darwin":
        vols = _volumes_mac()
    elif sys.platform == "win32":
        vols = _volumes_windows()
    else:
        vols = []
    out = []
    for v in vols:
        s = dict(v, name=v["label"] or os.path.basename(v["mount"].rstrip("/\\")) or v["dev"],
                 size_text=_human(v["size"]), usable=False, problem="", radio=None)
        if v["fs"] not in ("vfat", "msdos"):
            s["problem"] = (f"This stick is formatted as {v['fs'] or 'unknown'}. The radio needs FAT32. "
                            "Copy anything you need off it, then reformat it as FAT32.")
        elif not v["mount"]:
            s["problem"] = "not_mounted"
        else:
            s["usable"] = True
            try:
                s["free_text"] = _human(shutil.disk_usage(v["mount"]).free)
            except OSError:
                s["free_text"] = "?"
            info = P.read_stick(v["mount"])
            s["radio"] = {"stations": len(info["stations"]), "logos": len(info["logos"]),
                          "settings": info["settings"], "wifi_ssid": info["wifi_ssid"],
                          "report": info["report"], "project": info["project"]}
        out.append(s)
    return out


def mount_stick(dev):
    if not shutil.which("udisksctl"):
        raise ValueError("open the stick in your file manager to mount it")
    r = subprocess.run(["udisksctl", "mount", "-b", dev], capture_output=True, text=True, timeout=20)
    if r.returncode != 0:
        raise ValueError(r.stderr.strip() or "could not mount the stick")


# --------------------------------------------------------------------------
# the project: station list, settings, logos on this computer
# --------------------------------------------------------------------------

class Project:
    def __init__(self, folder):
        self.dir = os.path.abspath(os.path.expanduser(folder))
        os.makedirs(self.dir, exist_ok=True)
        self.lib = os.path.join(self.dir, "library.csv")
        self.cfg_path = os.path.join(self.dir, "p4radio.json")
        self.logo_dir = os.path.join(self.dir, "logos")
        self.lock = threading.RLock()
        self.rows = P.load_lib(self.lib)
        self.cfg = P.load_cfg(self.cfg_path)
        self.next_id = 1
        for r in self.rows:
            self._give_id(r)
        self._logo_cache = {}

    def _give_id(self, r):
        r["id"] = self.next_id
        self.next_id += 1

    def save(self):
        P.save_lib(self.lib, self.rows)
        P.save_cfg(self.cfg_path, self.cfg)

    def row(self, rid):
        for r in self.rows:
            if r["id"] == rid:
                return r
        raise ValueError("that station is no longer in the list")

    def on_radio(self):
        """The rows written to the stick, in order."""
        return [r for r in self.rows if r["keep"].lower().startswith("y")]

    def view(self):
        n = 0
        rows = []
        for r in self.rows:
            on = r["keep"].lower().startswith("y")
            n += on
            rows.append({"id": r["id"], "name": r["name"], "url": r["url"], "on": on,
                         "number": n if on else None, "status": r["status"], "codec": r["codec"],
                         "kbps": r["kbps"], "icy_name": r["icy_name"], "notes": r["notes"],
                         "logo": bool(r["logo"]),
                         "logo_v": hashlib.sha1(r["logo"].encode()).hexdigest()[:8] if r["logo"] else ""})
        return {"project": self.dir, "rows": rows, "settings": self.cfg.get("settings", {}),
                "limits": {"name": P.FW_NAME_MAX, "name_warn": P.NAME_WARN,
                           "stations": P.FW_STATIONS_MAX}}

    # -- edits ----------------------------------------------------------

    def set_rows(self, items):
        """items: the whole list in its new order, [{id, name, on}] (id None = keep out)."""
        with self.lock:
            new = []
            for it in items:
                r = self.row(int(it["id"]))
                if "name" in it:
                    name, _ = P.fw_name(str(it["name"]))
                    r["name"] = name
                if "on" in it:
                    r["keep"] = "y" if it["on"] else "n"
                new.append(r)
            self.rows = new
            self.save()

    def add(self, name, url):
        with self.lock:
            url = P.fw_url(url)
            if P.find_url(self.rows, url):
                raise ValueError("that station is already in the list")
            r = P.new_row(P.fw_name(name)[0], url)
            self._give_id(r)
            self.rows.append(r)
            self.save()
            return r["id"]

    def update(self, rid, name=None, url=None):
        with self.lock:
            r = self.row(rid)
            if name is not None:
                r["name"] = P.fw_name(name)[0]
            if url is not None and url.strip() != r["url"]:
                url = P.fw_url(url)
                if P.find_url(self.rows, url):
                    raise ValueError("another station already has that address")
                r["url"] = url
                for k in ("status", "codec", "kbps", "rate", "ch", "tls", "final_url",
                          "icy_name", "connect_ms", "speed", "tested"):
                    r[k] = ""
            self.save()

    def remove(self, rid):
        with self.lock:
            self.rows = [r for r in self.rows if r["id"] != rid]
            self.save()

    def set_logo(self, rid, data):
        if len(data) > UPLOAD_MAX:
            raise ValueError("that picture is too big (over 8 MB)")
        path = P.store_logo(data, self.logo_dir)
        try:
            self.render(path)
        except Exception as e:
            raise ValueError(f"that file is not a picture this program can use ({e})")
        with self.lock:
            self.row(rid)["logo"] = path
            self.save()

    def clear_logo(self, rid):
        with self.lock:
            self.row(rid)["logo"] = ""
            self.save()

    def load_stick(self, mount):
        with self.lock:
            self.rows, info, added = P.import_stick(mount, [], self.logo_dir, replace=True)
            for r in self.rows:
                self._give_id(r)
            if info["settings"]:
                self.cfg["settings"] = info["settings"]
            self.save()
            return added

    # -- logos ----------------------------------------------------------

    def bg(self):
        c = self.cfg.get("bg", "#000000")
        return tuple(int(c.lstrip("#")[k:k + 2], 16) for k in (0, 2, 4))

    def render(self, src, size=LOGO_SIZE, bg=None):
        """render_logo with a cache (keyed on the file's time and size)."""
        try:
            st = os.stat(src)
            key = (src, st.st_mtime_ns, st.st_size, size, bg or self.bg())
        except OSError:
            key = (src, 0, 0, size, bg or self.bg())
        hit = self._logo_cache.get(key)
        if hit is None:
            hit = P.render_logo(src, size, bg or self.bg())
            self._logo_cache[key] = hit
        return hit


def fetch_picture(url):
    if not url.lower().startswith(("http://", "https://")):
        raise ValueError("the picture's address must start with http:// or https://")
    req = urllib.request.Request(url, headers={"User-Agent": P.UA})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            data = r.read(UPLOAD_MAX + 1)
    except Exception as e:
        raise ValueError(f"could not download the picture ({e})")
    return data


# --------------------------------------------------------------------------
# writing the stick (in the background, the page polls)
# --------------------------------------------------------------------------

class Job:
    def __init__(self):
        self.lock = threading.Lock()
        self.state = "idle"
        self.log, self.result, self.error, self.kind = [], None, "", ""

    def snapshot(self):
        with self.lock:
            return {"state": self.state, "log": list(self.log), "result": self.result,
                    "error": self.error, "kind": self.kind}

    def say(self, line):
        with self.lock:
            self.log.append(line)

    def start(self, kind, fn):
        with self.lock:
            if self.state == "running":
                raise ValueError("the stick is already being written")
            self.state, self.log, self.result, self.error, self.kind = "running", [], None, "", kind

        def run():
            try:
                res = fn(self.say)
                with self.lock:
                    self.result, self.state = res, "done"
            except SystemExit as e:
                with self.lock:
                    self.error, self.state = str(e), "error"
            except Exception as e:
                with self.lock:
                    self.error, self.state = f"{type(e).__name__}: {e}", "error"
        threading.Thread(target=run, daemon=True).start()


def write_job(project, mount, dry_run, allow_folder=False):
    def fn(say):
        if not allow_folder:
            ok = [s for s in list_sticks() if s["usable"] and os.path.realpath(s["mount"]) ==
                  os.path.realpath(mount)]
            if not ok:
                raise SystemExit("that stick is no longer there; plug it in again and choose it")
        P.check_stick(mount, force=allow_folder)
        with project.lock:
            rows = [dict(r) for r in project.on_radio()]
            settings = dict(project.cfg.get("settings", {}))
        if not rows:
            raise SystemExit("no stations are switched on for the radio")
        say(f"preparing {sum(1 for r in rows if r['logo'])} logos")
        logos = P.write_logos(rows, LOGO_SIZE, "#000000", log=say,
                              render=lambda src, size, bg: project.render(src, size))
        say("writing" if not dry_run else "comparing with the stick")
        res = P.write_stick(mount, rows, settings=settings, logos=logos,
                            project=() if dry_run else (project.lib, project.cfg_path),
                            dry_run=dry_run, log=say)
        return res
    return fn


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------

class App:
    def __init__(self, project, allow_folder=False):
        self.project = project
        self.token = secrets.token_urlsafe(16)
        self.job = Job()
        self.allow_folder = allow_folder         # tests: a plain folder may stand in for a stick
        self.port = None


class Handler(http.server.BaseHTTPRequestHandler):
    app: App = None
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    # -- plumbing -------------------------------------------------------

    def _send(self, code, body, ctype="application/json", extra=()):
        if not isinstance(body, bytes):
            body = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in extra:
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _host_ok(self):
        host = self.headers.get("Host", "")
        return host in (f"127.0.0.1:{self.app.port}", f"localhost:{self.app.port}")

    def _token_ok(self):
        return secrets.compare_digest(self.headers.get("X-Token", ""), self.app.token)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n > UPLOAD_MAX + 1024:
            raise ValueError("too much data")
        return self.rfile.read(n) if n else b""

    def _json(self):
        b = self._body()
        return json.loads(b) if b else {}

    def _route(self, method):
        if not self._host_ok():
            return self._send(403, {"error": "forbidden"})
        u = urllib.parse.urlsplit(self.path)
        q = dict(urllib.parse.parse_qsl(u.query))
        path = u.path
        try:
            if method == "GET" and path == "/":
                with open(PAGE, "rb") as f:
                    page = f.read().replace(b"__TOKEN__", self.app.token.encode())
                return self._send(200, page, "text/html; charset=utf-8")
            if method == "GET" and path.startswith("/logo/"):
                return self._logo(int(path.rsplit("/", 1)[1]))
            if not path.startswith("/api/") or not self._token_ok():
                return self._send(404 if not path.startswith("/api/") else 403, {"error": "not found"})
            fn = getattr(self, f"api_{method.lower()}_{path[5:].replace('/', '_')}", None)
            if fn is None:
                return self._send(404, {"error": "not found"})
            return self._send(200, fn(q))
        except ValueError as e:
            return self._send(400, {"error": str(e)})
        except SystemExit as e:
            return self._send(400, {"error": str(e)})
        except Exception as e:
            return self._send(500, {"error": f"{type(e).__name__}: {e}"})

    def do_GET(self):
        self._route("GET")

    def do_POST(self):
        self._route("POST")

    def _logo(self, rid):
        pr = self.app.project
        try:
            with pr.lock:
                src = pr.row(rid)["logo"]
            data = pr.render(src) if src else None
        except Exception:
            data = None
        if data is None:
            return self._send(404, b"", "image/jpeg")
        self._send(200, data, "image/jpeg", [("Cache-Control", "max-age=3600")])

    # -- API ------------------------------------------------------------

    def api_get_state(self, q):
        return self.app.project.view()

    def api_get_sticks(self, q):
        return {"sticks": list_sticks()}

    def api_post_sticks_mount(self, q):
        mount_stick(self._json()["dev"])
        return {"sticks": list_sticks()}

    def _check_mount(self, mount):
        if self.app.allow_folder:
            return mount
        for s in list_sticks():
            if s["usable"] and s["mount"] == mount:
                return mount
        raise ValueError("that stick is no longer there")

    def api_post_stick_load(self, q):
        mount = self._check_mount(self._json()["mount"])
        added = self.app.project.load_stick(mount)
        return dict(self.app.project.view(), loaded=added)

    def api_post_rows(self, q):
        self.app.project.set_rows(self._json()["rows"])
        return self.app.project.view()

    def api_post_rows_add(self, q):
        b = self._json()
        rid = self.app.project.add(b.get("name", ""), b.get("url", ""))
        return dict(self.app.project.view(), added=rid)

    def api_post_rows_update(self, q):
        b = self._json()
        self.app.project.update(int(b["id"]), b.get("name"), b.get("url"))
        return self.app.project.view()

    def api_post_rows_remove(self, q):
        self.app.project.remove(int(self._json()["id"]))
        return self.app.project.view()

    def api_post_logo_upload(self, q):
        self.app.project.set_logo(int(q["id"]), self._body())
        return self.app.project.view()

    def api_post_logo_url(self, q):
        b = self._json()
        self.app.project.set_logo(int(b["id"]), fetch_picture(b.get("url", "").strip()))
        return self.app.project.view()

    def api_post_logo_clear(self, q):
        self.app.project.clear_logo(int(self._json()["id"]))
        return self.app.project.view()

    def api_post_write(self, q):
        b = self._json()
        mount = self._check_mount(b["mount"])
        dry = bool(b.get("dry_run"))
        self.app.job.start("check" if dry else "write",
                           write_job(self.app.project, mount, dry, self.app.allow_folder))
        return self.app.job.snapshot()

    def api_get_job(self, q):
        return self.app.job.snapshot()

    def api_post_eject(self, q):
        mount = self._check_mount(self._json()["mount"])
        return {"message": P.eject(mount)}


def serve(project_dir, port=0, open_browser=True, allow_folder=False, ready=None):
    app = App(Project(project_dir), allow_folder)
    handler = type("H", (Handler,), {"app": app})
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", port), handler)
    app.port = httpd.server_address[1]
    url = f"http://127.0.0.1:{app.port}/"
    if ready:
        ready(app, httpd)
    print(f"P4 Radio configurator: {url}  (project {app.project.dir})", flush=True)
    print("Keep this window open while you use the page; press Ctrl+C here to stop.", flush=True)
    if open_browser:
        threading.Timer(0.5, webbrowser.open, (url,)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print()
    finally:
        httpd.server_close()


def main(argv=None):
    p = argparse.ArgumentParser(prog="p4gui", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--project", default=os.path.join("~", "P4Radio"),
                   help="folder for the station list and logos (default ~/P4Radio)")
    p.add_argument("--port", type=int, default=0, help="port on 127.0.0.1 (default: any free one)")
    p.add_argument("--no-browser", action="store_true", help="don't open the browser")
    a = p.parse_args(argv)
    try:
        import PIL  # noqa: F401
    except ImportError:
        raise SystemExit("Pillow is needed for the logos:  pip install pillow")
    serve(a.project, a.port, not a.no_browser)


if __name__ == "__main__":
    main()

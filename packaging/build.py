#!/usr/bin/env python3
"""
Builds "P4 Radio Setup" as a program for the system this runs on, with
PyInstaller.  PyInstaller cannot build for another system, so the Windows .exe
is built on Windows and the macOS app on a Mac (the GitHub workflow in
.github/workflows/build.yml does all three).

    pip install -r packaging/requirements.txt
    python packaging/build.py

Result in dist/:  Windows "P4 Radio Setup.exe", macOS "P4 Radio Setup.app"
(zipped as P4-Radio-Setup-macOS.zip), Linux "p4-radio-setup".
"""
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NAME = "P4 Radio Setup"
BUILD = os.path.join(ROOT, "build")
DIST = os.path.join(ROOT, "dist")


def make_icon():
    """The app icon, drawn here so no picture file has to be kept: a radio on a blue tile."""
    from PIL import Image, ImageDraw
    n = 1024
    im = Image.new("RGBA", (n, n), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    d.rounded_rectangle((40, 40, n - 40, n - 40), radius=200, fill=(31, 111, 235))
    d.line((330, 330, 640, 150), fill="white", width=34)                       # aerial
    d.ellipse((618, 128, 668, 178), fill="white")
    d.rounded_rectangle((170, 330, n - 170, 820), radius=70, fill="white")     # body
    d.ellipse((240, 420, 540, 720), fill=(31, 111, 235))                        # speaker
    d.ellipse((330, 510, 450, 630), fill="white")
    for y in (450, 540, 630):                                                   # dial lines
        d.rounded_rectangle((600, y, 790, y + 34), radius=17, fill=(31, 111, 235))
    os.makedirs(BUILD, exist_ok=True)
    png = os.path.join(BUILD, "icon.png")
    im.save(png)
    ico = os.path.join(BUILD, "icon.ico")
    im.save(ico, sizes=[(s, s) for s in (16, 24, 32, 48, 64, 128, 256)])
    return png, ico


def main():
    png, ico = make_icon()
    sep = os.pathsep
    args = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
            "--distpath", DIST, "--workpath", os.path.join(BUILD, "pyinstaller"),
            "--specpath", BUILD,
            "--add-data", f"{os.path.join(ROOT, 'gui', 'index.html')}{sep}gui",
            "--collect-data", "tzdata", "--collect-data", "certifi",
            "--hidden-import", "tzdata", "--hidden-import", "certifi",
            "--windowed"]                        # no console: the app shows its own small window
    if sys.platform == "darwin":
        args += ["--name", NAME, "--icon", png, "--osx-bundle-identifier", "org.p4radio.setup"]
    elif sys.platform == "win32":
        args += ["--name", NAME, "--icon", ico, "--onefile"]
    else:
        args += ["--name", "p4-radio-setup", "--onefile"]
    args.append(os.path.join(ROOT, "p4gui.py"))
    print(" ".join(f'"{a}"' if " " in a else a for a in args), flush=True)
    subprocess.run(args, check=True, cwd=ROOT)
    if sys.platform == "darwin":                 # one file to download
        app = os.path.join(DIST, NAME + ".app")
        out = os.path.join(DIST, "P4-Radio-Setup-macOS.zip")
        subprocess.run(["ditto", "-c", "-k", "--keepParent", app, out], check=True)
    print("built:", ", ".join(sorted(os.listdir(DIST))))


if __name__ == "__main__":
    main()

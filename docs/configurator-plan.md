# P4 Radio Configurator plan

Goal: a user who is not technical opens one program, picks and tests stations,
sets WiFi and settings, optionally copies music, and leaves with a USB stick
that sets up the radio when it is plugged in. No console, no text editing.

The work builds on `p4radio.py`, which already searches radio-browser, probes
streams the way the radio does, keeps the library and makes logos.

## What the firmware expects on the stick

Read from `../p4-radio` (`usb_service.c`, `storage.c`, `station_parse.c`, README 7.1):

```
radio/stations.txt      "name | url" or a bare url; at most 255 stations;
                        name < 48 bytes, url < 256 bytes; '#' lines are comments
radio/1.jpg … N.jpg     304x304 baseline 4:2:0 JPEG, < 256 KB, N = station number
radio/config.txt        key = value: timezone (POSIX TZ), eq, brightness (20-100)
radio/wifi.txt          line 1 SSID (<= 32 bytes), line 2 password (<= 63 bytes)
radio/firmware.bin      optional firmware update
radio/report.txt        written BY the radio after each import
radio/update.txt        written BY the radio after a firmware update
music/…                 MP3 files, any folder layout, read in place
```

- The stick must be FAT32. exFAT (most sticks of 64 GB and up) and NTFS are not read.
- Logos come from `radio/` itself, not from a subfolder.
- EQ presets: flat bass treble acoustic live voice classical loudness.
- A setting in `config.txt` is applied only when it differs from the last file.
- Credentials saved from the radio's phone setup page win over `wifi.txt`.

Two problems found in the current tool:

1. `build --logos` writes `out/logos/N.jpeg`; the radio reads `radio/N.jpg`.
2. `build --line` can write layouts such as `{url},{name}`, which the radio
   would read as one long URL. The stick writer always uses `name | url`.

## Shape of the configurator

A local web app served by the same Python program (`http.server` on
127.0.0.1, opened in the browser), not Tkinter:

- previews play in the browser's `<audio>` element through a local
  `/preview?url=` proxy that reuses `_connect()`, so ICY replies, playlists and
  redirects behave as they do on the radio, and mpv is not needed;
- a better interface for little effort: drag to reorder, logo thumbnails, progress;
- standard library plus Pillow; shipped with PyInstaller as a Windows `.exe`
  and a macOS app, so users do not install Python.

The CLI stays. Both front ends share one core.

### Screens

1. **Stick**: find removable drives, show size, free space and filesystem;
   refuse anything but FAT32 with a plain explanation (no formatting in v1).
   Continue a project saved on the stick; show the radio's last `report.txt`.
2. **Find stations**: radio-browser search by country, genre and language,
   MP3/AAC only; Play, Test and Add on every row.
3. **My stations**: reorder, rename (warn over 24, limit 47), logo (drop a
   file or a URL, show the real 304x304 result), remove, Test all with plain
   reasons, 255 counter.
4. **WiFi & settings**: SSID and password (stored in plain text on the stick,
   say so), timezone by city mapped to POSIX TZ (default from the PC),
   EQ preset, brightness.
5. **Music** (optional): copy MP3s into `music/` keeping folders, skip files
   already there, check free space, make names FAT32-safe, progress bar;
   warn about progressive embedded cover art.
6. **Review & write**: summary, atomic writes, sync, eject, then
   "plug it into the radio, it restarts once".

### Closing the loop

When the stick comes back, read `radio/report.txt` and `radio/update.txt`
and show what the radio stored and anything it rejected, in plain words.

### Firmware questions (decide separately)

- Done in step 4: a changed `wifi.txt` wins over setup-page credentials (as
  `config.txt` does), so changing WiFi in the configurator works without
  `wifi forget`.
- Optionally clear the password from the stick once imported.

## Testing

- `tests/fake_icecast.py` for probe and preview.
- Fake-stick directory tests of the written layout.
- Compile the firmware's own `station_parse.c` on the host and run it on
  every generated `stations.txt`, `wifi.txt` and `config.txt`.
- Finally a real stick in the radio, with `report.txt` read back.

## Steps

1. **Stick writer.** One function that writes the full `radio/` set (stations,
   `N.jpg` logos, `config.txt`, `wifi.txt`), validates against the firmware
   limits, writes only changed files, removes stale logos, refuses a non-FAT
   target, syncs. CLI: `p4radio.py stick <mount>` and `p4radio.py report <mount>`.
   `build` uses the same logo naming. Tests, including the firmware parser.
   The module split waits for step 2, when the GUI is the second user and the
   interfaces it needs are known.
2. Web GUI with Stick, My stations, Write (`p4gui.py`, using `p4radio.py` as the core).
3. Search, preview proxy and Test in the GUI.
4. WiFi and settings screen; report read-back.
5. Music copy.
6. PyInstaller builds for Windows and macOS.

## Status

- [x] 1 stick writer: `stick` and `report` commands, `tests/test_stick.py`
      (19 tests, including the firmware's parser built on the PC); the same
      writer was used by the step 2 test on the radio
- [x] 2 GUI: `p4gui.py` + `gui/index.html` (Stick, Stations, Write), loading a
      stick's stations, logos and settings; `tests/test_gui.py`. The core stays
      in `p4radio.py` (no separate core module: the GUI imports it).
      Checked read-only against the real stick (26 stations / 26 logos: only
      stations.txt and config.txt text would change, logos pass through).
      Tested on the radio 2026-11-15: a stick written from the page with one
      new station and a new logo was imported and works
- [x] 3 Find stations page (radio-browser search, add with favicon, .m3u/.pls
      files, add by address), ▶ preview through `/preview` (same connection code
      as the probe, MP3/AAC only), background tests with plain-language results.
      Found and fixed on the way: probe() stalled when an ICY metadata block
      arrived split across reads (Capital, LBC, Smooth Relax showed 0.00x);
      all 27 real stations now test OK. Writing that would take stations off
      the radio now asks first (26 stations became 1 in the step 2 trial).
      Tested on the radio 2026-11-15: found, tested and added stations work
- [x] 4 Settings page: WiFi (keep / set / remove, nearby networks with 5 GHz-only
      ones struck through, password in memory only), time zone by place
      converted to POSIX, EQ, brightness. Firmware (p4-radio storage.c): a
      changed wifi.txt now clears the setup-page credentials, an unchanged
      one doesn't; report.txt names the network replaced. Report read-back
      was done in step 2 (first page). Tested on the radio 2026-11-15: clock,
      EQ and WiFi change all applied (firmware branch wifi-txt-replaces-setup)
- [x] 5 Music page and `music` command: MP3s (+ folder pictures, converted when
      the radio can't show them) into a named folder in music/, FAT32-safe names,
      the radio's path/depth/count limits, songs already anywhere on the stick
      skipped, room checked, progress and cancel, remove a folder. Checked on
      the real stick (FAT32 renames, 2 s file times, picture conversion) with a
      test folder, removed afterwards. Tested on the radio 2026-11-15: copied
      music plays
- [~] 6 Packaging: `packaging/build.py` (PyInstaller, icon drawn in the script,
      tzdata + certifi bundled), `.github/workflows/build.yml` (Windows, macOS
      arm64, Linux). App mode: small Tk window (Open the page / Quit), fixed
      port 47860 so a second start reopens the running one. Linux build tested
      here (stick, WiFi list, https search/preview, time zones, logos, check,
      window under Xvfb); fixed on the way: LD_LIBRARY_PATH made lsblk fail in
      the packaged app. Windows/macOS: built and tested on GitHub, then by hand
      on real machines (stick detection, eject and WiFi list there are untested)

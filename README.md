# P4 Radio Setup – configurator for the ESP32-P4 internet radio

Prepares the radio's USB stick: find, listen to and test stations, arrange them
with their logos, set WiFi, time zone, EQ and brightness, and copy music. Plug
the stick into the radio and it sets itself up, even a radio whose flash was
wiped.

Two ways to use it:

- **P4 Radio Setup**, a page in your web browser: a packaged program for
  Windows, macOS and Linux, or `python3 p4gui.py` from the source.
- **`p4radio.py`**, the same on the command line.

Both keep the station list in `library.csv` (editable in LibreOffice) and the
settings in `p4radio.json`, in the `P4Radio` folder in your home folder.

## Download and run (no Python needed)

Download from the [latest release](https://github.com/lalith-ais/p4-radio-companion/releases/latest):

| System | File | First start |
|---|---|---|
| Windows 10/11 | `P4-Radio-Setup-Windows.exe` | Windows may say "Windows protected your PC": click **More info**, then **Run anyway** (the program isn't signed). |
| macOS (Apple silicon) | `P4-Radio-Setup-macOS.zip`: unzip, move `P4 Radio Setup.app` to Applications | macOS says it can't check the developer: open **System Settings → Privacy & Security** and click **Open Anyway** (or right-click the app, **Open**). |
| Linux (x86-64) | `p4-radio-setup-linux-x86_64` | `chmod +x p4-radio-setup-linux-x86_64`, then run it. |

It opens the setup page in your web browser and shows a small window: keep it
open while you use the page, and click **Quit** when you are done. Starting it
again while it runs just opens the page again.

The Windows and macOS programs pass all the tests on GitHub's machines but have
not yet been tried by hand: finding the stick, ejecting it and the list of
nearby WiFi networks have only run there in tests. Intel Macs are not built.

## Setting up the radio, step by step

The page has six steps along the top. The usual order:

1. **USB stick.** Plug a FAT32 stick into the computer; it appears within a
   few seconds. Sticks formatted as exFAT (most of 64 GB and up) or NTFS are
   refused with an explanation, because the radio can't read them. If the
   stick already has the radio's files, **Load the stations from this stick**
   brings in its stations, logos and settings, and the radio's last report is
   shown ("What the radio said last time").
2. **Find stations.** Search radio-browser.info by name, genre and country;
   only stations in formats the radio plays are shown. **▶** listens, **Add**
   adds the station with its logo. A station can also be added by its web
   address, or from a `.m3u` / `.pls` file (the "listen in your player" files
   station websites offer), chosen or dropped on the page. New stations are
   tested straight away.
3. **My stations.** The order is the station number on the radio: drag to
   move. Switch stations on and off, listen, **Test all**, and click a station
   to edit its name and address, see its test result, or change its logo
   (click or drop a picture on it, or give a picture's web address). Results
   are in plain words ("Sends Opus audio; the radio plays MP3 and AAC only").
4. **Settings.** WiFi: leave as it is, connect the radio to a network, or take
   the WiFi details off the stick. Networks this computer sees only on 5 GHz
   are struck through: the radio does 2.4 GHz only. The password is kept in
   memory while the program runs, never in a file on this computer. Time zone
   by place (this computer's zone first), EQ preset and screen brightness, each
   with "leave as it is".
5. **Music** (optional). The music folders on the stick, with **Remove**, and
   **Copy music from this computer**: pick folders in a folder browser, choose
   the folder on the stick each goes into, **Check**, then **Copy to the
   stick**. See [Music](#music).
6. **Write.** **Check what will change**, then **Write to the stick**, then
   **Eject the stick**. If the write would take stations off the radio (for
   example a new list written over a stick that had others), the page names
   them and asks first.

Then plug the stick into the back of the radio. It reads the stick, restarts
once, and leaves a report on the stick. Bring the stick back to the computer:
the first page shows what the radio stored and anything it rejected.

**Restoring a radio** after its flash was wiped or replaced: flash the firmware,
then plug in a stick written by the configurator. The radio takes its WiFi,
stations, logos and settings from the stick alone (tested with a fully erased
unit).

**Listening** goes through the program, which reaches the station the way the
radio does (redirects, playlists, old Shoutcast servers) and refuses formats
the radio can't play, so a station that plays on the page plays on the radio.
A station without a name shows the name it sends, as the radio does.

**WiFi changes and phone setup.** With radio firmware `dc3a8cd` or later
(p4-radio `main`), a `wifi.txt` that differs from the last one imported
replaces WiFi details that were set up from a phone, and the report names the
network it replaced. The same stick plugged in again changes nothing, so a
network fixed from a phone stays fixed. Older firmware keeps the phone's
network until `wifi forget` on the radio's console.

The page talks only to the program, on 127.0.0.1 (port 47860, or a free one),
with a key that changes every run.

## What goes on the stick

```
radio/stations.txt     "name | url" per line, in radio order
radio/1.jpg … N.jpg    logos, 304x304 baseline JPEG, N = station number
radio/config.txt       timezone (POSIX), eq, brightness
radio/wifi.txt         network name and password (only when asked for)
radio/report.txt       written by the radio after each import
radio/update.txt       written by the radio after a firmware update
radio/firmware.bin     a firmware update, if you put one there (p4-radio README 7.2)
music/…                MP3 songs, any folder layout, read in place by the radio
p4radio/               a copy of library.csv and p4radio.json (a backup)
```

## From the source

```bash
python3 -m venv ~/.venvs/p4radio && . ~/.venvs/p4radio/bin/activate
pip install pillow            # logos (the page needs it)
sudo apt install mpv          # only for the command line's `listen`
python3 p4gui.py              # the page; or ./p4radio.py gui
```

`p4gui.py` options: `--project DIR` (default `~/P4Radio`), `--port N`,
`--no-browser`, `--window` (the small control window, as the packaged program
shows), `--console`. Without a window, stop it with Ctrl+C.

## Command line

```bash
./p4radio.py import /media/$USER/STICK         # a whole stick: stations, logos, settings
./p4radio.py import /media/sd/stations.txt     # or a stations file; line layout is auto-detected
./p4radio.py search --tag jazz --country GB    # radio-browser.info, pick by number
./p4radio.py search --country GB --limit 0 --add all   # every match (paged), add them all
./p4radio.py add https://example.com/stream --name "My Station"
./p4radio.py test                              # probe all -> station_log.md (+ logs/ history)
./p4radio.py listen --undecided                # play in mpv: k=keep d=drop n=next r=replay e=rename q=quit
./p4radio.py list
./p4radio.py move 7 1                          # reorder (order = station numbers on the radio)
./p4radio.py set 4 --logo ~/art/radiox.png     # or a URL; search fills in the favicon
./p4radio.py stick /media/$USER/STICK --wifi MyNetwork --timezone Europe/London
./p4radio.py music /media/$USER/STICK ~/Music --to favourites
./p4radio.py report /media/$USER/STICK         # after the radio has imported the stick
```

Selections (`test`, `listen`, `set`, `rm`) accept `all`, `3`, `1,4,7-9`, or a
name fragment. Run from the folder that holds `library.csv`, or pass
`-L ~/P4Radio/library.csv -C ~/P4Radio/p4radio.json` to share the page's list.

### Writing the radio's USB stick

`stick` writes the `radio/` folder the firmware imports: `stations.txt`, logos
`1.jpg … N.jpg`, `config.txt` and, when asked, `wifi.txt`.

```bash
./p4radio.py stick /media/$USER/STICK                    # stations + logos
./p4radio.py stick /media/$USER/STICK --wifi MyNetwork   # asks for the password
./p4radio.py stick /media/$USER/STICK --timezone Europe/London --eq bass --brightness 70
./p4radio.py stick /media/$USER/STICK --dry-run          # show what would change
./p4radio.py stick /media/$USER/STICK --eject            # unmount and power off when done
```

- Only the top of a removable FAT32 drive is accepted. exFAT and NTFS sticks
  are refused (the radio cannot read them), and so are fixed disks.
  `--force` writes into a plain folder instead.
- Stations follow `build`'s rules (keep=y, tested OK or WARN;
  `--include-untested` for stations read from a stick). Lines are always
  `name | url`, whatever `--line` layout `build` uses. Names are cut to the
  radio's 47 bytes, `|` becomes `/`, and a station whose URL the radio would
  refuse is left out, so line N always matches `N.jpg`.
- Only changed files are rewritten, and each one goes to a temp name first and
  is then renamed. Logos for station numbers that no longer exist, and
  duplicates such as `3.jpeg` or `03.jpg` next to `3.jpg`, are removed.
  `--no-logos` leaves the logos alone. A stick with no logos at all makes the
  radio keep the logos it already has.
- `--timezone` takes a zone name (`Europe/London`), `local`, or a POSIX string.
  It is converted to the POSIX form the radio needs. Timezone, EQ and brightness
  are remembered in `p4radio.json` and written to `config.txt` every time.
- `wifi.txt` is written only with `--wifi` and removed with `--no-wifi`.
  Otherwise it is left as it is. It holds the password in plain text.
- A copy of `library.csv` and `p4radio.json` goes into `p4radio/` on the stick.
- `import /media/$USER/STICK` reads a whole stick back: its stations, logos
  (copied to `logos/` next to the library) and settings. Logos that are already
  304x304 baseline JPEGs are written back byte for byte, so the radio does not
  recopy them.

### Music

```bash
./p4radio.py music /media/$USER/STICK                          # list what is on the stick
./p4radio.py music /media/$USER/STICK ~/Music --to favourites --dry-run
./p4radio.py music /media/$USER/STICK ~/Albums/Abba           # into music/Abba
./p4radio.py music /media/$USER/STICK --remove Abba
```

The radio plays MP3 files from `music/`, read in place. Copying follows its rules
(p4-radio `library.c`, `art_service.c`):

- Each folder goes into a folder in `music/`: its own name by default, or the
  one given (`--to`, or the "on the stick" box on the page; empty = `music/`
  itself).
- Only `.mp3` songs are copied; FLAC, M4A, Ogg and other formats are counted and
  left out. `cover.jpg`, `folder.jpg` and `front.jpg` go along (a PNG becomes a
  JPEG); a picture the radio can't show (progressive, over 1600 px or 2 MB) is
  converted to a baseline JPEG.
- Names are made FAT32-safe (`"*/:<>?\|` become `_`, reserved names like `CON`
  get a `_`), file names are shortened to keep paths within the radio's 191
  bytes, and anything more than 8 folders deep is skipped.
- A song with the same name and size anywhere on the stick is not copied again,
  so copying a folder again only adds what is new. Each file is written under a
  temp name and renamed; the original's time is kept for the next comparison.
- The copy stops before it starts if the stick is too small, and warns past the
  radio's 4000 songs. After copying, it counts songs whose embedded picture the
  radio can't show.

### What `test` checks

- DNS, TCP and TLS connection (certificate verified), connect time
- redirects (up to 8 hops), `.pls` / `.m3u` / `.asx` / `.xspf` wrappers, both
  `HTTP/1.x` and old Shoutcast `ICY 200 OK` replies
- HLS (`#EXT-X-`), Ogg/Opus/Vorbis/FLAC and fMP4 are marked FAIL (the radio does MP3 + ADTS AAC only)
- sniffs the audio (3 consecutive valid frames): MP3 bitrate/rate/channels; for ADTS
  AAC it tells AAC-LC from HE-AAC (core rate ≤ 24 kHz means implicit SBR) and
  HE-AACv2 (mono core at low bitrate means PS)
- ICY metadata is stripped and the current StreamTitle is shown
- **speed** = sustained throughput ÷ nominal bitrate, measured after the server's
  connect burst. WARN below 0.92×, because the radio's buffer would slowly drain
- estimated P4 decode CPU from the bench figures (MP3 ~5.5 %, AAC-LC ~5.3 %,
  HE-AAC ~11 %, HE-AACv2 ~15 %)
- flags station names over 24 characters (too busy on the player screen)

`station_log.md` is the readable report. A timestamped copy goes into `logs/`
on every run, so you can compare runs over time. The page runs the same test.

### `build`: stations.txt and logos in a folder

`build --out DIR --logos` writes `stations.txt` and `N.jpg` logos into a folder,
for copying by hand (for example to the root of an SD card on bench units).
`import` records the layout of an existing file (e.g. `{name}|{url}`) in
`p4radio.json`, and `build` writes the same layout back. Without an import the
default is `{name}|{url}`. Override with `build --line '{url},{name}'` (the
radio itself only reads `name | url` or a bare URL). `build` writes OK and WARN
stations marked keep=y, in library order. Line N matches logo `N.jpg`.

Other `build` options: `--resolved` writes post-redirect URLs (usually leave this
off, because CDN redirect targets often carry expiring tokens); `--header` adds a
`#` comment line; `--size` (multiple of 16, default 304); `--bg '#101418'`.

Logos are baseline (non-progressive) 4:2:0 JPEGs for the P4 hardware decoder.
Images are fitted centrally on the background colour. Favicons under 48 px are
rejected and tiny ones aren't blown up more than 2×. Stations without a logo
show the initials tile on the radio.

## Tests

```bash
for t in test_stick test_music test_probe test_gui; do python3 tests/$t.py; done
```

- `tests/test_stick.py`: the stick writer. It builds the radio firmware's own
  parser (`../p4-radio/main/station_parse.c`, or `$P4_RADIO_FW`) with gcc and
  runs it over every file it writes. Without gcc, the firmware or Pillow, those
  tests are skipped.
- `tests/test_music.py`: music copying, including pictures in made-up ID3 tags.
- `tests/test_probe.py`: `probe()` against every stream `tests/fake_icecast.py`
  serves.
- `tests/test_gui.py`: the page's API, with a folder standing in for the stick.

`tests/fake_icecast.py 8765` serves MP3 with ICY metadata (also split across
reads, `/icysplit`), HE-AAC, HE-AACv2, AAC-LC, an `ICY 200 OK` server, a .pls,
a 302, HLS, Opus, a throttled stream and a 404, so all of this runs without the
internet.

## Building the programs

```bash
pip install -r packaging/requirements.txt
python packaging/build.py      # -> dist/
```

PyInstaller only builds for the system it runs on, so
`.github/workflows/build.yml` builds Windows, macOS (Apple silicon) and Linux
on GitHub: push a tag such as `v0.2`, or **Actions → build → Run workflow**.
Each run also runs all four test suites on each system and posts every suite's
result as a note on the run (readable without signing in); the programs are
the run's artifacts. For a `v*` tag, every test must pass, and the programs are
then published as a GitHub release with the notes in `packaging/release-notes.md`.

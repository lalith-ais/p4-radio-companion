# p4radio – Linux companion for the ESP32-P4 internet radio

One Python file, standard library only (Pillow only for logos). It keeps a master
station library (`library.csv`, editable in LibreOffice), probes every stream the
way the radio will, lets you audition stations in mpv, and builds the
`stations.txt` + `N.jpg` logos that go to the radio, straight onto its USB stick.

```bash
python3 -m venv ~/.venvs/p4radio && . ~/.venvs/p4radio/bin/activate
pip install pillow            # only needed for build --logos
sudo apt install mpv          # only needed for listen
```

## Typical session

```bash
./p4radio.py import /media/sd/stations.txt     # existing list; line layout is auto-detected
./p4radio.py search --tag jazz --country GB    # radio-browser.info, pick by number
./p4radio.py search --country GB --limit 0 --add all   # every match (paged), add them all
./p4radio.py add https://example.com/stream --name "My Station"
./p4radio.py test                              # probe all -> station_log.md (+ logs/ history)
./p4radio.py listen --undecided                # play in mpv: k=keep d=drop n=next r=replay e=rename q=quit
./p4radio.py list
./p4radio.py move 7 1                          # reorder (order = station numbers on the radio)
./p4radio.py set 4 --logo ~/art/radiox.png     # or a URL; search fills in the favicon
./p4radio.py stick /media/$USER/STICK --wifi MyNetwork --timezone Europe/London
./p4radio.py report /media/$USER/STICK         # after the radio has imported the stick
```

## The configurator page

```bash
python3 p4gui.py              # or ./p4radio.py gui
```

The page opens in your web browser. It has five steps:

1. **USB stick**: lists removable drives and refuses anything that is not
   FAT32. It shows what is on the stick and the radio's last report, and
   **Load the stations from this stick** brings in its stations, logos and
   settings.
2. **Find stations**: search radio-browser.info by name, genre and country
   (only formats the radio plays are shown), ▶ to listen, **Add** to add with
   its logo. Stations can also be added by address or from a `.m3u` / `.pls`
   file. New stations are tested straight away.
3. **My stations**: drag to reorder (the order is the station number on the
   radio), switch stations on and off, listen, **Test all**, edit names and
   addresses, and click or drop a picture on a logo. Test results are in plain
   words ("Sends Opus audio; the radio plays MP3 and AAC only").
4. **Settings**: WiFi (leave as it is, connect to a network, or take the
   WiFi details off the stick), time zone by place (this computer's zone
   first), EQ preset and screen brightness, each with "leave as it is".
   Networks this computer sees only on 5 GHz are struck through: the radio
   does 2.4 GHz only. The WiFi password is kept in memory, never in a file on
   this computer.
5. **Write**: **Check what will change** first, then **Write to the stick**,
   then **Eject**. If the write would take stations off the radio, the page
   names them and asks first.

Listening goes through the program, which reaches the station the way the
radio does (redirects, playlists, old Shoutcast servers) and refuses formats
the radio can't play, so a station that plays here plays on the radio. No mpv
needed. A station without a name shows the name it sends, as the radio does.

The station list, settings and logos are kept in `~/P4Radio` (`--project DIR`
to change), the same `library.csv` / `p4radio.json` the commands below use.
The page talks only to the program on 127.0.0.1, with a per-run key. Close the
terminal window (or press Ctrl+C) to stop it.

Not there yet: music.

A changed `wifi.txt` replaces WiFi details set up from a phone only with radio
firmware that has that change (p4-radio after f575569); older firmware keeps
the phone's network until `wifi forget`. Windows and macOS drive detection is written but untested.

## Writing the radio's USB stick

`stick` writes the `radio/` folder the firmware imports: `stations.txt`, logos
`1.jpg … N.jpg`, `config.txt` and, when asked, `wifi.txt`. Plug the stick into
the radio; it imports the files, restarts once and leaves `radio/report.txt`,
which `report` prints.

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
- Stations follow `build`'s rules (keep=y, tested OK or WARN). Lines are always
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
  Otherwise it is left as it is. It holds the password in plain text. WiFi
  details saved from the radio's phone setup page win over `wifi.txt`.
- A copy of `library.csv` and `p4radio.json` goes into `p4radio/` on the stick.
- `import /media/$USER/STICK` reads a whole stick back: its stations, logos
  (copied to `logos/` next to the library) and settings. Logos that are already
  304x304 baseline JPEGs are written back byte for byte, so the radio does not
  recopy them.


Selections (`test`, `listen`, `set`, `rm`) accept `all`, `3`, `1,4,7-9`, or a name fragment.

## What `test` checks

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
on every run, so you can compare runs over time.

## stations.txt layout

`import` records the layout of your existing file (e.g. `{name}|{url}`) in
`p4radio.json`, and `build` writes the same layout back. Without an import the
default is `{name}|{url}`. Override with `build --line '{url},{name}'`.
`build` writes OK and WARN stations marked keep=y, in library order. Line N
matches logo `N.jpg`.

Other `build` options: `--resolved` writes post-redirect URLs (usually leave this
off, because CDN redirect targets often carry expiring tokens); `--header` adds a
`#` comment line (only if the firmware skips comments); `--size` (multiple of 16,
default 304); `--bg '#101418'`.

Logos are baseline (non-progressive) 4:2:0 JPEGs for the P4 hardware decoder. Images are
fitted centrally on the background colour. Favicons under 48 px are rejected
and tiny ones aren't blown up more than 2×. Stations without a logo show the
initials tile on the radio.

## Testing without the network

`tests/fake_icecast.py 8765` serves MP3+ICY metadata (also split across
reads, `/icysplit`), HE-AAC, HE-AACv2,
AAC-LC, an `ICY 200 OK` server, a .pls, a 302, HLS, Opus, a throttled stream
and a 404.

`tests/test_stick.py` tests the stick writer. It builds the radio firmware's own
parser (`../p4-radio/main/station_parse.c`, or `$P4_RADIO_FW`) with gcc and
runs it over every file it writes. Without gcc, the firmware or Pillow, those
tests are skipped. `tests/test_gui.py` drives the configurator page's API with a
folder standing in for the stick. `tests/test_probe.py` runs `probe()` against
every stream `fake_icecast.py` serves.

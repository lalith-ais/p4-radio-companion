# p4radio – Linux companion for the ESP32-P4 internet radio

One Python file, standard library only (Pillow only for logos). It keeps a master
station library (`library.csv`, editable in LibreOffice), probes every stream the
way the radio will, lets you audition stations in mpv, and builds the
`stations.txt` + `logos/N.jpeg` that go to the radio.

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
./p4radio.py build --out card --logos          # card/stations.txt + card/logos/1.jpeg ...
```

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
matches logo `N.jpeg`.

Other `build` options: `--resolved` writes post-redirect URLs (usually leave this
off, because CDN redirect targets often carry expiring tokens); `--header` adds a
`#` comment line (only if the firmware skips comments); `--size` (multiple of 16,
default 304); `--bg '#101418'`.

Logos are baseline (non-progressive) 4:2:0 JPEGs for the P4 hardware decoder. Images are
fitted centrally on the background colour. Favicons under 48 px are rejected
and tiny ones aren't blown up more than 2×. Stations without a logo show the
initials tile on the radio.

## Testing without the network

`tests/fake_icecast.py 8765` serves MP3+ICY metadata, HE-AAC, HE-AACv2,
AAC-LC, an `ICY 200 OK` server, a .pls, a 302, HLS, Opus, a throttled stream
and a 404.

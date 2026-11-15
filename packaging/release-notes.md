**P4 Radio Setup** prepares the USB stick of the ESP32-P4 internet radio
([p4-radio](https://github.com/lalith-ais/p4-radio)): find, listen to and test
stations, arrange them with their logos, set WiFi, time zone, EQ and brightness,
and copy MP3 music. Plug the stick into the radio and it sets itself up, even
after its flash was wiped.

## Download

| System | File | First start |
|---|---|---|
| Windows 10/11 | `P4-Radio-Setup-Windows.exe` | "Windows protected your PC": click **More info**, then **Run anyway** (the program isn't signed). |
| macOS (Apple silicon) | `P4-Radio-Setup-macOS.zip`: unzip, move the app to Applications | macOS can't check the developer: **System Settings → Privacy & Security → Open Anyway** (or right-click the app, **Open**). |
| Linux (x86-64) | `p4-radio-setup-linux-x86_64` | `chmod +x p4-radio-setup-linux-x86_64`, then run it. |

It opens the setup page in your web browser and shows a small window; click
**Quit** there when you are done. The steps are described in the
[README](https://github.com/lalith-ais/p4-radio-companion#setting-up-the-radio-step-by-step).

## What it does

- **USB stick**: finds FAT32 sticks, refuses exFAT/NTFS with an explanation,
  loads an existing stick's stations, logos and settings, shows the radio's
  last report.
- **Find stations**: radio-browser.info search (only formats the radio plays),
  listen, add with logo; add by address or from `.m3u` / `.pls` files.
- **My stations**: reorder, switch on/off, rename, logos, test with
  plain-language results.
- **Settings**: WiFi (2.4 GHz networks, password never saved on the computer),
  time zone by place, EQ, brightness.
- **Music**: copy MP3 folders, skipping songs already on the stick; folder
  pictures converted when the radio can't show them.
- **Write**: check what will change, write, eject; asks before taking stations
  off the radio.

Tested on the radio: stations, logos, settings, WiFi change, music, and a full
restore of a wiped unit from the stick.

## Notes

- A changed WiFi network replaces one set up from a phone only with radio
  firmware `dc3a8cd` or later (p4-radio `main`).
- The Windows and macOS programs pass all tests on GitHub's machines but have
  not yet been tried by hand; finding the stick, ejecting it and the nearby-WiFi
  list have only run there in tests. Intel Macs are not built.

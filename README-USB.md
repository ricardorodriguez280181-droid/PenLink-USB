# PenLink USB

An iPad app plus a PC program that talk over the USB cable. The PC streams a monitor to the iPad as
H.264 video (encoded by your GPU, decoded by the iPad's hardware; JPEG is the fallback); the Apple Pencil's position, pressure and tilt go back and become a real Windows pen
(through your existing `pen.py`). The old browser version in `penlink/` still works and is unchanged.

```
iPad app (Swift)  <--- USB cable (usbmuxd) --->  usb_server.py  ->  capture.py (screen) / pen.py (pen)
 listens on 8765                                  connects to it
```

## What is in the box

| Path | What it is |
|---|---|
| `ios/` | The iPad app: SwiftUI + UIKit, built by XcodeGen (`project.yml`), no Xcode project to edit |
| `.github/workflows/build-ios.yml` | Builds an unsigned `PenLink.ipa` on a GitHub macOS runner |
| `penlink/usb_server.py` | PC side: finds the iPad, connects, streams frames, injects the pen |
| `penlink/usbmux.py` | Small usbmuxd client (no extra Python packages needed) |
| `penlink/PenLink-USB.bat` | Double-click launcher for the PC side |
| `penlink/tools/selftest_usb.py` | Tests the PC side with a fake iPad (no hardware needed) |
| `PROJECT_NOTES.md` | Project memory: paste it into a new Claude chat to continue where you stopped |

## One-time setup

### 1. Build the app on GitHub
1. Create a new GitHub repository and upload this whole folder (keep the folder structure; `.github` must be at the top).
   Public repos get free macOS build minutes; private repos use your free quota at a 10x rate.
2. Open the **Actions** tab. The build starts on every push (or press **Run workflow**). It takes about 5 minutes.
3. When it is green, open the run and download the artifact **PenLink-ipa**. Unzip it to get `PenLink.ipa`.
4. If it is red: open the run, the **Summary** page lists the compiler errors. Copy them to me and I will fix them.
   The full log is the **build-log** artifact.

### 2. Install iTunes on the PC
Install **iTunes from apple.com** (not the Microsoft Store version; this is what Sideloadly also recommends).
This installs *Apple Mobile Device Service* (usbmuxd), which both Sideloadly and PenLink need.
The Microsoft Store "Apple Devices" app also installs it, so if you already have that, try it first.

### 3. Put the app on the iPad with Sideloadly
1. On the iPad: Settings > Privacy & Security > **Developer Mode** > on (it restarts the iPad).
2. Plug the iPad in, unlock it, tap **Trust**.
3. In Sideloadly pick `PenLink.ipa`, sign in with your Apple ID, press Start.
4. On the iPad: Settings > General > VPN & Device Management > your Apple ID > **Trust**.
5. A free Apple ID lasts 7 days; re-run Sideloadly (or leave it open with auto-refresh) when the app stops opening.

### 4. PC side
Same venv and `requirements.txt` as before (no new packages). Then:

```
penlink\PenLink-USB.bat            # real use
penlink\PenLink-USB.bat --demo     # drawing canvas instead of your screen, nothing is injected: test this first
```

## Using it
1. Plug in the iPad, open the **PenLink** app and leave it in the foreground (iOS pauses it otherwise).
2. Start `PenLink-USB.bat`. It connects by itself and reconnects if you unplug and replug.
   The PC window prints what it is waiting for (no iPad, app not open, and so on).
3. Draw with the Pencil. Fingers and palms are ignored. Pick Windows Ink in your drawing app (see the main README).
4. **Three-finger tap** on the iPad shows a log (also mirrored in the PC window, prefixed `[iPad]`).

## Video: H.264 (default) and JPEG
H.264 is the default. ffmpeg captures the screen and your AMD GPU encodes it (`h264_amf`), so Python no longer does
the heavy work per frame; the iPad decodes it in hardware. Needs ffmpeg once: `winget install Gyan.FFmpeg`.
If ffmpeg or the encoder is missing or fails, PenLink prints why and **falls back to JPEG by itself**.
The log line `Streaming H264 ...` or `Streaming JPEG ...` tells you which one you got.

Options (add them after the .bat name):

| Option | Default | |
|---|---|---|
| `--codec h264 / jpeg` | h264 | JPEG is the slow fallback |
| `--bitrate 30M` | 30M | H.264 quality. Try 20M to 60M; USB has plenty of room |
| `--fps 60` | 60 | |
| `--max-width 1920` | 1920 | Stream width |
| `--quality 80` | 80 | JPEG only |
| `--monitor N`, `--list-monitors` | 1 | Which screen |
| `--software` | off | Encode on the CPU (libx264) if the AMD encoder fails |
| `--capture gdigrab` | ddagrab | Other capture method if the picture is black |
| `--output-idx N` | auto | ddagrab: pick the screen output yourself (0-based) |
| `--flip-tilt-x`, `--flip-tilt-y` | off | Fix mirrored pen tilt |
| `--demo` | off | Test canvas, nothing injected |
| `--serial UDID` | any | Pick one iPad |

## Known limits
- H.264 on the iPad was written without being able to run it on a real device; if the picture stays black in H.264 but
  `--codec jpeg` works, send me the iPad log (three-finger tap) and the PC window.
- No Pencil double-tap / squeeze and no barrel button or eraser yet (Milestone 2).
- Pencil hover works only on iPads that report it (M2 iPad Pro and newer).
- **Tilt direction is untested on real hardware.** If strokes lean the wrong way in Krita, add `--flip-tilt-x` and/or `--flip-tilt-y`.
- The app accepts the connection on all network interfaces, not only USB. The only thing a stranger on your Wi-Fi
  could do is draw on your iPad screen while the app is open; I will lock it to USB-only once the connection is proven.
- The PC side is tested end to end against a fake iPad (both codecs, and the H.264 bytes are checked to decode in ffmpeg).
  The Swift app is only checked by the CI build and by your iPad.

## Troubleshooting
- **"Cannot talk to usbmuxd"**: iTunes / Apple Devices is not installed, or *Apple Mobile Device Service* is not running (Win+R, `services.msc`).
- **"No iPad found on USB"**: unlock the iPad, tap Trust, try another cable or port. Sideloadly seeing the iPad means this part is fine.
- **"iPad found. Open the PenLink app"**: the app is closed, in the background, or the iPad is locked.
- **App stops opening after a week**: free Apple ID signing expired; re-install with Sideloadly.
- **Black picture in H.264**: try `--software`, then `--capture gdigrab`, then `--codec jpeg`.
- **"no h264_amf encoder"**: update ffmpeg (`winget upgrade Gyan.FFmpeg`) or use `--software`.
- **Check the PC side without an iPad**: `python penlink/tools/selftest_usb.py`; every line should say PASS.

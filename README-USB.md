# PenLink USB (Milestone 1)

An iPad app plus a PC program that talk over the USB cable. The PC streams a monitor to the iPad as
JPEG frames; the Apple Pencil's position, pressure and tilt go back and become a real Windows pen
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

Options: `--monitor 2`, `--fps 60`, `--max-width 1920`, `--quality 80`, `--flip-tilt-x`, `--flip-tilt-y`,
`--serial <UDID>`, `--list-monitors`.

## Known limits of Milestone 1
- JPEG only. H.264 (lower bandwidth, hardware decoding) is Milestone 3.
- No Pencil double-tap / squeeze and no barrel button or eraser yet (Milestone 2).
- Pencil hover works only on iPads that report it (M2 iPad Pro and newer).
- **Tilt direction is untested on real hardware.** If strokes lean the wrong way in Krita, add `--flip-tilt-x` and/or `--flip-tilt-y`.
- The app accepts the connection on all network interfaces, not only USB. The only thing a stranger on your Wi-Fi
  could do is draw on your iPad screen while the app is open; I will lock it to USB-only once the connection is proven.
- I could not compile the Swift code in my environment. The PC side is tested end to end against a fake iPad;
  the first CI build may need a round of fixes from the error summary.

## Troubleshooting
- **"Cannot talk to usbmuxd"**: iTunes / Apple Devices is not installed, or *Apple Mobile Device Service* is not running (Win+R, `services.msc`).
- **"No iPad found on USB"**: unlock the iPad, tap Trust, try another cable or port. Sideloadly seeing the iPad means this part is fine.
- **"iPad found. Open the PenLink app"**: the app is closed, in the background, or the iPad is locked.
- **App stops opening after a week**: free Apple ID signing expired; re-install with Sideloadly.
- **Check the PC side without an iPad**: `python penlink/tools/selftest_usb.py` should print three PASS lines.

# PenLink: project notes (for people and for Claude)

Claude does not remember earlier chats on a free plan. This file is the memory. Keep it in the GitHub repo
so the whole team shares it. **Start every new chat by pasting this file** (and upload the repo zip if code changes
are needed). **End every session by asking: "Update PROJECT_NOTES.md with what we did"** and commit the result.

## Goal
Use an iPad + Apple Pencil as a pen tablet for a Windows PC. Native iPad app over a USB cable (low latency),
replacing the older Safari/Wi-Fi version (still in `penlink/`, unchanged).

## Our setup
- PC: Windows, AMD GPU (video is encoded by ffmpeg `h264_amf`). Python venv in `penlink/.venv`.
- iPad: sideloaded app. **No Mac available.** Free Apple ID, so the install expires every 7 days (re-run Sideloadly).
- Tools: GitHub Actions (builds the unsigned .ipa on macOS), Sideloadly (signs + installs from the PC),
  iTunes from apple.com (provides usbmuxd / Apple Mobile Device Service).
- Claude cannot compile Swift or touch the iPad. Compiler errors come from the Actions "Summary" page; paste them to Claude.

## Architecture
```
iPad app (Swift, listens on TCP 8765, loopback+all interfaces)  <-- USB / usbmuxd -->  usb_server.py (PC, client)
   PenView: Pencil touches -> JSON samples ->                                          -> pen.py (Windows pointer injection)
   H264Player (AVSampleBufferDisplayLayer) / UIImageView (JPEG) <- video <-            <- capture.py (ffmpeg or mss+Pillow)
```
USB is one-way: the PC connects to the iPad's port, never the reverse. `usbmux.py` is our own tiny usbmuxd client
(Windows: 127.0.0.1:27015). `usb_server.py` polls for the iPad and reconnects by itself.

Wire format (both directions): `[u32 big-endian N][u8 type][N-1 bytes payload]`
1 HELLO (PC->iPad JSON fw,fh,codec) | 2 FRAME (JPEG) | 3 PEN (iPad->PC JSON [[x,y,pressure,tiltX,tiltY,state]], state 0 hover/1 contact/2 out,
x/y are 0..1 over the picture) | 4 ACK (JPEG flow control) | 5 LOG | 6 VCONFIG (JSON nal,sps,pps base64) | 7 VIDEO (flag byte bit0=keyframe + AVCC frame).
H.264 comes from ffmpeg's fragmented MP4 output (the same pipeline as the web version); `usb_server.py` takes SPS/PPS from the avcC box
and the AVCC-formatted frame from each mdat, which is exactly what CMSampleBuffer needs. Falls back to JPEG if ffmpeg fails.

## Files
- `ios/project.yml` (XcodeGen), `ios/PenLink/*.swift`: `USBServer` (network, Network.framework), `PenView` (UIKit touches, hover, display),
  `H264Player`, `Wire` (framing, PenSample), `AppLog` (3-finger tap shows the log, also mirrored to the PC), `ContentView`.
- `.github/workflows/build-ios.yml`: builds `PenLink-ipa` (artifact). Workflow must be at repo root `.github/workflows/`.
  (An old GitHub "Swift" starter workflow running `swift build` will fail; delete it.)
- `penlink/usb_server.py`, `usbmux.py`, `PenLink-USB.bat`, `tools/selftest_usb.py` (fake iPad + fake usbmuxd, both codecs).
- `penlink/` rest: original web version (`server.py`, `capture.py`, `pen.py`, `gui.py`, `auth.py`, `static/`). `usb_server.py` reuses `capture.py`, `pen.py`, `server.apply_samples`.
- `README-USB.md`: user guide and options.

## Status
- Milestone 1 (JPEG over USB): built by CI, installed, ran on real hardware. Measured **18 fps** on the real screen (JPEG encoded by Python is the bottleneck, not USB).
- Milestone 3 (H.264, GPU encode, hardware decode): written. PC side passes `selftest_usb.py` (incl. "stream decodes in ffmpeg").
  **The iPad H.264 code has not yet been run on a device.** Expected to fix the fps.
- Not yet reported by the team: pen feel, tilt direction, latency, H.264 fps. Fill in below after testing.

## Known risks / open questions
- Tilt sign may be mirrored: use `--flip-tilt-x/--flip-tilt-y`, then make the fix permanent in `PenView.sample()`.
- The iPad listener accepts connections on all interfaces (should be restricted to USB/loopback once proven).
- Free Apple ID: app expires after 7 days; max 3 sideloaded apps.
- H.264 keyframes every 2 s; after an iPad decoder error it waits up to 2 s for the next keyframe.

## Roadmap
- Milestone 2: Pencil double-tap / squeeze (UIPencilInteraction) -> eraser or barrel button; Windows injector needs pen flags
  (`PEN_FLAG_BARREL`, `PEN_FLAG_INVERTED`/`ERASER`) in `pen.py` and a new field in the PEN message.
- Lock the listener to USB-only. Optional: add a "USB" mode to `gui.py`.
- Possible: HEVC, hardware JPEG fallback tuning, on-demand keyframe request (new message type).

## Session log (newest first; add a line each session: date, who, what, result)
- Added H.264 (Milestone 3), selftest for both codecs, this notes file. Result of device test: (fill in)
- Milestone 1 delivered; CI built; installed with Sideloadly; JPEG ran at 18 fps.

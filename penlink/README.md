# PenLink

Use an iPad and Apple Pencil as a pen tablet for a Windows PC, with no app on the iPad.

- The PC streams a monitor to a web page in Safari.
- The page reads Apple Pencil position, pressure and tilt (Pointer Events) and sends them back.
- The PC injects them as a real Windows pen using the Pointer Injection API, so Krita, Photoshop,
  Clip Studio and others see a pen with pressure (through Windows Ink).

Fingers are ignored on purpose, which gives you free palm rejection.

> **USB version:** a native iPad app that connects through the cable is in `../README-USB.md`
> (`usb_server.py`, `PenLink-USB.bat`). The browser version below still works as before.

## Setup (Windows 10 1809 or newer, 64-bit Python 3.9+)

Once, in PowerShell inside this folder:

```
py -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
winget install Gyan.FFmpeg
```

(If PowerShell refuses to run Activate.ps1, run `Set-ExecutionPolicy -Scope Process Bypass` first.)
ffmpeg is only needed for the H.264 and HEVC modes. PenLink finds it by itself, even in a window
that was open before you installed it.

## Using it

Double-click **PenLink.bat** (or run `python gui.py`). In the window:

1. Pick your monitor and video mode, press **Start**.
2. On the iPad, open Safari (same Wi-Fi) and go to the address shown in the window.
3. The first time, type the 6-digit PIN shown in the window. The iPad remembers it, so next time
   you only open the address. "Forget paired iPads" in the window undoes that.

Then in your drawing app:

- **Krita:** Settings > Configure Krita > Tablet Settings > Tablet Input API: *Windows Ink*
- **Photoshop:** Preferences > Tools > *Use Windows Ink*
- **Clip Studio Paint:** File > Preferences > Tablet > *Windows Ink*

Tick **Test mode** first if you want to check the iPad side: you get a drawing canvas and nothing is
injected into Windows. Windows Firewall will ask about Python the first time: allow private networks.

## Video modes

- **H.264 / HEVC (GPU):** encoded by your AMD card through ffmpeg. Sharper and lighter on Wi-Fi.
  Safari plays it through its video player, which buffers a little, so it can feel laggier.
  Needs iPadOS 17.1+. The iPad page shows a "buffer" number in ms: that is the extra delay.
- **JPEG:** the simple stream. Larger on Wi-Fi, but usually the lowest delay for drawing.

If drawing feels laggy in H.264, try JPEG. "Ping" on the iPad page only measures the pen's trip to the PC,
not the delay of the picture coming back.

Other tips:

- Tap the full-screen button, or use Share > Add to Home Screen and open it from there, to hide Safari's bars.
- Set the iPad's Auto-Lock to Never (Settings > Display & Brightness) while you work.
- A 5 GHz Wi-Fi connection, with the PC on Ethernet, gives the lowest lag.
- If the app you draw in runs as administrator, start PenLink as administrator too.

## Command line (optional)

```
python server.py --demo              test mode
python server.py                     real use; prints the address and a PIN
python server.py --list-monitors     show monitor numbers
python server.py --monitor 2         share a different monitor (0 = all, needs --capture gdigrab)
python server.py --codec jpeg        h264 (default), hevc or jpeg
python server.py --max-width 1280    smaller stream
python server.py --bitrate 20M       H.264/HEVC bitrate (default 12M)
python server.py --quality 60        JPEG quality
python server.py --fps 30            frame rate (default 60)
python server.py --capture gdigrab   fallback capture method if ddagrab fails
python server.py --output-idx 1      ddagrab: pick the DXGI output yourself (0-based)
python server.py --software          encode on the CPU instead of the GPU
python server.py --ffmpeg PATH       use a specific ffmpeg
```

## Security

Pairing needs the PIN shown on the PC, and five wrong PINs lock pairing until PenLink is restarted.
It is still plain HTTP, so only use it on a network you trust: someone on the same network could
watch the traffic. The pairing is stored in `penlink_secret.txt`; delete it (or use "Forget paired iPads")
to unpair everything.

## Troubleshooting

- **Page says "PC not reachable":** check the iPad is on the same network and the address matches the window.
- **Image shows but pen does nothing:** try Test mode. If that works, the problem is on the PC; look at the log
  in the window for `InjectSyntheticPointerInput failed`.
- **Black image or wrong monitor (H.264/HEVC):** set the monitor again, or use the command line with
  `--output-idx 0` (or 1), or `--capture gdigrab`.
- **"No h264_amf encoder":** update ffmpeg (`winget upgrade Gyan.FFmpeg`), or tick "Encode on the CPU".
- **"cannot decode" on the iPad:** use H.264 instead of HEVC, or JPEG.
- **No pressure in an app:** switch that app's tablet API to Windows Ink.

AV1 is not offered: the iPad's A16 chip has no hardware AV1 decoder.

## Known limits

- No eraser, barrel button, or Pencil double-tap (Safari doesn't expose them).
- Apps that only support the older Wintab API will not see the pen.
- Only the screen is shared, not the mouse cursor, so hover feedback comes from the ring drawn on the iPad.

## Files

- `gui.py`, `PenLink.bat`: the control window and its launcher
- `server.py`: web server, WebSocket, pairing, CLI
- `capture.py`: screen capture, ffmpeg pipeline, demo canvas
- `pen.py`: Windows pen injection
- `auth.py`: PIN pairing
- `usb_server.py`, `usbmux.py`, `PenLink-USB.bat`: the USB version (see `../README-USB.md`)
- `static/index.html`, `static/app.js`: the page that runs on the iPad

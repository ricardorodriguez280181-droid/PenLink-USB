#!/usr/bin/env python3
"""PenLink over USB.

The PenLink app on the iPad listens on a port; this program finds the iPad through usbmuxd
(installed with iTunes / Apple Devices), connects to it through the cable, streams the screen
to it (H.264 from the GPU, or JPEG as a fallback) and turns the Apple Pencil samples it sends
back into a Windows pen.

Wire format, both directions:  [4 bytes big-endian length N][1 byte type][N-1 bytes payload]
  1 HELLO   PC -> iPad   JSON {"fw": width, "fh": height, "codec": "h264" | "jpeg"}
  2 FRAME   PC -> iPad   one JPEG image (jpeg mode)
  3 PEN     iPad -> PC   JSON [[x, y, pressure, tiltX, tiltY, state], ...]
  4 ACK     iPad -> PC   JPEG frame received (frees one slot, see MAX_IN_FLIGHT)
  5 LOG     iPad -> PC   UTF-8 text, shown in this window
  6 VCONFIG PC -> iPad   JSON {"nal": 4, "sps": base64, "pps": base64}   (h264, once per session)
  7 VIDEO   PC -> iPad   1 flag byte (bit 0 = keyframe) + one H.264 frame in AVCC form
                         (each NAL unit preceded by its length, `nal` bytes, big-endian)
"""
import argparse
import asyncio
import base64
import json
import logging
import socket
import struct
from concurrent.futures import ThreadPoolExecutor

import pen
import usbmux
from capture import (DemoSource, ScreenSource, VideoStream, check_ffmpeg,
                     ffmpeg_command)
from server import MAX_IN_FLIGHT, apply_samples, list_monitors

log = logging.getLogger("penlink.usb")

MSG_HELLO, MSG_FRAME, MSG_PEN, MSG_ACK, MSG_LOG, MSG_VCONFIG, MSG_VIDEO = 1, 2, 3, 4, 5, 6, 7
MAX_INBOUND = 1 << 20     # the iPad only sends small messages
STATS_EVERY = 5.0         # seconds between "N fps" log lines


class ProtocolError(Exception):
    pass


def pack(kind, payload=b""):
    return struct.pack(">IB", len(payload) + 1, kind) + payload


async def read_message(reader):
    (length,) = struct.unpack(">I", await reader.readexactly(4))
    if length < 1 or length > MAX_INBOUND:
        raise ProtocolError("bad message length %d" % length)
    body = await reader.readexactly(length)
    return body[0], body[1:]


# ---- H.264 helpers ----------------------------------------------------------------------
def parse_avcc(init):
    """Pull the SPS, PPS and NAL length size out of the MP4 init segment's avcC box."""
    i = init.find(b"avcC")
    if i < 0:
        raise ValueError("no avcC box in the init segment")
    b = init[i + 4:]
    nal_len = (b[4] & 3) + 1
    pos = 6
    sps_list = []
    for _ in range(b[5] & 31):
        n = int.from_bytes(b[pos:pos + 2], "big")
        sps_list.append(bytes(b[pos + 2:pos + 2 + n]))
        pos += 2 + n
    pps_list = []
    for _ in range(b[pos]):
        pos += 1
        n = int.from_bytes(b[pos:pos + 2], "big")
        pps_list.append(bytes(b[pos + 2:pos + 2 + n]))
        pos += 2 + n
    if not sps_list or not pps_list:
        raise ValueError("avcC box has no SPS/PPS")
    return sps_list[0], pps_list[0], nal_len


def split_fragment(frag, nal_len):
    """A fragment is moof + mdat. Return (the mdat contents, is it a keyframe)."""
    pos = 0
    while pos + 8 <= len(frag):
        size = int.from_bytes(frag[pos:pos + 4], "big")
        if size < 8:
            break
        if frag[pos + 4:pos + 8] == b"mdat":
            au = frag[pos + 8:pos + size]
            key, i = False, 0
            while i + nal_len < len(au):
                n = int.from_bytes(au[i:i + nal_len], "big")
                if au[i + nal_len] & 0x1F == 5:   # IDR slice
                    key = True
                    break
                i += nal_len + n
            return au, key
        pos += size
    raise ValueError("fragment without mdat")


def flip_tilt(samples, flip_x, flip_y):
    if not (flip_x or flip_y) or not isinstance(samples, list):
        return samples
    out = []
    for s in samples:
        try:
            s = list(s)
            if flip_x:
                s[3] = -int(s[3])
            if flip_y:
                s[4] = -int(s[4])
        except (TypeError, ValueError, IndexError):
            continue
        out.append(s)
    return out


class Stats:
    """Logs 'N fps, X MB/s' every few seconds."""

    def __init__(self):
        self.frames, self.bytes, self.mark = 0, 0, None

    def add(self, nbytes):
        now = asyncio.get_running_loop().time()
        if self.mark is None:
            self.mark = now
        self.frames += 1
        self.bytes += nbytes
        if now - self.mark >= STATS_EVERY:
            span = now - self.mark
            log.info("%.0f fps, %.1f MB/s", self.frames / span, self.bytes / span / 1e6)
            self.frames, self.bytes, self.mark = 0, 0, now


# ---- senders ------------------------------------------------------------------------------
async def send_frames(app, writer, credits):
    """JPEG mode: send screen frames, never more than MAX_IN_FLIGHT ahead of the iPad."""
    loop = asyncio.get_running_loop()
    source, executor = app["source"], app["executor"]
    interval = 1.0 / app["args"].fps
    force = True              # always send a first frame
    stats = Stats()
    while True:
        await credits.acquire()
        started = loop.time()
        frame = await loop.run_in_executor(executor, source.grab_jpeg, force)
        force = False
        if frame is None:     # screen unchanged: nothing to send, hand the credit back
            credits.release()
        else:
            writer.write(pack(MSG_FRAME, frame))
            await writer.drain()
            stats.add(len(frame))
        await asyncio.sleep(max(0.0, interval - (loop.time() - started)))


async def send_video(writer, stream, nal_len):
    """H.264 mode: forward each frame ffmpeg produces. The picture is encoded on the GPU."""
    stats = Stats()
    try:
        async for frag in stream.fragments():
            au, key = split_fragment(frag, nal_len)
            writer.write(pack(MSG_VIDEO, (b"\x01" if key else b"\x00") + au))
            await writer.drain()
            stats.add(len(au))
    except asyncio.IncompleteReadError:
        log.error("ffmpeg stopped sending video (its message is printed above this line)")
        raise


async def start_video(app):
    """Start ffmpeg. Returns (stream, sps, pps, nal_len), or None if it cannot run (JPEG is used then)."""
    args, source = app["args"], app["source"]
    stream = VideoStream(ffmpeg_command(args, source), source.feeder)
    try:
        init = await asyncio.wait_for(stream.start(), 20)
        sps, pps, nal_len = parse_avcc(init)
        return stream, sps, pps, nal_len
    except (asyncio.IncompleteReadError, asyncio.TimeoutError, ValueError, OSError) as exc:
        log.error("The H.264 encoder did not start (%s). Read the ffmpeg message above. "
                  "Using JPEG instead; try --software or --capture gdigrab for H.264.",
                  type(exc).__name__)
        await stream.stop()
        args.codec = "jpeg"
        return None


async def receive_messages(app, reader, credits):
    args = app["args"]
    while True:
        kind, payload = await read_message(reader)
        if kind == MSG_PEN:
            try:
                samples = json.loads(payload)
            except ValueError:
                continue
            apply_samples(app, flip_tilt(samples, args.flip_tilt_x, args.flip_tilt_y))
        elif kind == MSG_ACK:
            try:
                credits.release()
            except ValueError:
                pass          # already at the maximum
        elif kind == MSG_LOG:
            log.info("[iPad] %s", payload.decode("utf-8", "replace"))


async def run_session(app, reader, writer):
    sock = writer.get_extra_info("socket")
    if sock is not None:
        try:
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError:
            pass

    video = await start_video(app) if app["args"].codec == "h264" else None
    stream = video[0] if video else None
    width, height = app["source"].frame_size
    try:
        hello = {"fw": width, "fh": height, "codec": "h264" if video else "jpeg"}
        writer.write(pack(MSG_HELLO, json.dumps(hello).encode()))
        credits = asyncio.BoundedSemaphore(MAX_IN_FLIGHT)
        if video:
            _, sps, pps, nal_len = video
            config = {"nal": nal_len, "sps": base64.b64encode(sps).decode(),
                      "pps": base64.b64encode(pps).decode()}
            writer.write(pack(MSG_VCONFIG, json.dumps(config).encode()))
            sender = send_video(writer, stream, nal_len)
        else:
            sender = send_frames(app, writer, credits)
        await writer.drain()
        log.info("Streaming %s %dx%d", hello["codec"].upper(), width, height)

        tasks = [asyncio.create_task(sender),
                 asyncio.create_task(receive_messages(app, reader, credits))]
        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                exc = task.exception()
                if exc and not isinstance(exc, (asyncio.IncompleteReadError, ConnectionError,
                                                ProtocolError, OSError)):
                    log.error("session error: %r", exc)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
    except (ConnectionError, OSError):
        pass
    finally:
        if stream is not None:
            await stream.stop()   # waits until ffmpeg is gone
        app["injector"].release()
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass


async def serve(app):
    """Wait for an iPad, connect, run until it disconnects, repeat. Prints each state once."""
    args = app["args"]
    last = None

    def say(text):
        nonlocal last
        if text != last:
            last = text
            log.info(text)

    while True:
        try:
            devices = await usbmux.list_devices(args.usbmuxd)
        except usbmux.UsbmuxError as exc:
            say("Cannot talk to usbmuxd: %s. Install iTunes (apple.com version) or the Apple Devices "
                "app and make sure 'Apple Mobile Device Service' is running." % exc)
            await asyncio.sleep(3)
            continue
        if args.serial:
            devices = [d for d in devices if d.serial == args.serial]
        if not devices:
            say("No iPad found on USB. Plug it in, unlock it and tap Trust if asked.")
            await asyncio.sleep(1.5)
            continue
        device = devices[0]
        try:
            reader, writer = await usbmux.connect(device.id, args.device_port, args.usbmuxd)
        except usbmux.ConnectRefused:
            say("iPad found. Open the PenLink app on it and keep it in the foreground.")
            await asyncio.sleep(1.0)
            continue
        except usbmux.UsbmuxError as exc:
            say("iPad found but connecting failed: %s" % exc)
            await asyncio.sleep(2.0)
            continue
        say("Connected to the iPad (%s)" % (device.serial or device.id))
        await run_session(app, reader, writer)
        last = None
        log.info("iPad disconnected")
        await asyncio.sleep(1.0)


def parse_args(argv=None):
    p = argparse.ArgumentParser(description="Use an iPad (PenLink app) and Apple Pencil as a pen tablet over USB.")
    p.add_argument("--monitor", type=int, default=1, help="monitor to share (default: 1)")
    p.add_argument("--list-monitors", action="store_true", help="print the monitors and exit")
    p.add_argument("--max-width", type=int, default=1920, help="downscale the stream to this width (default: 1920)")
    p.add_argument("--fps", type=int, default=60, help="frames per second (default: 60)")
    p.add_argument("--codec", choices=["h264", "jpeg"], default="h264",
                   help="h264 (default, GPU encoded, much faster) or jpeg (simple fallback)")
    p.add_argument("--bitrate", default="30M", help="h264 bitrate, e.g. 20M or 50M (default: 30M; USB has room)")
    p.add_argument("--quality", type=int, default=80, help="jpeg quality 1-95 (default: 80)")
    p.add_argument("--capture", choices=["ddagrab", "gdigrab"], default="ddagrab",
                   help="ffmpeg screen capture method (default: ddagrab, GPU based)")
    p.add_argument("--output-idx", type=int, default=None,
                   help="ddagrab output number (0-based); default is --monitor minus 1")
    p.add_argument("--software", action="store_true", help="encode on the CPU (libx264) instead of the AMD GPU")
    p.add_argument("--ffmpeg", default="ffmpeg", help="ffmpeg executable (default: found automatically)")
    p.add_argument("--demo", action="store_true",
                   help="test mode: show a drawing canvas instead of your screen and do not inject any input")
    p.add_argument("--device-port", type=int, default=8765, help="port the iPad app listens on (default: 8765)")
    p.add_argument("--serial", help="only use the iPad with this serial number / UDID")
    p.add_argument("--flip-tilt-x", action="store_true", help="invert pen tilt left/right if it feels mirrored")
    p.add_argument("--flip-tilt-y", action="store_true", help="invert pen tilt up/down if it feels mirrored")
    p.add_argument("--usbmuxd", metavar="HOST:PORT", help="override where usbmuxd listens (for testing)")
    args = p.parse_args(argv)
    if args.usbmuxd:
        host, _, port = args.usbmuxd.rpartition(":")
        args.usbmuxd = (host or "127.0.0.1", int(port))
    return args


def build_app(args):
    if args.codec == "h264":
        problem = check_ffmpeg(args)
        if problem:
            log.warning("H.264 is not available: %s. Using JPEG instead.", problem)
            args.codec = "jpeg"
    if args.demo:
        source, injector = DemoSource(args.max_width, args.fps, args.quality), pen.DummyPen()
    else:
        source = ScreenSource(args.monitor, args.max_width, args.quality)
        try:
            injector = pen.create_injector()
        except OSError as exc:
            raise SystemExit("error: %s" % exc)
    # One worker thread: mss needs all capture calls on the thread that opened it.
    executor = ThreadPoolExecutor(max_workers=1)
    try:
        executor.submit(source.open, True).result()   # grab=True, so the JPEG fallback always works
    except ValueError as exc:
        raise SystemExit("error: %s" % exc)
    except ImportError as exc:
        raise SystemExit("error: %s (run: pip install -r requirements.txt)" % exc)
    return {"args": args, "source": source, "injector": injector, "executor": executor}


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
    args = parse_args()
    pen.enable_dpi_awareness()
    if args.list_monitors:
        list_monitors()
        return
    app = build_app(args)
    print()
    print("  PenLink USB is running%s (%s)." % (" (demo mode)" if args.demo else "", app["args"].codec.upper()))
    print("  Plug in the iPad, open the PenLink app on it and leave it in the foreground.")
    print("  Press Ctrl+C to stop.")
    print()
    try:
        asyncio.run(serve(app))
    except KeyboardInterrupt:
        pass
    finally:
        app["executor"].shutdown(wait=False)
        app["injector"].close()


if __name__ == "__main__":
    main()

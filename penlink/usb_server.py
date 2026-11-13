#!/usr/bin/env python3
"""PenLink over USB.

The PenLink app on the iPad listens on a port; this program finds the iPad through usbmuxd
(installed with iTunes / Apple Devices), connects to it through the cable, streams the screen
to it as JPEG frames and turns the Apple Pencil samples it sends back into a Windows pen.

Wire format, both directions:  [4 bytes big-endian length N][1 byte type][N-1 bytes payload]
  1 HELLO  PC -> iPad   JSON {"fw": width, "fh": height}
  2 FRAME  PC -> iPad   one JPEG image
  3 PEN    iPad -> PC   JSON [[x, y, pressure, tiltX, tiltY, state], ...]   (same as the web version)
  4 ACK    iPad -> PC   frame received and decoded (frees one slot, see MAX_IN_FLIGHT)
  5 LOG    iPad -> PC   UTF-8 text, shown in this window
"""
import argparse
import asyncio
import json
import logging
import socket
import struct
from concurrent.futures import ThreadPoolExecutor

import pen
import usbmux
from capture import DemoSource, ScreenSource
from server import MAX_IN_FLIGHT, apply_samples, list_monitors

log = logging.getLogger("penlink.usb")

MSG_HELLO, MSG_FRAME, MSG_PEN, MSG_ACK, MSG_LOG = 1, 2, 3, 4, 5
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


async def send_frames(app, writer, credits):
    """Send screen frames, never more than MAX_IN_FLIGHT ahead of the iPad."""
    loop = asyncio.get_running_loop()
    source, executor = app["source"], app["executor"]
    interval = 1.0 / app["args"].fps
    force = True              # always send a first frame
    frames, sent, mark = 0, 0, loop.time()
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
            frames += 1
            sent += len(frame)
        now = loop.time()
        if now - mark >= STATS_EVERY:
            log.info("%.0f fps, %.1f MB/s", frames / (now - mark), sent / (now - mark) / 1e6)
            frames, sent, mark = 0, 0, now
        await asyncio.sleep(max(0.0, interval - (now - started)))


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
    width, height = app["source"].frame_size
    writer.write(pack(MSG_HELLO, json.dumps({"fw": width, "fh": height}).encode()))
    await writer.drain()

    credits = asyncio.BoundedSemaphore(MAX_IN_FLIGHT)
    tasks = [asyncio.create_task(send_frames(app, writer, credits)),
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
    p.add_argument("--quality", type=int, default=80, help="JPEG quality 1-95 (default: 80; USB has room for more)")
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
        executor.submit(source.open, True).result()
    except ValueError as exc:
        raise SystemExit("error: %s" % exc)
    except ImportError as exc:
        raise SystemExit("error: %s (run: pip install -r requirements.txt)" % exc)
    return {"args": args, "source": source, "injector": injector, "executor": executor}


def main():
    args = parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
    pen.enable_dpi_awareness()
    if args.list_monitors:
        list_monitors()
        return
    app = build_app(args)
    print()
    print("  PenLink USB is running%s." % (" (demo mode)" if args.demo else ""))
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

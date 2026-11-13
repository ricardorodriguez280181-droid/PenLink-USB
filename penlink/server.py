#!/usr/bin/env python3
"""PenLink server.

Streams your screen to a web page on the iPad and turns Apple Pencil input from that
page into real pen input on this PC. Normally started from gui.py; this file also works
from the command line.
"""
import argparse
import asyncio
import json
import logging
import secrets
import socket
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from aiohttp import WSMsgType, web

import auth
import pen
from capture import (DemoSource, ScreenSource, VideoStream, check_ffmpeg,
                     codec_string, ffmpeg_command)

log = logging.getLogger("penlink")

STATIC_DIR = Path(__file__).parent / "static"
MAX_IN_FLIGHT = 2        # JPEG mode: frames sent but not yet acknowledged by the iPad
MAX_SAMPLES = 64         # pen samples accepted per message
MAX_PAIR_FAILS = 5       # wrong PINs allowed before pairing is locked until restart

STATE_HOVER, STATE_CONTACT, STATE_OUT = 0, 1, 2


@web.middleware
async def no_cache(request, handler):
    response = await handler(request)
    response.headers["Cache-Control"] = "no-store"
    return response


async def index(request):
    return web.FileResponse(STATIC_DIR / "index.html")


async def auth_check(request):
    """The page asks this first: is the token it remembers still valid?"""
    if auth.same(request.query.get("token", ""), request.app["token"]):
        return web.Response(text="ok")
    raise web.HTTPForbidden(text="not paired")


async def pair(request):
    """Trade the PIN shown on the PC for a token the iPad remembers."""
    app = request.app
    if app["pair_fails"] >= MAX_PAIR_FAILS:
        raise web.HTTPTooManyRequests(text="pairing locked, restart PenLink")
    try:
        data = await request.json()
    except ValueError:
        raise web.HTTPBadRequest(text="bad request")
    pin = str(data.get("pin", "")).strip() if isinstance(data, dict) else ""
    if auth.same(pin, app["pin"]):
        app["pair_fails"] = 0
        log.info("iPad paired (%s)", request.remote)
        return web.json_response({"token": app["token"]})
    app["pair_fails"] += 1
    log.warning("wrong PIN from %s (%d/%d)", request.remote, app["pair_fails"], MAX_PAIR_FAILS)
    raise web.HTTPForbidden(text="wrong pin")


def apply_samples(app, samples):
    """Map normalised pen samples (0..1 across the shared screen) to pixels and inject them."""
    source, injector = app["source"], app["injector"]
    left, top, width, height = source.geometry
    if not isinstance(samples, list):
        return
    for sample in samples[:MAX_SAMPLES]:
        try:
            x, y, pressure, tilt_x, tilt_y, state = sample
            x = min(max(float(x), 0.0), 1.0)
            y = min(max(float(y), 0.0), 1.0)
            pressure = min(max(float(pressure), 0.0), 1.0)
            tilt_x, tilt_y, state = int(tilt_x), int(tilt_y), int(state)
        except (TypeError, ValueError):
            continue
        px = left + round(x * (width - 1))
        py = top + round(y * (height - 1))
        contact = state == STATE_CONTACT
        injector.update(px, py, state != STATE_OUT, contact, pressure, tilt_x, tilt_y)
        source.on_pen(px - left, py - top, pressure, contact)


async def stream_frames(app, ws, credits):
    """JPEG mode: send screen frames, never more than MAX_IN_FLIGHT ahead of the iPad."""
    loop = asyncio.get_running_loop()
    source, executor = app["source"], app["executor"]
    interval = 1.0 / app["args"].fps
    force = True  # always send a first frame to a new client
    try:
        width, height = source.frame_size
        await ws.send_json({"t": "hello", "mode": "jpeg", "fw": width, "fh": height,
                            "demo": app["args"].demo})
        while not ws.closed:
            await credits.acquire()
            started = loop.time()
            frame = await loop.run_in_executor(executor, source.grab_jpeg, force)
            force = False
            if frame is None:  # screen unchanged: nothing to send, hand the credit back
                credits.release()
            else:
                await ws.send_bytes(frame)
            await asyncio.sleep(max(0.0, interval - (loop.time() - started)))
    except (ConnectionResetError, RuntimeError):
        pass
    except Exception:
        log.exception("frame stream stopped")


async def stream_video(app, ws):
    """H.264/HEVC mode: run ffmpeg for this connection and forward its MP4 fragments.

    Each new connection gets its own ffmpeg, so the stream always starts with a fresh
    init segment and a keyframe.
    """
    args, source = app["args"], app["source"]
    stream = VideoStream(ffmpeg_command(args, source), source.feeder)
    try:
        init = await stream.start()
        mime = 'video/mp4; codecs="%s"' % codec_string(init, args.codec)
        width, height = source.frame_size
        await ws.send_json({"t": "hello", "mode": "mp4", "fw": width, "fh": height,
                            "demo": args.demo, "mime": mime})
        await ws.send_bytes(init)
        async for fragment in stream.fragments():
            await ws.send_bytes(fragment)
        log.error("ffmpeg stopped sending video")
    except asyncio.IncompleteReadError:
        log.error("ffmpeg exited early; its message is printed above this line")
        await ws.send_json({"t": "error", "msg": "encoder failed to start, see the PC window"})
    except (ConnectionResetError, RuntimeError):
        return
    except asyncio.CancelledError:
        raise
    except Exception:
        log.exception("video stream stopped")
    finally:
        await stream.stop()
    await ws.close(code=1011, message=b"encoder stopped")


async def ws_handler(request):
    app = request.app
    if not auth.same(request.query.get("token", ""), app["token"]):
        raise web.HTTPForbidden(text="not paired")

    ws = web.WebSocketResponse(heartbeat=15, compress=False)
    await ws.prepare(request)

    # Only one iPad/tab controls the pen at a time: the newest connection wins.
    previous = app["active"]
    app["active"] = ws
    if previous is not None and not previous.closed:
        await previous.close(code=4000, message=b"replaced by a newer connection")

    log.info("iPad connected from %s", request.remote)
    credits = None
    if app["args"].codec == "jpeg":
        credits = asyncio.BoundedSemaphore(MAX_IN_FLIGHT)
        sender = asyncio.create_task(stream_frames(app, ws, credits))
    else:
        sender = asyncio.create_task(stream_video(app, ws))
    try:
        async for msg in ws:
            if msg.type != WSMsgType.TEXT:
                continue
            try:
                data = json.loads(msg.data)
            except ValueError:
                continue
            if not isinstance(data, dict):
                continue
            kind = data.get("t")
            if kind == "pen":
                apply_samples(app, data.get("s"))
            elif kind == "ack" and credits is not None:
                try:
                    credits.release()
                except ValueError:
                    pass  # already at the maximum
            elif kind == "ping":
                await ws.send_json({"t": "pong", "c": data.get("c")})
    finally:
        sender.cancel()
        await asyncio.gather(sender, return_exceptions=True)  # waits until ffmpeg is gone
        if app["active"] is ws:
            app["active"] = None
            app["injector"].release()
        log.info("iPad disconnected")
    return ws


async def on_cleanup(app):
    app["executor"].shutdown(wait=False)
    app["injector"].close()


def make_app(args, source, injector, executor):
    app = web.Application(middlewares=[no_cache])
    app["args"] = args
    app["pin"] = args.pin
    app["token"] = auth.device_token(auth.load_secret())
    app["pair_fails"] = 0
    app["source"] = source
    app["injector"] = injector
    app["executor"] = executor
    app["active"] = None
    app.router.add_get("/", index)
    app.router.add_get("/auth", auth_check)
    app.router.add_post("/pair", pair)
    app.router.add_get("/ws", ws_handler)
    app.router.add_static("/static", STATIC_DIR)
    app.on_cleanup.append(on_cleanup)
    return app


def lan_ip():
    """Best guess at this PC's address on the local network (no packets are sent)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def list_monitors():
    import mss

    with mss.mss() as sct:
        for i, m in enumerate(sct.monitors):
            note = "all monitors combined" if i == 0 else ("usually your main display" if i == 1 else "")
            print("%d: %dx%d at (%d, %d)  %s" % (i, m["width"], m["height"], m["left"], m["top"], note))


def parse_args(argv=None):
    p = argparse.ArgumentParser(description="Use an iPad and Apple Pencil as a pen tablet for this PC.")
    p.add_argument("--host", default="0.0.0.0", help="address to listen on (default: all interfaces)")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--monitor", type=int, default=1, help="monitor to share; 0 = all monitors (default: 1)")
    p.add_argument("--list-monitors", action="store_true", help="print the monitors and exit")
    p.add_argument("--max-width", type=int, default=1920, help="downscale the stream to this width (default: 1920)")
    p.add_argument("--fps", type=int, default=60, help="frames per second (default: 60)")
    p.add_argument("--codec", choices=["h264", "hevc", "jpeg"], default="h264",
                   help="h264 (default), hevc (smaller at the same quality) or jpeg (simple, lowest lag)")
    p.add_argument("--bitrate", default="12M", help="h264/hevc bitrate, e.g. 8M or 20M (default: 12M)")
    p.add_argument("--quality", type=int, default=70, help="jpeg quality 1-95 (default: 70)")
    p.add_argument("--capture", choices=["ddagrab", "gdigrab"], default="ddagrab",
                   help="ffmpeg screen capture method (default: ddagrab, GPU based)")
    p.add_argument("--output-idx", type=int, default=None,
                   help="ddagrab output number (0-based); default is --monitor minus 1")
    p.add_argument("--software", action="store_true",
                   help="encode on the CPU (libx264/libx265) instead of the AMD GPU")
    p.add_argument("--ffmpeg", default="ffmpeg", help="ffmpeg executable (default: found automatically)")
    p.add_argument("--pin", help="pairing PIN (default: random on every start)")
    p.add_argument("--demo", action="store_true",
                   help="test mode: show a drawing canvas instead of your screen and do not inject any input")
    return p.parse_args(argv)


def main():
    args = parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
    pen.enable_dpi_awareness()

    if args.list_monitors:
        list_monitors()
        return
    args.pin = args.pin or auth.new_pin()

    problem = check_ffmpeg(args)
    if problem:
        raise SystemExit("error: %s" % problem)

    if args.demo:
        source, injector = DemoSource(args.max_width, args.fps, args.quality), pen.DummyPen()
    else:
        source = ScreenSource(args.monitor, args.max_width, args.quality)
        try:
            injector = pen.create_injector()
        except OSError as exc:
            raise SystemExit("error: %s" % exc)

    # One worker thread: in JPEG mode mss needs all capture calls on the thread that opened it.
    executor = ThreadPoolExecutor(max_workers=1)
    try:
        executor.submit(source.open, args.codec == "jpeg").result()
    except ValueError as exc:
        raise SystemExit("error: %s" % exc)
    except ImportError as exc:
        raise SystemExit("error: %s (run: pip install -r requirements.txt)" % exc)

    app = make_app(args, source, injector, executor)

    print()
    print("  PenLink is running%s." % (" (demo mode)" if args.demo else ""))
    print("  On the iPad, open Safari (same Wi-Fi network) and go to:")
    print()
    print("      http://%s:%d" % (lan_ip(), args.port))
    print()
    print("  First time on this iPad, enter the PIN:  %s" % args.pin)
    print("  Press Ctrl+C to stop.")
    print()
    web.run_app(app, host=args.host, port=args.port, print=None, access_log=None)


if __name__ == "__main__":
    main()

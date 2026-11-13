"""Tests the PC side of PenLink USB without an iPad.

Starts a fake usbmuxd and a fake iPad app (same wire format as the real one), runs usb_server.serve()
in demo mode against them, and checks frames, pen input and (for H.264) that the stream really decodes.

    python tools/selftest_usb.py            # JPEG, plus H.264 if ffmpeg is installed
"""
import asyncio
import base64
import json
import logging
import plistlib
import struct
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pen  # noqa: E402
import usb_server  # noqa: E402
from capture import find_ffmpeg  # noqa: E402
from usb_server import (MSG_ACK, MSG_FRAME, MSG_HELLO, MSG_LOG, MSG_PEN, MSG_VCONFIG,  # noqa: E402
                        MSG_VIDEO, pack, read_message)


class RecordingPen(pen.DummyPen):
    def __init__(self):
        super().__init__()
        self.calls = []

    def update(self, x, y, in_range, in_contact, pressure=0.0, tilt_x=0, tilt_y=0):
        self.calls.append((x, y, in_range, in_contact, round(pressure, 2), tilt_x, tilt_y))
        super().update(x, y, in_range, in_contact, pressure, tilt_x, tilt_y)


async def fake_ipad(reader, writer, res):
    """What the Swift app does: read messages, ack JPEG frames, send pen samples."""
    sent_pen = False
    annexb = b""
    try:
        while True:
            kind, payload = await read_message(reader)
            if kind == MSG_HELLO:
                res["hello"] = json.loads(payload)
                writer.write(pack(MSG_LOG, b"hello from fake iPad"))
            elif kind == MSG_VCONFIG:
                cfg = json.loads(payload)
                res["config"] = cfg
                sps, pps = base64.b64decode(cfg["sps"]), base64.b64decode(cfg["pps"])
                annexb += b"\x00\x00\x00\x01" + sps + b"\x00\x00\x00\x01" + pps
                res["sps_ok"] = sps[0] & 0x1F == 7 and pps[0] & 0x1F == 8
            elif kind in (MSG_FRAME, MSG_VIDEO):
                res["frames"] += 1
                if kind == MSG_FRAME:
                    res["jpeg_ok"] &= payload[:2] == b"\xff\xd8"
                    writer.write(pack(MSG_ACK))
                else:
                    key, au, n = payload[0] & 1, payload[1:], res["config"]["nal"]
                    if res["frames"] == 1:
                        res["first_is_key"] = bool(key)
                    i = 0
                    while i + n <= len(au):          # AVCC (length prefixed) -> Annex B
                        size = int.from_bytes(au[i:i + n], "big")
                        if i + n + size > len(au):
                            res["avcc_ok"] = False
                            break
                        annexb += b"\x00\x00\x00\x01" + au[i + n:i + n + size]
                        i += n + size
                if res["frames"] == 3 and not sent_pen:
                    sent_pen = True
                    samples = [[0.5, 0.5, 0.0, 10, -5, 0], [0.5, 0.5, 0.7, 10, -5, 1],
                               [0.6, 0.55, 0.8, 10, -5, 1], [0.6, 0.55, 0.0, 0, 0, 0],
                               [0.6, 0.55, 0.0, 0, 0, 2]]
                    writer.write(pack(MSG_PEN, json.dumps(samples).encode()))
                await writer.drain()
    except (asyncio.IncompleteReadError, ConnectionError):
        pass
    finally:
        res["annexb"] = annexb


async def fake_usbmuxd(reader, writer, ipad_port, state, res):
    """Answers ListDevices and Connect like the real usbmuxd, then tunnels bytes."""
    head = await reader.readexactly(16)
    length, version, request, tag = struct.unpack("<IIII", head)
    assert version == 1 and request == 8
    msg = plistlib.loads(await reader.readexactly(length - 16))

    def reply(payload):
        body = plistlib.dumps(payload)
        writer.write(struct.pack("<IIII", 16 + len(body), 1, 8, tag) + body)

    if msg["MessageType"] == "ListDevices":
        if not state["plugged"]:
            reply({"DeviceList": []})
        else:
            reply({"DeviceList": [
                {"DeviceID": 7, "MessageType": "Attached",
                 "Properties": {"ConnectionType": "USB", "DeviceID": 7, "SerialNumber": "FAKE-UDID"}},
                {"DeviceID": 8, "MessageType": "Attached",
                 "Properties": {"ConnectionType": "Network", "DeviceID": 8, "SerialNumber": "WIFI"}}]})
        await writer.drain()
        writer.close()
    elif msg["MessageType"] == "Connect":
        assert msg["DeviceID"] == 7
        port = ((msg["PortNumber"] & 0xFF) << 8) | (msg["PortNumber"] >> 8)
        assert port == 8765, port
        if not state["app_open"]:
            res["refused_seen"] = True
            reply({"MessageType": "Result", "Number": 3})
            await writer.drain()
            writer.close()
            return
        reply({"MessageType": "Result", "Number": 0})
        await writer.drain()
        up_r, up_w = await asyncio.open_connection("127.0.0.1", ipad_port)

        async def pump(src, dst):
            try:
                while data := await src.read(65536):
                    dst.write(data)
                    await dst.drain()
            except ConnectionError:
                pass
            finally:
                dst.close()

        await asyncio.gather(pump(reader, up_w), pump(up_r, writer))


def decodes(annexb):
    exe = find_ffmpeg()
    proc = subprocess.run([exe, "-v", "error", "-f", "h264", "-i", "pipe:0", "-f", "null", "-"],
                          input=annexb, capture_output=True)
    return proc.returncode == 0 and not proc.stderr.strip(), proc.stderr.decode(errors="replace")[:300]


async def run(codec):
    res = {"hello": None, "frames": 0, "jpeg_ok": True, "refused_seen": False, "avcc_ok": True}
    state = {"plugged": False, "app_open": False}
    ipad = await asyncio.start_server(lambda r, w: fake_ipad(r, w, res), "127.0.0.1", 0)
    ipad_port = ipad.sockets[0].getsockname()[1]
    mux = await asyncio.start_server(lambda r, w: fake_usbmuxd(r, w, ipad_port, state, res), "127.0.0.1", 0)
    mux_port = mux.sockets[0].getsockname()[1]

    argv = ["--demo", "--fps", "30", "--codec", codec, "--usbmuxd", "127.0.0.1:%d" % mux_port]
    if codec == "h264":
        argv.append("--software")
    args = usb_server.parse_args(argv)
    app = usb_server.build_app(args)
    app["injector"] = RecordingPen()
    task = asyncio.create_task(usb_server.serve(app))

    await asyncio.sleep(1.8)          # nothing plugged in yet
    state["plugged"] = True
    await asyncio.sleep(2.0)          # plugged in, app not open: must be refused and retried
    state["app_open"] = True
    await asyncio.sleep(5.0)          # app open: session runs
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    await asyncio.sleep(0.3)
    app["executor"].shutdown(wait=False)

    calls = app["injector"].calls
    checks = {
        "retried while the app was closed": res["refused_seen"],
        "hello with frame size and codec": bool(res["hello"] and res["hello"]["fw"] > 0
                                                and res["hello"]["codec"] == codec),
        "pen contact arrived with pressure and tilt": any(c[3] and c[4] == 0.7 and c[5:] == (10, -5)
                                                          for c in calls),
        "pen was lifted at the end": bool(calls) and not calls[-1][2],
    }
    if codec == "jpeg":
        checks["got JPEG frames"] = res["frames"] > 5 and res["jpeg_ok"]
    else:
        ok, err = decodes(res.get("annexb", b""))
        checks["SPS/PPS sent"] = bool(res.get("sps_ok"))
        checks["got H.264 frames"] = res["frames"] > 20 and res["avcc_ok"]
        checks["first frame is a keyframe"] = bool(res.get("first_is_key"))
        checks["stream decodes in ffmpeg (%s)" % (err or "no errors")] = ok
    print("\n== %s ==  frames received: %d" % (codec.upper(), res["frames"]))
    for name, ok in checks.items():
        print(("PASS  " if ok else "FAIL  ") + name)
    return all(checks.values())


async def main():
    logging.basicConfig(level=logging.WARNING)
    modes = ["jpeg"] + (["h264"] if find_ffmpeg() else [])
    if "h264" not in modes:
        print("(ffmpeg not found: skipping the H.264 test)")
    good = True
    for codec in modes:
        good &= await run(codec)
    sys.exit(0 if good else 1)


asyncio.run(main())

"""Tests the PC side of PenLink USB without an iPad.

Starts a fake usbmuxd and a fake iPad app (same wire format as the real one), runs usb_server.serve()
in demo mode against them, and checks that frames arrive and pen samples come out as pen events.

    python tools/selftest_usb.py
"""
import asyncio
import json
import logging
import plistlib
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import usb_server  # noqa: E402
from usb_server import MSG_ACK, MSG_FRAME, MSG_HELLO, MSG_LOG, MSG_PEN, pack, read_message  # noqa: E402

results = {"hello": None, "frames": 0, "jpeg_ok": True, "refused_seen": False}


async def fake_ipad(reader, writer):
    """What the Swift app does: read messages, ack frames, send pen samples."""
    sent_pen = False
    try:
        while True:
            kind, payload = await read_message(reader)
            if kind == MSG_HELLO:
                results["hello"] = json.loads(payload)
                writer.write(pack(MSG_LOG, b"hello from fake iPad"))
            elif kind == MSG_FRAME:
                results["frames"] += 1
                results["jpeg_ok"] &= payload[:2] == b"\xff\xd8"
                writer.write(pack(MSG_ACK))
                if results["frames"] == 3 and not sent_pen:
                    sent_pen = True
                    samples = [[0.5, 0.5, 0.0, 10, -5, 0], [0.5, 0.5, 0.7, 10, -5, 1],
                               [0.6, 0.55, 0.8, 10, -5, 1], [0.6, 0.55, 0.0, 0, 0, 0],
                               [0.6, 0.55, 0.0, 0, 0, 2]]
                    writer.write(pack(MSG_PEN, json.dumps(samples).encode()))
                await writer.drain()
    except (asyncio.IncompleteReadError, ConnectionError):
        pass


async def fake_usbmuxd(reader, writer, ipad_port, state):
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
            results["refused_seen"] = True
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


async def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
    state = {"plugged": False, "app_open": False}
    ipad = await asyncio.start_server(fake_ipad, "127.0.0.1", 0)
    ipad_port = ipad.sockets[0].getsockname()[1]
    mux = await asyncio.start_server(lambda r, w: fake_usbmuxd(r, w, ipad_port, state), "127.0.0.1", 0)
    mux_port = mux.sockets[0].getsockname()[1]

    args = usb_server.parse_args(["--demo", "--fps", "30", "--usbmuxd", "127.0.0.1:%d" % mux_port])
    app = usb_server.build_app(args)
    task = asyncio.create_task(usb_server.serve(app))

    await asyncio.sleep(1.8)          # nothing plugged in yet
    state["plugged"] = True
    await asyncio.sleep(2.0)          # plugged in, app not open: must be refused and retried
    state["app_open"] = True
    await asyncio.sleep(4.0)          # app open: session runs
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    app["executor"].shutdown(wait=False)

    checks = {
        "retried while app was closed": results["refused_seen"],
        "hello received with frame size": bool(results["hello"] and results["hello"]["fw"] > 0),
        "got JPEG frames": results["frames"] > 5 and results["jpeg_ok"],
    }
    for name, ok in checks.items():
        print(("PASS  " if ok else "FAIL  ") + name)
    print("frames received:", results["frames"], " hello:", results["hello"])
    sys.exit(0 if all(checks.values()) else 1)


asyncio.run(main())

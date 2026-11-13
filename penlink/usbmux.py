"""Minimal usbmuxd client, with no dependencies.

usbmuxd is the service that Apple's iTunes / Apple Devices app installs on Windows. It can open a TCP
connection from this PC to a port that an app on the iPad is listening on, through the USB cable.
(The reverse is not possible, which is why the iPad app is the listener and this PC is the client.)

Protocol: every message is a 16 byte little-endian header (length, version=1, request=8, tag)
followed by an XML/binary plist. After a successful "Connect" the socket becomes a raw tunnel.
Windows: usbmuxd listens on 127.0.0.1:27015. macOS/Linux: unix socket /var/run/usbmuxd.
"""
import asyncio
import plistlib
import struct
import sys
from dataclasses import dataclass

PROG = "PenLink"
WINDOWS_ADDR = ("127.0.0.1", 27015)
UNIX_PATH = "/var/run/usbmuxd"

RESULT_OK = 0
RESULT_DEVICE_GONE = 2
RESULT_REFUSED = 3      # the iPad is there, but nothing listens on that port (app not open)


class UsbmuxError(Exception):
    """usbmuxd is missing or answered with something unexpected."""


class ConnectRefused(UsbmuxError):
    """The iPad is connected, but the PenLink app is not listening (yet)."""


@dataclass
class Device:
    id: int
    serial: str
    product_id: int = 0


async def _open(addr):
    if addr is None:
        if sys.platform == "win32":
            return await asyncio.open_connection(*WINDOWS_ADDR)
        return await asyncio.open_unix_connection(UNIX_PATH)
    if isinstance(addr, str):
        return await asyncio.open_unix_connection(addr)
    return await asyncio.open_connection(*addr)


async def _request(reader, writer, message, tag):
    message = dict(message, ClientVersionString=PROG, ProgName=PROG, kLibUSBMuxVersion=3)
    body = plistlib.dumps(message)
    writer.write(struct.pack("<IIII", 16 + len(body), 1, 8, tag) + body)
    await writer.drain()
    head = await reader.readexactly(16)
    length = struct.unpack("<I", head[:4])[0]
    if length < 16 or length > 1 << 24:
        raise UsbmuxError("unexpected reply from usbmuxd")
    return plistlib.loads(await reader.readexactly(length - 16))


async def list_devices(addr=None):
    """USB-connected devices currently known to usbmuxd (Wi-Fi sync devices are left out)."""
    try:
        reader, writer = await _open(addr)
    except (OSError, asyncio.TimeoutError) as exc:
        raise UsbmuxError("cannot reach usbmuxd (%s)" % exc)
    try:
        reply = await _request(reader, writer, {"MessageType": "ListDevices"}, 1)
    except (asyncio.IncompleteReadError, ConnectionError) as exc:
        raise UsbmuxError("usbmuxd closed the connection (%s)" % exc)
    finally:
        writer.close()
    devices = []
    for entry in reply.get("DeviceList", []):
        props = entry.get("Properties", {})
        if props.get("ConnectionType", "USB") != "USB":
            continue
        devices.append(Device(int(props.get("DeviceID", entry.get("DeviceID", 0))),
                              str(props.get("SerialNumber", "")),
                              int(props.get("ProductID", 0))))
    return devices


async def connect(device_id, port, addr=None):
    """Open a tunnel to a TCP port on the device. Returns (reader, writer)."""
    try:
        reader, writer = await _open(addr)
    except OSError as exc:
        raise UsbmuxError("cannot reach usbmuxd (%s)" % exc)
    try:
        port_be = ((port & 0xFF) << 8) | (port >> 8)   # usbmuxd wants network byte order
        reply = await _request(reader, writer, {"MessageType": "Connect", "DeviceID": device_id,
                                                "PortNumber": port_be}, 2)
    except (asyncio.IncompleteReadError, ConnectionError) as exc:
        writer.close()
        raise UsbmuxError("usbmuxd closed the connection (%s)" % exc)
    except BaseException:
        writer.close()
        raise
    number = reply.get("Number", -1)
    if number == RESULT_OK:
        return reader, writer
    writer.close()
    if number == RESULT_REFUSED:
        raise ConnectRefused("connection refused by the iPad")
    raise UsbmuxError("usbmuxd could not connect (result %s)" % number)

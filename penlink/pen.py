"""Pen input injection for PenLink.

On Windows this uses Microsoft's documented Pointer Injection API
(CreateSyntheticPointerDevice / InjectSyntheticPointerInput, Windows 10 1809+),
so apps such as Krita, Photoshop or Clip Studio see a real pen with pressure and tilt.

On other systems DummyPen only logs what would have been injected, which is
handy for testing the iPad side of things.
"""
import ctypes
import logging
import sys

log = logging.getLogger("penlink.pen")

IS_WINDOWS = sys.platform == "win32"

# ---- constants from winuser.h -------------------------------------------------
PT_PEN = 3

POINTER_FEEDBACK_DEFAULT = 1
POINTER_FEEDBACK_INDIRECT = 2
POINTER_FEEDBACK_NONE = 3

POINTER_FLAG_NEW = 0x00000001
POINTER_FLAG_INRANGE = 0x00000002
POINTER_FLAG_INCONTACT = 0x00000004
POINTER_FLAG_FIRSTBUTTON = 0x00000010
POINTER_FLAG_DOWN = 0x00010000
POINTER_FLAG_UPDATE = 0x00020000
POINTER_FLAG_UP = 0x00040000

PEN_MASK_PRESSURE = 0x00000001
PEN_MASK_ROTATION = 0x00000002
PEN_MASK_TILT_X = 0x00000004
PEN_MASK_TILT_Y = 0x00000008

MAX_PRESSURE = 1024


# ---- structures ---------------------------------------------------------------
# Fixed-width types are used instead of wintypes so the layout is identical on
# every 64-bit platform (c_ulong is 8 bytes on Linux but 4 on Windows).
class POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_int32), ("y", ctypes.c_int32)]


class RECT(ctypes.Structure):
    _fields_ = [
        ("left", ctypes.c_int32),
        ("top", ctypes.c_int32),
        ("right", ctypes.c_int32),
        ("bottom", ctypes.c_int32),
    ]


class POINTER_INFO(ctypes.Structure):
    _fields_ = [
        ("pointerType", ctypes.c_uint32),
        ("pointerId", ctypes.c_uint32),
        ("frameId", ctypes.c_uint32),
        ("pointerFlags", ctypes.c_uint32),
        ("sourceDevice", ctypes.c_void_p),
        ("hwndTarget", ctypes.c_void_p),
        ("ptPixelLocation", POINT),
        ("ptHimetricLocation", POINT),
        ("ptPixelLocationRaw", POINT),
        ("ptHimetricLocationRaw", POINT),
        ("dwTime", ctypes.c_uint32),
        ("historyCount", ctypes.c_uint32),
        ("InputData", ctypes.c_int32),
        ("dwKeyStates", ctypes.c_uint32),
        ("PerformanceCount", ctypes.c_uint64),
        ("ButtonChangeType", ctypes.c_int32),
    ]


class POINTER_PEN_INFO(ctypes.Structure):
    _fields_ = [
        ("pointerInfo", POINTER_INFO),
        ("penFlags", ctypes.c_uint32),
        ("penMask", ctypes.c_uint32),
        ("pressure", ctypes.c_uint32),
        ("rotation", ctypes.c_uint32),
        ("tiltX", ctypes.c_int32),
        ("tiltY", ctypes.c_int32),
    ]


class POINTER_TOUCH_INFO(ctypes.Structure):
    # Never used here, but it is the larger union member, so it must be declared
    # for POINTER_TYPE_INFO to have the size Windows expects.
    _fields_ = [
        ("pointerInfo", POINTER_INFO),
        ("touchFlags", ctypes.c_uint32),
        ("touchMask", ctypes.c_uint32),
        ("rcContact", RECT),
        ("rcContactRaw", RECT),
        ("orientation", ctypes.c_uint32),
        ("pressure", ctypes.c_uint32),
    ]


class _INFO_UNION(ctypes.Union):
    _fields_ = [("touchInfo", POINTER_TOUCH_INFO), ("penInfo", POINTER_PEN_INFO)]


class POINTER_TYPE_INFO(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", ctypes.c_uint32), ("u", _INFO_UNION)]


def _clamp(value, low, high):
    return max(low, min(high, value))


# ---- injectors ----------------------------------------------------------------
class PenInjector:
    """Injects pen input as a real Windows pen device."""

    name = "Windows pointer injection"

    def __init__(self):
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        try:
            create = user32.CreateSyntheticPointerDevice
        except AttributeError:
            raise OSError(
                "CreateSyntheticPointerDevice is missing: Windows 10 version 1809 or newer is required"
            )
        create.argtypes = [ctypes.c_int, ctypes.c_uint32, ctypes.c_int]
        create.restype = ctypes.c_void_p

        self._inject = user32.InjectSyntheticPointerInput
        self._inject.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(POINTER_TYPE_INFO),
            ctypes.c_uint32,
        ]
        self._inject.restype = ctypes.c_int

        self._destroy = user32.DestroySyntheticPointerDevice
        self._destroy.argtypes = [ctypes.c_void_p]
        self._destroy.restype = None

        self._device = create(PT_PEN, 1, POINTER_FEEDBACK_DEFAULT)
        if not self._device:
            raise OSError(
                "CreateSyntheticPointerDevice failed (error %d)" % ctypes.get_last_error()
            )

        self._in_range = False
        self._in_contact = False
        self._last_xy = (0, 0)
        self._last_error = None

    def update(self, x, y, in_range, in_contact, pressure=0.0, tilt_x=0, tilt_y=0):
        """Send one pen sample. x/y are screen pixels, pressure is 0..1, tilt is degrees."""
        in_range = in_range or in_contact
        if not in_range and not self._in_range:
            return  # still out of range, nothing to tell Windows

        flags = 0
        if in_range:
            flags |= POINTER_FLAG_INRANGE
            if not self._in_range:
                flags |= POINTER_FLAG_NEW
        if in_contact:
            flags |= POINTER_FLAG_INCONTACT | POINTER_FLAG_FIRSTBUTTON
            flags |= POINTER_FLAG_UPDATE if self._in_contact else POINTER_FLAG_DOWN
        else:
            flags |= POINTER_FLAG_UP if self._in_contact else POINTER_FLAG_UPDATE

        entry = POINTER_TYPE_INFO()
        entry.type = PT_PEN
        pen = entry.penInfo
        pen.pointerInfo.pointerType = PT_PEN
        pen.pointerInfo.pointerId = 0
        pen.pointerInfo.pointerFlags = flags
        pen.pointerInfo.ptPixelLocation.x = int(x)
        pen.pointerInfo.ptPixelLocation.y = int(y)
        pen.penMask = PEN_MASK_PRESSURE | PEN_MASK_TILT_X | PEN_MASK_TILT_Y
        if in_contact:
            pen.pressure = _clamp(int(max(pressure, 0.01) * MAX_PRESSURE), 1, MAX_PRESSURE)
        else:
            pen.pressure = 0
        pen.tiltX = _clamp(int(tilt_x), -90, 90)
        pen.tiltY = _clamp(int(tilt_y), -90, 90)

        if self._inject(self._device, ctypes.byref(entry), 1):
            self._in_range = in_range
            self._in_contact = in_contact
            self._last_xy = (int(x), int(y))
            self._last_error = None
        else:
            # State is left unchanged so the next sample retries the same transition.
            code = ctypes.get_last_error()
            if code != self._last_error:
                self._last_error = code
                log.error(
                    "InjectSyntheticPointerInput failed: %s. If the target app runs as "
                    "administrator, start PenLink as administrator too.",
                    ctypes.WinError(code),
                )

    def release(self):
        """Lift the pen and take it out of range (e.g. when the iPad disconnects)."""
        if self._in_range:
            self.update(*self._last_xy, in_range=False, in_contact=False)

    def close(self):
        self.release()
        if self._device:
            self._destroy(self._device)
            self._device = None


class DummyPen:
    """Stand-in for non-Windows systems and demo mode: logs state changes only."""

    name = "dummy (log only)"

    def __init__(self):
        self._in_range = False
        self._in_contact = False

    def update(self, x, y, in_range, in_contact, pressure=0.0, tilt_x=0, tilt_y=0):
        in_range = in_range or in_contact
        if in_range and not self._in_range:
            log.info("pen in range")
        if in_contact and not self._in_contact:
            log.info(
                "pen down at (%d, %d) pressure=%.2f tilt=(%d, %d)",
                x, y, pressure, tilt_x, tilt_y,
            )
        if not in_contact and self._in_contact:
            log.info("pen up at (%d, %d)", x, y)
        if not in_range and self._in_range:
            log.info("pen out of range")
        self._in_range = in_range
        self._in_contact = in_contact

    def release(self):
        self.update(0, 0, in_range=False, in_contact=False)

    def close(self):
        pass


def enable_dpi_awareness():
    """Make capture and pen coordinates use real pixels on scaled (125%/150%) displays."""
    if not IS_WINDOWS:
        return
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # per-monitor aware
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def create_injector():
    if IS_WINDOWS:
        return PenInjector()
    log.warning("Not running on Windows: pen events will only be logged, not injected.")
    return DummyPen()

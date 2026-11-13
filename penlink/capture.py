"""Screen sources and the ffmpeg video pipeline for PenLink.

The screen is encoded to H.264 (or HEVC) by ffmpeg, on the GPU through AMD's AMF encoder,
and sent to the iPad as fragmented MP4, which Safari plays with Media Source Extensions.
"""
import asyncio
import io
import logging
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path

from PIL import Image, ImageDraw

log = logging.getLogger("penlink.capture")


def _even(n):
    return max(2, n - n % 2)


def fit_size(width, height, max_width):
    """Scale (width, height) down to at most max_width wide, keeping the aspect ratio.
    Both numbers are made even, because H.264 and HEVC need that."""
    scale = min(1.0, max_width / width)
    return _even(round(width * scale)), _even(round(height * scale))


def to_jpeg(img, size, quality):
    if img.size != size:
        img = img.resize(size, Image.Resampling.BILINEAR)
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=quality)
    return buf.getvalue()


# ---- sources ---------------------------------------------------------------------------
class ScreenSource:
    """A real monitor. For H.264/HEVC mss only learns where the monitor is and ffmpeg captures;
    in JPEG mode (open(grab=True)) mss grabs the frames itself.

    mss keeps per-thread handles on Windows, so in JPEG mode open() and grab_jpeg() must run
    on the same thread (the server uses a one-thread executor for exactly that).
    """

    feeder = None

    def __init__(self, monitor_index, max_width, quality=70):
        self.monitor_index = monitor_index
        self._max_width = max_width
        self._quality = quality
        self._sct = None
        self._region = None
        self._previous = None
        self.geometry = None    # (left, top, width, height) in physical screen pixels
        self.frame_size = None  # size of the video sent to the iPad

    def open(self, grab=False):
        import mss  # imported here so --demo works without mss installed

        sct = mss.mss()
        monitors = sct.monitors
        if not 0 <= self.monitor_index < len(monitors):
            sct.close()
            raise ValueError(
                "monitor %d does not exist; valid values are 0-%d (see --list-monitors)"
                % (self.monitor_index, len(monitors) - 1)
            )
        m = monitors[self.monitor_index]
        self.geometry = (m["left"], m["top"], m["width"], m["height"])
        self.frame_size = fit_size(m["width"], m["height"], self._max_width)
        if grab:
            self._sct = sct
            self._region = {k: m[k] for k in ("left", "top", "width", "height")}
        else:
            sct.close()

    def grab_jpeg(self, force=False):
        """Return a JPEG of the screen, or None if nothing changed since the last frame."""
        shot = self._sct.grab(self._region)
        if not force and shot.raw == self._previous:
            return None
        self._previous = shot.raw
        img = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
        return to_jpeg(img, self.frame_size, self._quality)

    def on_pen(self, x, y, pressure, contact):
        pass


class DemoSource:
    """A test pattern with 'ink'. Lets you test the iPad side without screen capture."""

    WIDTH, HEIGHT = 1280, 720
    monitor_index = 1

    def __init__(self, max_width, fps, quality=70):
        self.geometry = (0, 0, self.WIDTH, self.HEIGHT)
        self.frame_size = fit_size(self.WIDTH, self.HEIGHT, max_width)
        self.fps = min(fps, 30)  # raw frames are piped through Python, so keep this modest
        self._quality = quality
        self._lock = threading.Lock()
        self._last = None
        self._canvas = Image.new("RGB", (self.WIDTH, self.HEIGHT), (250, 249, 245))
        draw = ImageDraw.Draw(self._canvas)
        for x in range(0, self.WIDTH, 80):
            draw.line([(x, 0), (x, self.HEIGHT)], fill=(228, 226, 220))
        for y in range(0, self.HEIGHT, 80):
            draw.line([(0, y), (self.WIDTH, y)], fill=(228, 226, 220))

    def open(self, grab=False):
        pass

    def _frame(self):
        with self._lock:
            img = self._canvas.copy()
        ImageDraw.Draw(img).text(
            (16, 12), time.strftime("PenLink demo  %H:%M:%S"), fill=(110, 110, 110)
        )
        return img

    def _raw_frame(self):
        return self._frame().tobytes()

    def grab_jpeg(self, force=False):
        return to_jpeg(self._frame(), self.frame_size, self._quality)

    async def feeder(self, stdin):
        """Pipe raw RGB frames into ffmpeg at a steady rate."""
        loop = asyncio.get_running_loop()
        interval = 1.0 / self.fps
        due = loop.time()
        try:
            while True:
                stdin.write(await loop.run_in_executor(None, self._raw_frame))
                await stdin.drain()
                due += interval
                await asyncio.sleep(max(0.0, due - loop.time()))
        except (BrokenPipeError, ConnectionResetError):
            pass  # ffmpeg went away; the reader side reports it

    def on_pen(self, x, y, pressure, contact):
        """Draw ink where the pen touches, with a width that follows pressure."""
        with self._lock:
            if not contact:
                self._last = None
                return
            radius = 0.5 + pressure * 7
            draw = ImageDraw.Draw(self._canvas)
            if self._last is not None:
                draw.line([self._last, (x, y)], fill=(25, 25, 25), width=max(1, round(radius * 2)))
            draw.ellipse([x - radius, y - radius, x + radius, y + radius], fill=(25, 25, 25))
            self._last = (x, y)


# ---- ffmpeg ----------------------------------------------------------------------------
def encoder_name(args):
    if args.software:
        return {"h264": "libx264", "hevc": "libx265"}[args.codec]
    return {"h264": "h264_amf", "hevc": "hevc_amf"}[args.codec]


def find_ffmpeg(explicit=None):
    """Find ffmpeg: an explicit path, PATH, or where winget puts it (so no PATH fiddling is needed)."""
    if explicit and explicit != "ffmpeg":
        return explicit if Path(explicit).is_file() else shutil.which(explicit)
    found = shutil.which("ffmpeg")
    if found:
        return found
    local = os.environ.get("LOCALAPPDATA")
    if local:
        winget = Path(local) / "Microsoft" / "WinGet"
        link = winget / "Links" / "ffmpeg.exe"
        if link.is_file():
            return str(link)
        for path in (winget / "Packages").glob("Gyan.FFmpeg*/**/ffmpeg.exe"):
            return str(path)
    for path in (r"C:\ffmpeg\bin\ffmpeg.exe", r"C:\Program Files\ffmpeg\bin\ffmpeg.exe"):
        if Path(path).is_file():
            return path
    return None


def check_ffmpeg(args):
    """Return a problem description, or None if this ffmpeg can do the job."""
    if args.codec == "jpeg":
        return None  # JPEG mode does not use ffmpeg
    exe = find_ffmpeg(args.ffmpeg)
    if not exe:
        return ("ffmpeg was not found. Install it with: winget install Gyan.FFmpeg "
                "(or choose the JPEG mode, which does not need it)")
    args.ffmpeg = exe
    encoder = encoder_name(args)
    out = subprocess.run([exe, "-hide_banner", "-encoders"], capture_output=True, text=True).stdout
    if encoder not in out:
        hint = "update ffmpeg to a recent full build" if not args.software else "use a full ffmpeg build"
        return "this ffmpeg has no %s encoder: %s, or try --software" % (encoder, hint)
    if not args.demo and args.capture == "ddagrab":
        if args.monitor == 0 and args.output_idx is None:
            return "ddagrab captures one monitor at a time: use --monitor 1, 2, ... or --capture gdigrab"
        filters = subprocess.run([exe, "-hide_banner", "-filters"], capture_output=True, text=True).stdout
        if "ddagrab" not in filters:
            return "this ffmpeg has no ddagrab filter (needs ffmpeg 5.0+). Update it, or use --capture gdigrab"
    return None


def ffmpeg_command(args, source):
    w, h = source.frame_size
    gw, gh = source.geometry[2], source.geometry[3]
    cmd = [args.ffmpeg, "-hide_banner", "-loglevel", "warning", "-nostdin"]
    pixfmt = "yuv420p" if args.software else "nv12"
    scale = "scale=%d:%d:flags=fast_bilinear" % (w, h)

    if args.demo:
        cmd += ["-f", "rawvideo", "-pix_fmt", "rgb24", "-s", "%dx%d" % (gw, gh),
                "-framerate", str(source.fps), "-i", "pipe:0"]
        vf = "%s,format=%s" % (scale, pixfmt)
    elif args.capture == "ddagrab":
        idx = args.output_idx if args.output_idx is not None else max(0, source.monitor_index - 1)
        cmd += ["-f", "lavfi", "-i",
                "ddagrab=output_idx=%d:framerate=%d:draw_mouse=0" % (idx, args.fps)]
        vf = "hwdownload,format=bgra,%s,format=%s" % (scale, pixfmt)
    else:  # gdigrab
        left, top = source.geometry[0], source.geometry[1]
        cmd += ["-f", "gdigrab", "-framerate", str(args.fps), "-draw_mouse", "0",
                "-offset_x", str(left), "-offset_y", str(top),
                "-video_size", "%dx%d" % (gw, gh), "-i", "desktop"]
        vf = "%s,format=%s" % (scale, pixfmt)

    cmd += ["-vf", vf, "-c:v", encoder_name(args), "-g", str(args.fps * 2)]
    if args.software:
        cmd += ["-preset", "ultrafast", "-tune", "zerolatency", "-b:v", args.bitrate]
        if args.codec == "h264":
            cmd += ["-bf", "0"]
        else:
            cmd += ["-x265-params", "log-level=error"]
    else:
        cmd += ["-usage", "ultralowlatency", "-quality", "speed", "-rc", "cbr", "-b:v", args.bitrate]
    if args.codec == "hevc":
        cmd += ["-tag:v", "hvc1"]  # Safari only accepts HEVC in MP4 under this tag
    cmd += ["-an", "-f", "mp4", "-movflags", "empty_moov+default_base_moof+frag_every_frame",
            "-flush_packets", "1", "pipe:1"]
    return cmd


def codec_string(init, codec):
    """Build the MIME codec string Safari wants from the init segment's avcC / hvcC box."""
    if codec == "h264":
        i = init.find(b"avcC")
        if i < 0:
            raise ValueError("no avcC box in the init segment")
        return "avc1.%02X%02X%02X" % (init[i + 5], init[i + 6], init[i + 7])
    i = init.find(b"hvcC")
    if i < 0:
        raise ValueError("no hvcC box in the init segment")
    b = init[i + 4:i + 17]
    space, tier, profile = b[1] >> 6, (b[1] >> 5) & 1, b[1] & 31
    compat = int("{:032b}".format(int.from_bytes(b[2:6], "big"))[::-1], 2)
    text = "hvc1.%s%d.%X.%s%d" % ("ABC"[space - 1] if space else "", profile, compat,
                                  "H" if tier else "L", b[12])
    constraints = list(b[6:12])
    while constraints and constraints[-1] == 0:
        constraints.pop()
    return text + "".join(".%X" % c for c in constraints)


class VideoStream:
    """One ffmpeg process. start() returns the MP4 init segment, fragments() the rest."""

    def __init__(self, cmd, feeder=None):
        self._cmd = cmd
        self._feeder = feeder
        self._proc = None
        self._feed_task = None

    async def start(self):
        self._proc = await asyncio.create_subprocess_exec(
            *self._cmd,
            stdin=asyncio.subprocess.PIPE if self._feeder else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
        )
        if self._feeder:
            self._feed_task = asyncio.create_task(self._feeder(self._proc.stdin))
        init = b""
        while True:  # ftyp + moov
            box = await self._read_box()
            init += box
            if box[4:8] == b"moov":
                return init

    async def _read_box(self):
        head = await self._proc.stdout.readexactly(8)
        size = int.from_bytes(head[:4], "big")
        if size < 8:
            raise ValueError("unsupported MP4 box from ffmpeg")
        return head + await self._proc.stdout.readexactly(size - 8)

    async def fragments(self):
        """Yield each moof+mdat pair (one video frame) as one blob."""
        frag = b""
        while True:
            box = await self._read_box()
            frag += box
            if box[4:8] == b"mdat":
                yield frag
                frag = b""

    async def stop(self):
        if self._feed_task:
            self._feed_task.cancel()
            await asyncio.gather(self._feed_task, return_exceptions=True)
        if self._proc is not None:
            if self._proc.returncode is None:
                try:
                    self._proc.kill()
                except ProcessLookupError:
                    pass
            await self._proc.wait()

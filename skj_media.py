#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
SKJ Media — one file, every device.

An ffmpeg + yt-dlp front-end that picks its own interface:

    PySide6 GUI  ->  Tkinter GUI  ->  plain-text CLI

Runs on Windows, Linux, macOS, Android (Termux) and iOS (a-Shell).
On iOS and Android it goes straight to text mode, because there is no
usable desktop toolkit there.

What it does
------------
  * convert with presets (H.264, HEVC, AV1, VP9, ProRes, DNxHR, GIF, audio)
  * job queue + batch convert (several files, whole folders, globs)
  * hardware encoding: NVENC, Quick Sync, VAAPI, VideoToolbox, AMF
  * "make it fit": two-pass encode to a target file size (e.g. 25M)
  * filters: crop (incl. 16:9 / 9:16 / square), rotate, flip, speed,
    denoise, deinterlace, fades, loudness normalisation, volume,
    burned-in subtitles, image watermark, audio track pick
  * tools: inspect, thumbnail, contact sheet, waveform, loudness meter,
    join, split, subtitle extraction
  * downloads: info preview, up to 4K, clip one section, playlist ranges,
    SponsorBlock, cookies from your browser, rate limit, skip-already-got
    archive, split by chapters, several links at once
  * live speed + ETA, profiles, history, watch folder, notifications

Usage
-----
    python skj_media.py                    auto-pick the interface
    python skj_media.py --cli              force text mode
    python skj_media.py --tk               force Tkinter
    python skj_media.py --qt               force PySide6
    python skj_media.py --doctor           check ffmpeg / yt-dlp / GPU / toolkits

One-shot, no menus:
    python skj_media.py --convert in.mkv out.mp4 --preset mp4 --height 1080
    python skj_media.py --convert in.mov clip.mp4 --start 1:00 --end 1:30 --target-size 10M
    python skj_media.py --batch ~/Videos/raw --preset hevc --hw auto --outdir ~/Videos/out
    python skj_media.py --download "https://youtu.be/..." --format audio-mp3
    python skj_media.py --thumbnail in.mp4 --at 0:42
    python skj_media.py --watch ~/Inbox --preset mp4-small --outdir ~/Done

Put ffmpeg / ffprobe next to this script (or in ./bin) for a portable setup,
or point SKJ_FFMPEG / SKJ_FFPROBE / SKJ_YTDLP at them.

SKJ Tech — skj-tech.online
"""

from __future__ import annotations

import argparse
import collections
import glob
import itertools
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time

APP_NAME = "SKJ Media"
APP_VERSION = "2.0.0"

# --------------------------------------------------------------------------
# platform
# --------------------------------------------------------------------------


def detect_platform() -> str:
    """Return one of: ios, android, windows, macos, linux."""
    if "a-Shell" in os.environ.get("TERM_PROGRAM", "") or sys.platform == "ios":
        return "ios"
    # a-Shell sets these; also its executable lives inside an app bundle
    if os.environ.get("APPDIR", "").endswith(".app") and sys.platform == "darwin":
        if not shutil.which("sw_vers"):
            return "ios"
    if "com.apple.CoreSimulator" in sys.executable:
        return "ios"
    if "/var/mobile" in os.path.expanduser("~") or "/private/var/mobile" in sys.prefix:
        return "ios"
    if "ANDROID_ROOT" in os.environ or "TERMUX_VERSION" in os.environ:
        return "android"
    if sys.platform.startswith("win"):
        return "windows"
    if sys.platform == "darwin":
        return "macos"
    return "linux"


PLATFORM = detect_platform()
IS_MOBILE = PLATFORM in ("ios", "android")

# a-Shell's Python has no working subprocess: commands must go through
# os.system(), which routes into ios_system.
NO_SUBPROCESS = PLATFORM == "ios"

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

INSTALL_HINTS = {
    "ios": {
        "ffmpeg": "a-Shell ships ffmpeg already. Type `ffmpeg -version` to confirm.",
        "yt-dlp": "pip install yt-dlp",
    },
    "android": {
        "ffmpeg": "pkg install ffmpeg",
        "yt-dlp": "pip install yt-dlp   (or: pkg install yt-dlp)",
    },
    "linux": {
        "ffmpeg": "sudo dnf install ffmpeg     # or apt install ffmpeg",
        "yt-dlp": "pip install --user yt-dlp",
    },
    "macos": {"ffmpeg": "brew install ffmpeg", "yt-dlp": "brew install yt-dlp"},
    "windows": {
        "ffmpeg": "winget install Gyan.FFmpeg",
        "yt-dlp": "winget install yt-dlp.yt-dlp   (or: pip install yt-dlp)",
    },
}


def find_binary(name: str, env_var: str) -> list[str] | None:
    """Locate a helper binary as an argv prefix.

    Order: env override, next to this script (portable installs), PATH,
    then a-Shell built-ins / the yt_dlp module.
    """
    override = os.environ.get(env_var)
    if override and os.path.isfile(override):
        return [override]
    exe = name + (".exe" if PLATFORM == "windows" else "")
    for folder in (SCRIPT_DIR, os.path.join(SCRIPT_DIR, "bin")):
        candidate = os.path.join(folder, exe)
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return [candidate]
    hit = shutil.which(name)
    if hit:
        return [hit]
    if NO_SUBPROCESS and name in ("ffmpeg", "ffprobe"):
        # ios_system built-ins are not on PATH but are callable by name.
        return [name]
    # yt-dlp is often only importable, not on PATH
    if name == "yt-dlp":
        try:
            import yt_dlp  # noqa: F401

            return [sys.executable, "-m", "yt_dlp"]
        except Exception:
            return None
    return None


BIN = {
    "ffmpeg": find_binary("ffmpeg", "SKJ_FFMPEG"),
    "ffprobe": find_binary("ffprobe", "SKJ_FFPROBE"),
    "yt-dlp": find_binary("yt-dlp", "SKJ_YTDLP"),
}
# ffprobe usually sits next to ffmpeg, even when only ffmpeg was pointed at
if BIN["ffmpeg"] and not BIN["ffprobe"] and os.path.isabs(BIN["ffmpeg"][0]):
    _probe = os.path.join(os.path.dirname(BIN["ffmpeg"][0]),
                          "ffprobe" + (".exe" if PLATFORM == "windows" else ""))
    if os.path.isfile(_probe):
        BIN["ffprobe"] = [_probe]


def bin_label(name: str) -> str | None:
    return " ".join(BIN[name]) if BIN.get(name) else None


def install_hint(tool: str) -> str:
    return INSTALL_HINTS.get(PLATFORM, {}).get(tool, f"install {tool} and put it on PATH")


def quote_cmd(cmd) -> str:
    if PLATFORM == "windows":
        return subprocess.list2cmdline(cmd)
    return " ".join(shlex.quote(c) for c in cmd)


def popen_kwargs() -> dict:
    # keep a console window from flashing up for every job on Windows
    if PLATFORM == "windows":
        return {"creationflags": 0x08000000}  # CREATE_NO_WINDOW
    return {}


def capture(cmd, timeout=60) -> tuple[int, str, str]:
    """Run a short helper command and return (code, stdout, stderr)."""
    if NO_SUBPROCESS:
        out_fd, out_path = tempfile.mkstemp(prefix="skjmedia-", suffix=".out")
        err_fd, err_path = tempfile.mkstemp(prefix="skjmedia-", suffix=".err")
        os.close(out_fd)
        os.close(err_fd)
        try:
            code = os.system(f"{quote_cmd(cmd)} > {shlex.quote(out_path)} "
                             f"2> {shlex.quote(err_path)}")
            with open(out_path, "r", errors="replace") as fh:
                out = fh.read()
            with open(err_path, "r", errors="replace") as fh:
                err = fh.read()
            return code, out, err
        finally:
            for path in (out_path, err_path):
                try:
                    os.remove(path)
                except OSError:
                    pass
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                             errors="replace", timeout=timeout,
                             stdin=subprocess.DEVNULL, **popen_kwargs())
        return res.returncode, res.stdout or "", res.stderr or ""
    except (OSError, subprocess.TimeoutExpired) as exc:
        return -1, "", str(exc)


# --------------------------------------------------------------------------
# settings, profiles, history
# --------------------------------------------------------------------------


def config_dir() -> str:
    if os.environ.get("SKJ_MEDIA_HOME"):
        return os.environ["SKJ_MEDIA_HOME"]
    if PLATFORM == "windows":
        return os.path.join(os.environ.get("APPDATA") or os.path.expanduser("~"), "SKJ Media")
    if PLATFORM == "macos":
        return os.path.expanduser("~/Library/Application Support/SKJ Media")
    if PLATFORM == "ios":
        return os.path.expanduser("~/Documents/.skj-media")
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(base, "skj-media")


CONFIG_DIR = config_dir()

DEFAULT_SETTINGS = {
    "hw": "none",            # none / auto / nvenc / qsv / vaapi / videotoolbox / amf
    "outdir": "",            # blank = next to the source file
    "download_dir": "",      # blank = current folder
    "suffix": "_skj",
    "overwrite": False,
    "notify": True,
    "profiles": {},          # name -> {"preset": ..., option: value, ...}
    "presets": {},           # user presets: name -> {"label", "ext", "args", "kind"}
    "last_convert": {},
    "last_download": {},
}


class Settings:
    def __init__(self):
        self.path = os.path.join(CONFIG_DIR, "settings.json")
        self.data = json.loads(json.dumps(DEFAULT_SETTINGS))
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                self.data.update(json.load(fh))
        except (OSError, ValueError):
            pass

    def get(self, key, default=None):
        val = self.data.get(key)
        return default if val is None else val

    def set(self, key, value):
        self.data[key] = value
        self.save()

    def save(self):
        try:
            os.makedirs(CONFIG_DIR, exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(self.data, fh, indent=2, ensure_ascii=False)
            os.replace(tmp, self.path)
        except OSError:
            pass


SETTINGS = Settings()
HISTORY_PATH = os.path.join(CONFIG_DIR, "history.jsonl")


def record_history(job) -> None:
    entry = {"t": time.time(), "kind": job.kind, "title": job.title,
             "target": job.target, "status": job.status,
             "took": round(job.took or 0.0, 1), "summary": job.summary}
    try:
        os.makedirs(CONFIG_DIR, exist_ok=True)
        with open(HISTORY_PATH, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        pass


def load_history(limit=200) -> list[dict]:
    try:
        with open(HISTORY_PATH, "r", encoding="utf-8") as fh:
            lines = fh.readlines()
    except OSError:
        return []
    if len(lines) > 2000:  # keep the file from growing forever
        lines = lines[-1000:]
        try:
            with open(HISTORY_PATH, "w", encoding="utf-8") as fh:
                fh.writelines(lines)
        except OSError:
            pass
    out = []
    for line in lines[-limit:]:
        try:
            out.append(json.loads(line))
        except ValueError:
            pass
    return list(reversed(out))


def clear_history() -> None:
    try:
        os.remove(HISTORY_PATH)
    except OSError:
        pass


def notify(title: str, message: str) -> None:
    """Best-effort desktop / phone notification."""
    if not SETTINGS.get("notify", True) or NO_SUBPROCESS:
        return
    try:
        if PLATFORM == "macos":
            script = f"display notification {json.dumps(message)} with title {json.dumps(title)}"
            subprocess.Popen(["osascript", "-e", script],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        elif PLATFORM == "linux" and shutil.which("notify-send"):
            subprocess.Popen(["notify-send", "-a", APP_NAME, title, message],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        elif PLATFORM == "android" and shutil.which("termux-notification"):
            subprocess.Popen(["termux-notification", "--title", title, "--content", message],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass


def open_folder(path: str) -> None:
    folder = path if os.path.isdir(path) else os.path.dirname(path)
    if not folder or not os.path.isdir(folder):
        return
    try:
        if PLATFORM == "windows":
            os.startfile(folder)  # noqa: S606 — opens Explorer
        elif PLATFORM == "macos":
            subprocess.Popen(["open", folder])
        else:
            subprocess.Popen(["xdg-open", folder], stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
    except Exception:
        pass


# --------------------------------------------------------------------------
# small parsers / formatters
# --------------------------------------------------------------------------

_HMS_RE = re.compile(r"^(?:(\d+(?:\.\d+)?)h)?(?:(\d+(?:\.\d+)?)m)?(?:(\d+(?:\.\d+)?)s)?$")


def parse_timecode(value) -> float | None:
    """'90', '1:30', '00:01:30.5', '1m30s' -> seconds. None if unparseable."""
    if value is None:
        return None
    text = str(value).strip().lower()
    if not text:
        return None
    m = _HMS_RE.match(text)
    if m and any(m.groups()):
        h, mn, s = (float(g) if g else 0.0 for g in m.groups())
        return h * 3600 + mn * 60 + s
    try:
        total = 0.0
        for part in text.split(":"):
            total = total * 60 + float(part)
        return total if total >= 0 else None
    except ValueError:
        return None


def fmt_ts(seconds: float) -> str:
    """seconds -> HH:MM:SS.mmm (what ffmpeg likes best)."""
    seconds = max(0.0, seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{int(h):02d}:{int(m):02d}:{s:06.3f}"


def fmt_dur(seconds) -> str:
    """seconds -> 1:05 or 1:02:03, for ETAs and durations."""
    if seconds is None:
        return "?"
    seconds = int(round(max(0.0, seconds)))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def fmt_bytes(n) -> str:
    try:
        n = float(n)
    except (TypeError, ValueError):
        return "?"
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def parse_size(value) -> int | None:
    """'25M', '25 MB', '8MiB', '700k', '1.5G' -> bytes (1024-based). Bare number = MB."""
    m = re.match(r"^\s*(\d+(?:\.\d+)?)\s*([kmg]?)i?b?\s*$", str(value or ""), re.I)
    if not m:
        return None
    num, unit = float(m.group(1)), (m.group(2) or "m").lower()
    return int(num * {"k": 1024, "m": 1024 ** 2, "g": 1024 ** 3}[unit])


def parse_kbps(value) -> int | None:
    """'192k' -> 192, '5M' -> 5000, '320' -> 320, '2500000' -> 2500."""
    m = re.match(r"^\s*(\d+(?:\.\d+)?)\s*([km]?)(?:bps|b|bit/s)?\s*$", str(value or ""), re.I)
    if not m:
        return None
    num, unit = float(m.group(1)), m.group(2).lower()
    if unit == "m":
        return int(num * 1000)
    if unit == "k":
        return int(num)
    return int(num / 1000) if num > 20000 else int(num)


def _float(value, what: str, lo=None, hi=None) -> float | None:
    if value in (None, ""):
        return None
    try:
        num = float(str(value).strip().lower().replace("db", "").replace(",", "."))
    except ValueError:
        raise RuntimeError(f"{what}: expected a number, got {value!r}")
    if (lo is not None and num < lo) or (hi is not None and num > hi):
        raise RuntimeError(f"{what} must be between {lo:g} and {hi:g}")
    return num


def _int(value, what: str, lo=None, hi=None) -> int | None:
    num = _float(value, what, lo, hi)
    return None if num is None else int(num)


def _time(value, what: str) -> float | None:
    if value in (None, ""):
        return None
    secs = parse_timecode(value)
    if secs is None:
        raise RuntimeError(f"{what}: can't read {value!r} as a time (try 1:30 or 00:01:30)")
    return secs


def clean_opts(opts: dict) -> dict:
    out = {}
    for k, v in (opts or {}).items():
        if isinstance(v, str):
            v = v.strip()
        if v in (None, "", False):
            continue
        out[k] = v
    return out


# --------------------------------------------------------------------------
# encoders + hardware acceleration
# --------------------------------------------------------------------------

_ENCODERS: set[str] | None = None


def ffmpeg_encoders() -> set[str]:
    """Names of the encoders this ffmpeg build was compiled with."""
    global _ENCODERS
    if _ENCODERS is None:
        _ENCODERS = set()
        if BIN["ffmpeg"]:
            _code, out, _err = capture(BIN["ffmpeg"] + ["-hide_banner", "-encoders"], 30)
            for line in out.splitlines():
                parts = line.split()
                if len(parts) >= 2 and len(parts[0]) == 6 and parts[0][0] in "VAS":
                    _ENCODERS.add(parts[1])
    return _ENCODERS


HW_BACKENDS = {
    "nvenc": {"label": "NVIDIA NVENC", "h264": "h264_nvenc", "hevc": "hevc_nvenc",
              "av1": "av1_nvenc"},
    "qsv": {"label": "Intel Quick Sync", "h264": "h264_qsv", "hevc": "hevc_qsv",
            "av1": "av1_qsv"},
    "vaapi": {"label": "VAAPI (Linux AMD/Intel)", "h264": "h264_vaapi",
              "hevc": "hevc_vaapi", "av1": "av1_vaapi"},
    "videotoolbox": {"label": "Apple VideoToolbox", "h264": "h264_videotoolbox",
                     "hevc": "hevc_videotoolbox"},
    "amf": {"label": "AMD AMF", "h264": "h264_amf", "hevc": "hevc_amf", "av1": "av1_amf"},
}
HW_ORDER = {
    "macos": ["videotoolbox"],
    "windows": ["nvenc", "qsv", "amf"],
    "linux": ["nvenc", "vaapi", "qsv"],
}
VAAPI_DEVICE = os.environ.get("SKJ_VAAPI_DEVICE", "/dev/dri/renderD128")

_HW_OK: dict[str, bool] = {}


def hw_works(backend: str) -> bool:
    """Actually try a tiny encode — 'compiled in' does not mean 'GPU present'."""
    if backend in _HW_OK:
        return _HW_OK[backend]
    ok = False
    enc = HW_BACKENDS.get(backend, {}).get("h264")
    if BIN["ffmpeg"] and enc and enc in ffmpeg_encoders() and not NO_SUBPROCESS:
        pre = ["-vaapi_device", VAAPI_DEVICE] if backend == "vaapi" else []
        vf = ["-vf", "format=nv12,hwupload"] if backend == "vaapi" else []
        cmd = (BIN["ffmpeg"] + ["-hide_banner", "-loglevel", "error"] + pre
               + ["-f", "lavfi", "-i", "color=black:s=256x256:d=0.2"] + vf
               + ["-c:v", enc, "-frames:v", "3", "-f", "null", "-"])
        ok = capture(cmd, 20)[0] == 0
    _HW_OK[backend] = ok
    return ok


def hw_choices() -> list[tuple[str, str]]:
    return ([("none", "Software (CPU) — best quality per MB"),
             ("auto", "Auto-detect GPU encoder")]
            + [(k, v["label"]) for k, v in HW_BACKENDS.items()])


def resolve_hw(choice) -> tuple[str | None, str | None]:
    """-> (backend or None, warning or None)."""
    choice = (choice or "none").lower()
    if choice in ("none", "off", "cpu", "software"):
        return None, None
    if choice == "auto":
        for backend in HW_ORDER.get(PLATFORM, []):
            if hw_works(backend):
                return backend, None
        return None, "no working GPU encoder found — using the CPU"
    if choice not in HW_BACKENDS:
        return None, f"unknown hardware encoder {choice!r} — using the CPU"
    if not hw_works(choice):
        return None, f"{HW_BACKENDS[choice]['label']} isn't usable here — using the CPU"
    return choice, None


# --------------------------------------------------------------------------
# presets
# --------------------------------------------------------------------------

FASTSTART = ["-movflags", "+faststart"]

VIDEO_PRESETS = {
    "mp4": {"label": "MP4 / H.264 + AAC (plays everywhere)", "ext": "mp4",
            "family": "h264", "crf": 20, "speed": "medium", "pix": "yuv420p",
            "acodec": "aac", "ab": "192k", "extra": FASTSTART},
    "mp4-small": {"label": "MP4 / H.264, smaller file (CRF 26)", "ext": "mp4",
                  "family": "h264", "crf": 26, "speed": "slow", "pix": "yuv420p",
                  "acodec": "aac", "ab": "128k", "extra": FASTSTART},
    "hevc": {"label": "MP4 / H.265 (half the size, fussier playback)", "ext": "mp4",
             "family": "hevc", "crf": 24, "speed": "medium", "pix": "yuv420p",
             "acodec": "aac", "ab": "160k", "extra": FASTSTART},
    "av1": {"label": "MP4 / AV1 (smallest, needs a newer device)", "ext": "mp4",
            "family": "av1", "crf": 32, "pix": "yuv420p",
            "acodec": "aac", "ab": "160k", "extra": FASTSTART},
    "webm": {"label": "WebM / VP9 + Opus", "ext": "webm", "family": "vp9", "crf": 32,
             "acodec": "libopus", "ab": "128k"},
    "dnxhr": {"label": "MOV / DNxHR HQ — editing intermediate for DaVinci Resolve",
              "ext": "mov", "vargs": ["-c:v", "dnxhd", "-profile:v", "dnxhr_hq",
                                      "-pix_fmt", "yuv422p"], "acodec": "pcm_s16le"},
    "prores": {"label": "MOV / ProRes 422 — editing intermediate", "ext": "mov",
               "vargs": ["-c:v", "prores_ks", "-profile:v", "2",
                         "-pix_fmt", "yuv422p10le"], "acodec": "pcm_s16le"},
    "proxy": {"label": "MOV / DNxHR LB — lightweight proxy", "ext": "mov",
              "vargs": ["-c:v", "dnxhd", "-profile:v", "dnxhr_lb", "-pix_fmt", "yuv422p"],
              "acodec": "pcm_s16le"},
    "copy": {"label": "Remux only — no re-encode, instant", "ext": None, "copy": True},
    "gif": {"label": "Animated GIF (palette-optimised)", "ext": "gif", "gif": True},
}

AUDIO_PRESETS = {
    "mp3": {"label": "MP3 320 kbps", "ext": "mp3", "acodec": "libmp3lame", "ab": "320k"},
    "mp3-small": {"label": "MP3 128 kbps", "ext": "mp3", "acodec": "libmp3lame", "ab": "128k"},
    "m4a": {"label": "M4A / AAC 256 kbps", "ext": "m4a", "acodec": "aac", "ab": "256k"},
    "opus": {"label": "Opus 128 kbps", "ext": "opus", "acodec": "libopus", "ab": "128k"},
    "ogg": {"label": "Ogg Vorbis 192 kbps", "ext": "ogg", "acodec": "libvorbis", "ab": "192k"},
    "wav": {"label": "WAV 16-bit PCM", "ext": "wav", "acodec": "pcm_s16le"},
    "flac": {"label": "FLAC lossless", "ext": "flac", "acodec": "flac"},
    "alac": {"label": "ALAC lossless (Apple)", "ext": "m4a", "acodec": "alac"},
}
for _p in VIDEO_PRESETS.values():
    _p["kind"] = "video"
for _p in AUDIO_PRESETS.values():
    _p["kind"] = "audio"

LOSSLESS_AUDIO = {"pcm_s16le", "pcm_s24le", "flac", "alac"}

ALL_PRESETS = {**VIDEO_PRESETS, **AUDIO_PRESETS}

# user presets from settings.json: {"name": {"label": "...", "ext": "mkv",
#   "kind": "video", "args": ["-c:v", "libx264", ...]}}
for _name, _p in (SETTINGS.get("presets") or {}).items():
    if isinstance(_p, dict) and isinstance(_p.get("args"), list):
        ALL_PRESETS[_name] = {"label": _p.get("label") or f"custom: {_name}",
                              "ext": _p.get("ext") or "mkv",
                              "kind": _p.get("kind") or "video",
                              "args": [str(a) for a in _p["args"]]}


def preset_ext(key: str, src: str = "") -> str:
    p = ALL_PRESETS.get(key, {})
    if p.get("ext"):
        return p["ext"]
    if p.get("copy"):
        return os.path.splitext(src)[1].lstrip(".") or "mkv"
    return key


DOWNLOAD_FORMATS = {
    "best": {"label": "Best available video+audio",
             "args": ["-f", "bv*+ba/b", "--merge-output-format", "mp4"]},
    "2160p": {"label": "Up to 4K",
              "args": ["-f", "bv*[height<=2160]+ba/b[height<=2160]",
                       "--merge-output-format", "mp4"]},
    "1440p": {"label": "Up to 1440p",
              "args": ["-f", "bv*[height<=1440]+ba/b[height<=1440]",
                       "--merge-output-format", "mp4"]},
    "1080p": {"label": "Up to 1080p",
              "args": ["-f", "bv*[height<=1080]+ba/b[height<=1080]",
                       "--merge-output-format", "mp4"]},
    "720p": {"label": "Up to 720p",
             "args": ["-f", "bv*[height<=720]+ba/b[height<=720]",
                      "--merge-output-format", "mp4"]},
    "480p": {"label": "Up to 480p (slow connection)",
             "args": ["-f", "bv*[height<=480]+ba/b[height<=480]",
                      "--merge-output-format", "mp4"]},
    "compat": {"label": "H.264 MP4 for old phones / TVs / editors",
               "args": ["-f", "bv*[vcodec^=avc1]+ba[ext=m4a]/b[ext=mp4]/b",
                        "--merge-output-format", "mp4"]},
    "video-only": {"label": "Video only, no sound",
                   "args": ["-f", "bv*/b", "--merge-output-format", "mp4"]},
    "audio-mp3": {"label": "Audio only -> MP3",
                  "args": ["-f", "ba/b", "-x", "--audio-format", "mp3",
                           "--audio-quality", "0"]},
    "audio-m4a": {"label": "Audio only -> M4A",
                  "args": ["-f", "ba[ext=m4a]/ba/b", "-x", "--audio-format", "m4a"]},
    "audio-opus": {"label": "Audio only -> Opus (no re-encode on YouTube)",
                   "args": ["-f", "ba[acodec=opus]/ba/b", "-x", "--audio-format", "opus"]},
    "audio-flac": {"label": "Audio only -> FLAC",
                   "args": ["-f", "ba/b", "-x", "--audio-format", "flac"]},
}

MEDIA_EXTS = {
    ".mp4", ".m4v", ".mkv", ".mov", ".avi", ".webm", ".mpg", ".mpeg", ".ts", ".mts",
    ".m2ts", ".wmv", ".flv", ".3gp", ".mxf", ".gif", ".mp3", ".m4a", ".aac", ".wav",
    ".flac", ".ogg", ".opus", ".wma", ".aif", ".aiff",
}


# --------------------------------------------------------------------------
# option fields — one description drives the CLI prompts and both GUIs
# --------------------------------------------------------------------------


class Field:
    """kind: text | choice | check | file | dir | save"""

    def __init__(self, key, label, kind="text", choices=None, ph="", tip="",
                 basic=False, video=False, default=None):
        self.key, self.label, self.kind = key, label, kind
        self.choices = choices or []
        self.ph, self.tip, self.basic, self.video, self.default = ph, tip, basic, video, default

    def options(self) -> list[tuple[str, str]]:
        return [c if isinstance(c, tuple) else (c, c or "—") for c in self.choices]


CONVERT_FIELDS = [
    Field("height", "Height", ph="keep", basic=True, video=True,
          tip="Scale to this many pixels tall, e.g. 1080 or 720. Width follows."),
    Field("start", "Start", ph="00:00:10", basic=True, tip="Trim: where the clip starts"),
    Field("end", "End", ph="00:01:30", basic=True, tip="Trim: where the clip ends"),
    Field("crf", "Quality (CRF)", ph="preset default", basic=True, video=True,
          tip="Lower = better and bigger. 18–28 is sensible for H.264."),
    Field("target_size", "Fit in size", ph="e.g. 25M", basic=True, video=True,
          tip="Two-pass encode that lands just under this size. Discord: 10M; email: 20M."),
    Field("fps", "Frame rate", ph="keep", video=True, tip="e.g. 30 or 60"),
    Field("vbitrate", "Video bitrate", ph="e.g. 6M", video=True,
          tip="Fixed average bitrate instead of CRF quality"),
    Field("abitrate", "Audio bitrate", ph="preset default", tip="e.g. 128k, 192k, 320k"),
    Field("speed", "Speed ×", ph="1.0", tip="0.5 = slow motion, 2 = double speed (audio too)"),
    Field("volume", "Volume dB", ph="0", tip="+6 is about twice as loud, -6 half"),
    Field("fade_in", "Fade in (s)", ph="0", tip="Fade picture and sound in"),
    Field("fade_out", "Fade out (s)", ph="0", tip="Fade picture and sound out at the end"),
    Field("crop", "Crop", ph="16:9, 9:16, square or W:H:X:Y", video=True,
          tip="A ratio crops the centre (9:16 = vertical for TikTok/Shorts)."),
    Field("rotate", "Rotate", "choice", ["", "90", "180", "270"], video=True,
          tip="Clockwise degrees"),
    Field("flip", "Flip", "choice", [("", "—"), ("h", "horizontal"), ("v", "vertical"),
                                     ("hv", "both")], video=True),
    Field("audio_track", "Audio track #", ph="0", tip="Which audio stream to keep (0 = first)"),
    Field("burn_subs", "Burn subtitles", "file", ph=".srt / .ass / .vtt", video=True,
          tip="Draw a subtitle file permanently onto the picture"),
    Field("watermark", "Watermark", "file", ph="logo.png", video=True,
          tip="Overlay an image (PNG with transparency works best)"),
    Field("watermark_pos", "Watermark at", "choice",
          [("br", "bottom right"), ("bl", "bottom left"), ("tr", "top right"),
           ("tl", "top left"), ("center", "centre")], video=True, default="br"),
    Field("extra", "Extra ffmpeg args", ph="-tune film", tip="Added just before the output"),
    Field("denoise", "Denoise", "check", video=True, tip="Light hqdn3d noise reduction"),
    Field("deinterlace", "Deinterlace", "check", video=True, tip="For old TV / camcorder footage"),
    Field("normalize", "Normalise loudness", "check", tip="EBU R128 to -16 LUFS"),
    Field("mute", "Remove audio", "check", video=True),
    Field("strip_metadata", "Strip metadata", "check",
          tip="Drop tags, GPS, camera info and chapters"),
]

DOWNLOAD_FIELDS = [
    Field("outdir", "Folder", "dir", ph="current folder"),
    Field("template", "File name", ph="%(title).80s.%(ext)s",
          tip="yt-dlp output template, e.g. %(uploader)s - %(title)s.%(ext)s"),
    Field("section", "Only this part", ph="1:00-2:30", tip="Download just a section"),
    Field("items", "Playlist items", ph="1-10,15", tip="Which playlist entries (needs Whole playlist)"),
    Field("sub_langs", "Subtitle languages", ph="en,pl"),
    Field("limit_rate", "Speed limit", ph="e.g. 2M", tip="Bytes per second, e.g. 500K or 2M"),
    Field("fragments", "Parallel fragments", ph="1", tip="4 can be much faster on HLS/DASH"),
    Field("cookies_browser", "Cookies from", "choice",
          ["", "chrome", "firefox", "edge", "brave", "safari", "opera", "vivaldi", "chromium"],
          tip="Use your browser login — for age-gated or members-only videos"),
    Field("extra", "Extra yt-dlp args", ph="--no-mtime"),
    Field("playlist", "Whole playlist", "check"),
    Field("subs", "Subtitles", "check"),
    Field("thumbnail", "Embed thumbnail", "check"),
    Field("metadata", "Embed metadata", "check", default=True),
    Field("sponsorblock", "Cut sponsor segments", "check"),
    Field("split_chapters", "Split by chapters", "check"),
    Field("archive", "Skip already downloaded", "check",
          tip="Remembers what you got, so re-running a playlist only fetches new ones"),
]

TOOLS = {
    "inspect": {"label": "Inspect — every stream, codec and tag", "fields": []},
    "thumbnail": {"label": "Thumbnail — grab one frame as JPG", "fields": [
        Field("at", "Time", ph="auto (10% in)"), Field("width", "Width", ph="original")]},
    "sheet": {"label": "Contact sheet — grid of frames", "fields": [
        Field("cols", "Columns", ph="4"), Field("rows", "Rows", ph="4"),
        Field("width", "Sheet width", ph="1600")]},
    "waveform": {"label": "Waveform — picture of the audio", "fields": [
        Field("width", "Width", ph="1600"), Field("height", "Height", ph="300"),
        Field("color", "Colour", ph="#ff6a00")]},
    "loudness": {"label": "Loudness meter — LUFS, true peak, range", "fields": []},
    "join": {"label": "Join — glue files end to end", "multi": True, "fields": [
        Field("mode", "Mode", "choice", [("copy", "fast (same format files)"),
                                        ("mp4", "re-encode to MP4")], default="copy"),
        Field("output", "Output file", "save", ph="joined.mp4 next to the first file")]},
    "split": {"label": "Split — cut into equal chunks (no re-encode)", "fields": [
        Field("every", "Chunk length", ph="10:00")]},
    "subs": {"label": "Extract subtitles to .srt", "fields": [
        Field("track", "Subtitle track #", ph="0")]},
}


# --------------------------------------------------------------------------
# probing
# --------------------------------------------------------------------------

_PROBE_CACHE: dict = {}


def probe_json(path: str) -> dict:
    if not BIN["ffprobe"] or not path:
        return {}
    try:
        key = (os.path.abspath(path), os.path.getmtime(path))
    except OSError:
        return {}
    if key in _PROBE_CACHE:
        return _PROBE_CACHE[key]
    code, out, _err = capture(BIN["ffprobe"] + [
        "-v", "quiet", "-print_format", "json", "-show_format", "-show_streams",
        "-show_chapters", path], 30)
    try:
        data = json.loads(out) if code == 0 and out.strip() else {}
    except ValueError:
        data = {}
    _PROBE_CACHE[key] = data
    return data


def probe_duration(path: str) -> float | None:
    """Seconds of media, or None. Used to turn ffmpeg's time into a percent."""
    try:
        return float(probe_json(path).get("format", {}).get("duration"))
    except (TypeError, ValueError):
        return None


def _rate(text) -> float | None:
    try:
        num, _, den = str(text).partition("/")
        val = float(num) / float(den or 1)
        return val if val > 0 else None
    except (ValueError, ZeroDivisionError):
        return None


def probe_summary(path: str) -> str:
    d = probe_json(path)
    if not d:
        return ""
    bits = []
    for s in d.get("streams", []):
        if s.get("codec_type") == "video" and s.get("disposition", {}).get("attached_pic"):
            continue
        if s.get("codec_type") == "video":
            fps = _rate(s.get("avg_frame_rate")) or _rate(s.get("r_frame_rate"))
            bits.append(f"{s.get('codec_name')} {s.get('width')}x{s.get('height')}"
                        + (f" {fps:.3g}fps" if fps else ""))
        elif s.get("codec_type") == "audio":
            bits.append(f"{s.get('codec_name')} {s.get('channels')}ch")
    fmt = d.get("format", {})
    if fmt.get("duration"):
        bits.append(fmt_dur(float(fmt["duration"])))
    if fmt.get("size"):
        bits.append(fmt_bytes(int(fmt["size"])))
    return " | ".join(bits)


def probe_details(path: str) -> str:
    d = probe_json(path)
    if not d:
        return f"could not read {path}" + ("" if BIN["ffprobe"] else " (ffprobe missing)")
    fmt = d.get("format", {})
    lines = [f"File      {path}"]
    lines.append(f"Container {fmt.get('format_long_name') or fmt.get('format_name')}")
    if fmt.get("duration"):
        lines.append(f"Duration  {fmt_dur(float(fmt['duration']))} ({float(fmt['duration']):.2f}s)")
    if fmt.get("size"):
        lines.append(f"Size      {fmt_bytes(int(fmt['size']))}")
    if fmt.get("bit_rate"):
        lines.append(f"Bitrate   {int(fmt['bit_rate']) // 1000} kb/s")
    for k, v in (fmt.get("tags") or {}).items():
        if k.lower() in ("title", "artist", "album", "date", "creation_time", "encoder",
                         "comment", "com.apple.quicktime.model", "location"):
            lines.append(f"Tag       {k} = {v}")
    for s in d.get("streams", []):
        kind = s.get("codec_type", "?")
        tags = s.get("tags") or {}
        head_ = f"#{s.get('index')} {kind:<9} {s.get('codec_name', '?')}"
        if s.get("profile"):
            head_ += f" ({s['profile']})"
        extra = []
        if kind == "video":
            extra.append(f"{s.get('width')}x{s.get('height')}")
            fps = _rate(s.get("avg_frame_rate")) or _rate(s.get("r_frame_rate"))
            if fps:
                extra.append(f"{fps:.3f} fps")
            if s.get("pix_fmt"):
                extra.append(s["pix_fmt"])
            transfer = s.get("color_transfer", "")
            if transfer in ("smpte2084", "arib-std-b67"):
                extra.append("HDR " + ("PQ" if transfer == "smpte2084" else "HLG"))
            if s.get("field_order") not in (None, "progressive", "unknown"):
                extra.append("interlaced")
            rot = tags.get("rotate")
            for side in s.get("side_data_list", []) or []:
                if "rotation" in side:
                    rot = side["rotation"]
            if rot:
                extra.append(f"rotated {rot}°")
            if s.get("disposition", {}).get("attached_pic"):
                extra.append("cover art")
        elif kind == "audio":
            extra.append(f"{s.get('channels')}ch {s.get('channel_layout', '')}".strip())
            if s.get("sample_rate"):
                extra.append(f"{int(s['sample_rate']) / 1000:g} kHz")
        if s.get("bit_rate"):
            extra.append(f"{int(s['bit_rate']) // 1000} kb/s")
        if tags.get("language"):
            extra.append(f"lang={tags['language']}")
        if tags.get("title"):
            extra.append(f"\"{tags['title']}\"")
        lines.append(f"{head_:<34} {' · '.join(x for x in extra if x)}")
    chapters = d.get("chapters") or []
    if chapters:
        lines.append(f"Chapters  {len(chapters)}")
        for ch in chapters[:12]:
            lines.append(f"  {fmt_dur(float(ch.get('start_time', 0))):>8}  "
                         f"{(ch.get('tags') or {}).get('title', '')}")
        if len(chapters) > 12:
            lines.append(f"  … and {len(chapters) - 12} more")
    return "\n".join(lines)


def media_has(path: str, kind: str) -> bool:
    return any(s.get("codec_type") == kind for s in probe_json(path).get("streams", []))


# --------------------------------------------------------------------------
# jobs
# --------------------------------------------------------------------------


class Step:
    def __init__(self, cmd, total=None, label="", cwd=None):
        self.cmd, self.total, self.label, self.cwd = cmd, total, label, cwd


class Job:
    _ids = itertools.count(1)

    def __init__(self, kind, title, steps, target="", summarize=None, cleanup=(),
                 warnings=()):
        self.id = next(Job._ids)
        self.kind, self.title, self.steps, self.target = kind, title, steps, target
        self.summarize = summarize
        self.cleanup_paths = list(cleanup)
        self.warnings = list(warnings)
        self.status = "queued"       # queued / running / done / failed / cancelled
        self.code = None
        self.took = None
        self.summary = ""
        self.lines = collections.deque(maxlen=3000)
        self.progress = None

    def cleanup(self):
        for path in self.cleanup_paths:
            try:
                if os.path.isdir(path):
                    shutil.rmtree(path, ignore_errors=True)
                elif os.path.exists(path):
                    os.remove(path)
            except OSError:
                pass

    def tail(self, n=8) -> list[str]:
        lines = [ln for ln in self.lines if not ln.startswith("$ ")]
        return lines[-n:]


def ffmpeg_head(loglevel="warning") -> list[str]:
    if not BIN["ffmpeg"]:
        raise RuntimeError(f"ffmpeg not found. {install_hint('ffmpeg')}")
    return BIN["ffmpeg"] + ["-hide_banner", "-nostdin", "-y", "-loglevel", loglevel,
                            "-progress", "pipe:1", "-nostats"]


_RESERVED: set[str] = set()


def unique_path(path: str, reserve=True) -> str:
    """Never clobber an existing file (or one another queued job will write)."""
    base, ext = os.path.splitext(path)
    candidate, n = path, 2
    while (os.path.exists(candidate) or candidate in _RESERVED) \
            and not SETTINGS.get("overwrite"):
        candidate = f"{base} ({n}){ext}"
        n += 1
    if reserve:
        _RESERVED.add(candidate)
    return candidate


def output_path(src: str, preset: str, outdir=None, pattern=None, ext=None,
                reserve=True) -> str:
    name = os.path.splitext(os.path.basename(src))[0]
    pattern = pattern or ("{name}" + SETTINGS.get("suffix", "_skj"))
    base = (pattern.replace("{name}", name).replace("{preset}", preset)
            .replace("{date}", time.strftime("%Y-%m-%d")))
    folder = outdir or SETTINGS.get("outdir") or os.path.dirname(os.path.abspath(src))
    folder = os.path.abspath(os.path.expanduser(folder))
    path = os.path.join(folder, f"{base}.{ext or preset_ext(preset, src)}")
    return unique_path(path, reserve)


def expand_inputs(items, recursive=False) -> list[str]:
    """Files, folders and globs -> sorted list of media files."""
    out = []
    for item in items:
        item = os.path.expanduser(item.strip().strip('"').strip("'"))
        if not item:
            continue
        if os.path.isdir(item):
            walker = os.walk(item) if recursive else [(item, [], os.listdir(item))]
            for folder, _dirs, files in walker:
                for f in sorted(files):
                    if os.path.splitext(f)[1].lower() in MEDIA_EXTS and not f.startswith("."):
                        out.append(os.path.join(folder, f))
        elif any(ch in item for ch in "*?["):
            out.extend(sorted(p for p in glob.glob(item, recursive=recursive)
                              if os.path.isfile(p)))
        else:
            out.append(item)
    seen, uniq = set(), []
    for p in out:
        ap = os.path.abspath(p)
        if ap not in seen:
            seen.add(ap)
            uniq.append(ap)
    return uniq


# --------------------------------------------------------------------------
# convert
# --------------------------------------------------------------------------

WATERMARK_POS = {"br": "W-w-24:H-h-24", "bl": "24:H-h-24", "tr": "W-w-24:24",
                 "tl": "24:24", "center": "(W-w)/2:(H-h)/2"}
VIDEO_ONLY_KEYS = [f.key for f in CONVERT_FIELDS if f.video]


def crop_filter(value: str) -> str:
    v = value.strip().lower()
    v = {"square": "1:1", "vertical": "9:16", "portrait": "9:16", "tiktok": "9:16",
         "wide": "16:9", "cinema": "21:9"}.get(v, v)
    parts = v.split(":")
    if not all(p.isdigit() for p in parts) or len(parts) not in (2, 4):
        raise RuntimeError("crop: use a ratio like 16:9 / 9:16 / square, W:H or W:H:X:Y")
    nums = [int(p) for p in parts]
    if len(nums) == 2 and nums[0] <= 64 and nums[1] <= 64:
        a, b = nums  # a ratio -> biggest centred box with that shape, even sizes
        return (f"crop=w='floor(min(iw,ih*{a}/{b})/2)*2'"
                f":h='floor(min(ih,iw*{b}/{a})/2)*2'")
    return "crop=" + ":".join(parts)


def atempo_chain(speed: float) -> list[str]:
    # atempo only takes 0.5..2.0 per instance on older ffmpeg builds
    out = []
    while speed > 2.0:
        out.append("atempo=2.0")
        speed /= 2.0
    while speed < 0.5:
        out.append("atempo=0.5")
        speed /= 0.5
    out.append(f"atempo={speed:.6g}")
    return out


def _sw_encoder(family: str) -> str:
    if family == "h264":
        return "libx264"
    if family == "hevc":
        return "libx265"
    if family == "vp9":
        return "libvpx-vp9"
    encs = ffmpeg_encoders()
    for enc in ("libsvtav1", "libaom-av1"):
        if enc in encs:
            return enc
    return "libsvtav1"


def video_codec_args(p: dict, opts: dict, backend=None, bitrate_k=None,
                     pass_no=None, passlog=None) -> tuple[list, list, list]:
    """-> (codec args, args that go before -i, filters to append)."""
    crf = int(_float(opts.get("crf"), "quality (CRF)", 0, 63) or p["crf"])
    fam = p["family"]
    pre, tail = [], []
    rate = (["-b:v", f"{bitrate_k}k", "-maxrate", f"{int(bitrate_k * 1.5)}k",
             "-bufsize", f"{bitrate_k * 2}k"] if bitrate_k else [])

    if backend and fam in HW_BACKENDS[backend]:
        a = ["-c:v", HW_BACKENDS[backend][fam]]
        if backend == "nvenc":
            a += ["-preset", "p5", "-tune", "hq", "-rc", "vbr"]
            a += rate or ["-cq", str(crf), "-b:v", "0"]
            a += ["-pix_fmt", "yuv420p"]
        elif backend == "qsv":
            a += ["-preset", "medium"]
            a += rate or ["-global_quality", str(crf)]
            a += ["-pix_fmt", "nv12"]
        elif backend == "vaapi":
            pre = ["-vaapi_device", VAAPI_DEVICE]
            tail = ["format=nv12", "hwupload"]
            a += rate or ["-rc_mode", "CQP", "-qp", str(crf)]
        elif backend == "videotoolbox":
            # -q:v is 1..100, higher = better; roughly matches CRF 18..30
            a += rate or ["-q:v", str(max(1, min(100, int(round(100 - crf * 1.6)))))]
            a += ["-pix_fmt", "yuv420p"]
        elif backend == "amf":
            a += ["-quality", "balanced"]
            a += (["-rc", "vbr_peak"] + rate) if rate else \
                ["-rc", "cqp", "-qp_i", str(crf), "-qp_p", str(crf)]
        if fam == "hevc":
            a += ["-tag:v", "hvc1"]
        return a, pre, tail

    enc = _sw_encoder(fam)
    a = ["-c:v", enc]
    if enc in ("libx264", "libx265"):
        a += ["-preset", p.get("speed", "medium")]
    elif enc == "libsvtav1":
        a += ["-preset", "7"]
    elif enc == "libaom-av1":
        a += ["-cpu-used", "6", "-row-mt", "1"]
    elif enc == "libvpx-vp9":
        a += ["-deadline", "good", "-cpu-used", "2", "-row-mt", "1"]
    if bitrate_k:
        a += ["-b:v", f"{bitrate_k}k"]
        if pass_no:
            a += ["-pass", str(pass_no), "-passlogfile", passlog]
        elif enc in ("libx264", "libx265", "libvpx-vp9"):
            a += rate[2:]
    else:
        a += ["-crf", str(crf)]
        if enc in ("libvpx-vp9", "libaom-av1"):
            a += ["-b:v", "0"]
    if enc == "libx265":
        a += ["-x265-params", "log-level=error"]
    if p.get("pix"):
        a += ["-pix_fmt", p["pix"]]
    if fam == "hevc":
        a += ["-tag:v", "hvc1"]
    return a, pre, tail


def build_convert_job(src: str, dst: str, opts: dict, hw=None) -> Job:
    """Turn one input + options into a Job (one or two ffmpeg passes)."""
    head_ = ffmpeg_head()
    opts = clean_opts(opts)
    src, dst = os.path.abspath(os.path.expanduser(src)), os.path.abspath(os.path.expanduser(dst))
    if not os.path.isfile(src):
        raise RuntimeError(f"no such file: {src}")
    if src == dst:
        raise RuntimeError("the output would overwrite the input — pick another name")
    key = opts.get("preset") or "mp4"
    if key not in ALL_PRESETS:
        raise RuntimeError(f"unknown preset {key!r}")
    p = ALL_PRESETS[key]
    kind = p.get("kind", "video")
    gif, copy, raw = bool(p.get("gif")), bool(p.get("copy")), p.get("args")
    if not os.path.splitext(dst)[1]:  # ffmpeg picks the format from the extension
        dst += "." + preset_ext(key, src)
    warn = []
    os.makedirs(os.path.dirname(dst), exist_ok=True)

    if kind == "audio":
        dropped = [k for k in VIDEO_ONLY_KEYS if opts.get(k) and k != "watermark_pos"]
        if dropped:
            warn.append(f"ignored for an audio preset: {', '.join(dropped)}")

    # ---- timing
    start_s = _time(opts.get("start"), "start")
    end_s = _time(opts.get("end"), "end")
    dur_opt = _time(opts.get("duration"), "duration")
    speed = _float(opts.get("speed"), "speed", 0.1, 10.0) or 1.0
    src_dur = probe_duration(src)
    if end_s is not None:
        section = end_s - (start_s or 0.0)
        if section <= 0:
            raise RuntimeError("end must come after start")
    elif dur_opt:
        section = dur_opt
    elif src_dur:
        section = max(0.0, src_dur - (start_s or 0.0))
    else:
        section = None
    out_dur = section / speed if section else None
    explicit_len = end_s is not None or dur_opt is not None

    backend = None
    if kind == "video" and p.get("family"):
        backend, hw_warn = resolve_hw(hw if hw is not None else SETTINGS.get("hw"))
        if hw_warn and (hw or SETTINGS.get("hw")) not in (None, "none"):
            warn.append(hw_warn)

    tmpdir = None
    cwd = None

    def scratch():
        nonlocal tmpdir
        if tmpdir is None:
            tmpdir = tempfile.mkdtemp(prefix="skjmedia-")
        return tmpdir

    fin = _float(opts.get("fade_in"), "fade in", 0)
    fout = _float(opts.get("fade_out"), "fade out", 0)
    height = _int(opts.get("height"), "height", 16, 8640)
    fps = _float(opts.get("fps"), "frame rate", 1, 240)

    # ---- video filters
    vf, tail = [], []
    if kind == "video" and not copy:
        if opts.get("deinterlace"):
            vf.append("bwdif")
        if opts.get("crop"):
            vf.append(crop_filter(opts["crop"]))
        rot = str(opts.get("rotate", "")).lower()
        rotations = {"90": "transpose=1", "cw": "transpose=1", "180": "hflip,vflip",
                     "270": "transpose=2", "-90": "transpose=2", "ccw": "transpose=2"}
        if rot and rot not in ("0", "none"):
            if rot not in rotations:
                raise RuntimeError("rotate: use 90, 180 or 270")
            vf.append(rotations[rot])
        flip = str(opts.get("flip", "")).lower()
        flips = {"h": "hflip", "v": "vflip", "hv": "hflip,vflip", "both": "hflip,vflip"}
        if flip:
            if flip not in flips:
                raise RuntimeError("flip: use h, v or hv")
            vf.append(flips[flip])
        if height and not gif:
            vf.append(f"scale=-2:{height}:flags=lanczos")
        if opts.get("denoise"):
            vf.append("hqdn3d")
        if opts.get("burn_subs"):
            subs = os.path.abspath(os.path.expanduser(opts["burn_subs"]))
            if not os.path.isfile(subs):
                raise RuntimeError(f"subtitle file not found: {subs}")
            # filter arguments need painful escaping for Windows drive letters,
            # quotes and commas, so copy the file to a plain name and run there
            ext = os.path.splitext(subs)[1].lower() or ".srt"
            shutil.copyfile(subs, os.path.join(scratch(), "subs" + ext))
            cwd = scratch()
            sub = f"subtitles=subs{ext}"
            if start_s:  # a fast seek resets timestamps; shift so subs stay in sync
                sub = f"setpts=PTS+{start_s:.3f}/TB,{sub},setpts=PTS-STARTPTS"
            vf.append(sub)
        if speed != 1.0:
            vf.append(f"setpts=PTS/{speed:.6g}")
        if fps and not gif:
            vf.append(f"fps={fps:g}")
        if fin:
            vf.append(f"fade=t=in:st=0:d={fin:g}")
        if fout:
            if out_dur:
                vf.append(f"fade=t=out:st={max(0.0, out_dur - fout):.3f}:d={fout:g}")
            else:
                warn.append("fade out skipped — couldn't read the duration")
        if gif:
            scale = f"scale=-1:{height}:flags=lanczos" if height else \
                "scale='min(480,iw)':-1:flags=lanczos"
            tail = [f"fps={fps or 15:g}", scale,
                    "split[a][b];[a]palettegen=stats_mode=diff[p];"
                    "[b][p]paletteuse=dither=bayer:bayer_scale=5"]
    elif copy:
        if any(opts.get(k) for k in VIDEO_ONLY_KEYS if k != "watermark_pos") \
                or speed != 1.0 or fin or fout or opts.get("volume") or opts.get("normalize"):
            warn.append("filters are ignored by the remux-only preset")
        if start_s or explicit_len:
            warn.append("remux cuts land on keyframes, so the trim may be a little off")

    # ---- audio filters
    mute = bool(opts.get("mute")) or gif
    if mute and kind == "audio":
        raise RuntimeError("can't remove the audio from an audio-only preset")
    if kind == "audio" and probe_json(src) and not media_has(src, "audio"):
        raise RuntimeError("this file has no audio track")
    if kind == "video" and not mute and not copy and probe_json(src) \
            and not media_has(src, "audio"):
        mute = True  # nothing to encode; keeps fades/atempo from erroring
    af = []
    if not copy and not mute:
        if speed != 1.0:
            af += atempo_chain(speed)
        vol = _float(opts.get("volume"), "volume", -60, 60)
        if vol:
            af.append(f"volume={vol:g}dB")
        if opts.get("normalize"):
            # loudnorm works at 192 kHz internally; bring it back to a normal rate
            af += ["loudnorm=I=-16:TP=-1.5:LRA=11", "aresample=48000"]
        if fin:
            af.append(f"afade=t=in:st=0:d={fin:g}")
        if fout and out_dur:
            af.append(f"afade=t=out:st={max(0.0, out_dur - fout):.3f}:d={fout:g}")

    # ---- codecs / bitrate
    vargs, pre, hwtail = [], [], []
    target_k = None
    two_pass = False
    ab = opts.get("abitrate") or p.get("ab")
    if kind == "video" and p.get("family"):
        ab_k = 0 if mute else (parse_kbps(ab) or 128)
        if opts.get("target_size"):
            if not out_dur:
                raise RuntimeError("fit-in-size needs the duration, and ffprobe couldn't read it")
            size_b = parse_size(opts["target_size"])
            if not size_b:
                raise RuntimeError(f"can't read size {opts['target_size']!r} (try 25M)")
            target_k = int(size_b * 8 / 1000.0 / out_dur * 0.96 - ab_k)
            if target_k < 50:
                raise RuntimeError(
                    f"{opts['target_size']} is too small for {fmt_dur(out_dur)} of video — "
                    "trim it shorter, lower the audio bitrate or remove the audio")
            if opts.get("vbitrate"):
                warn.append("fit-in-size overrides the video bitrate")
        elif opts.get("vbitrate"):
            target_k = parse_kbps(opts["vbitrate"])
            if not target_k:
                raise RuntimeError(f"can't read bitrate {opts['vbitrate']!r} (try 6M)")
        vargs, pre, hwtail = video_codec_args(p, opts, backend, target_k)
        two_pass = bool(opts.get("target_size")) and vargs[1] in ("libx264", "libvpx-vp9")
    elif kind == "video" and p.get("vargs"):
        vargs = list(p["vargs"])
        if opts.get("crf") or opts.get("target_size") or opts.get("vbitrate"):
            warn.append(f"quality/size settings don't apply to {key}")
    elif kind == "video" and gif:
        vargs = ["-loop", "0"]
        if opts.get("target_size"):
            warn.append("fit-in-size doesn't work for GIFs — lower the height or fps instead")
    elif kind == "audio":
        vargs = ["-vn"]

    aargs = []
    if raw or copy:
        pass
    elif mute:
        aargs = ["-an"]
    elif p.get("acodec"):
        aargs = ["-c:a", p["acodec"]]
        if ab and p["acodec"] not in LOSSLESS_AUDIO:
            aargs += ["-b:a", str(ab)]

    # ---- inputs, graph, mapping
    inputs = list(pre)
    if start_s:
        inputs += ["-ss", fmt_ts(start_s)]
    if explicit_len and section:
        # as an input option the length is in source time, so speed changes
        # and filters downstream don't shorten or stretch the cut
        inputs += ["-t", f"{section:.3f}"]
    inputs += ["-i", src]
    vgraph, vmap, amap = [], [], []
    track = _int(opts.get("audio_track"), "audio track", 0, 99)
    wm = opts.get("watermark") if kind == "video" and not copy else None
    if wm:
        wm = os.path.abspath(os.path.expanduser(wm))
        if not os.path.isfile(wm):
            raise RuntimeError(f"watermark image not found: {wm}")
        inputs += ["-i", wm]
        pos = WATERMARK_POS.get(opts.get("watermark_pos") or "br", WATERMARK_POS["br"])
        graph = (f"[0:v]{','.join(vf) or 'null'}[base];"
                 f"[1:v]format=rgba,colorchannelmixer=aa=0.85[wm];"
                 f"[base][wm]overlay={pos}")
        if tail + hwtail:
            graph += "," + ",".join(tail + hwtail)
        vgraph = ["-filter_complex", graph + "[vout]"]
        vmap = ["-map", "[vout]"]
        amap = [] if mute else ["-map", f"0:a:{track or 0}?"]
    else:
        chain = vf + tail + hwtail
        if chain and not raw:
            vgraph = ["-vf", ",".join(chain)]
        if track is not None:
            vmap = ["-map", "0:v:0?"] if kind == "video" and not mute else []
            amap = [] if mute else ["-map", f"0:a:{track}"]
    maps = vmap + amap
    afargs = ["-af", ",".join(af)] if af and not raw else []
    if raw and (vf or af):
        if "-vf" not in raw and "-filter_complex" not in raw and (vf + tail):
            vgraph = ["-vf", ",".join(vf + tail)]
        if "-af" not in raw and af:
            afargs = ["-af", ",".join(af)]

    out_tail = []
    if opts.get("strip_metadata"):
        out_tail += ["-map_metadata", "-1", "-map_chapters", "-1"]
    out_tail += list(p.get("extra") or [])
    if copy:
        out_tail = ["-c", "copy"] + out_tail
    extra = shlex.split(opts["extra"], posix=PLATFORM != "windows") if opts.get("extra") else []
    codec = list(raw) if raw else vargs + aargs

    steps = []
    if two_pass:
        passlog = os.path.join(scratch(), "pass")
        v1 = video_codec_args(p, opts, None, target_k, 1, passlog)[0]
        v2 = video_codec_args(p, opts, None, target_k, 2, passlog)[0]
        steps.append(Step(head_ + inputs + vgraph + vmap + v1
                          + ["-an", "-f", "null", os.devnull], out_dur, "pass 1/2", cwd))
        steps.append(Step(head_ + inputs + vgraph + afargs + maps + v2 + aargs
                          + out_tail + extra + [dst], out_dur, "pass 2/2", cwd))
    else:
        steps.append(Step(head_ + inputs + vgraph + afargs + maps + codec
                          + out_tail + extra + [dst], out_dur, "", cwd))

    def summarize(_lines):
        try:
            out_b, in_b = os.path.getsize(dst), os.path.getsize(src)
        except OSError:
            return ""
        ratio = f" — {out_b / in_b * 100:.0f}% of the original" if in_b else ""
        return f"{fmt_bytes(out_b)}{ratio}"

    title = f"{os.path.basename(src)} → {os.path.basename(dst)}"
    if backend:
        title += f" [{backend}]"
    return Job("convert", title, steps, dst, summarize,
               [tmpdir] if tmpdir else [], warn)


def jobs_from_convert(inputs, opts, outdir=None, pattern=None, hw=None,
                      reserve=True) -> tuple[list[Job], list[str]]:
    jobs, errors = [], []
    preset = (opts or {}).get("preset") or "mp4"
    for src in inputs:
        try:
            dst = output_path(src, preset, outdir, pattern, reserve=reserve)
            jobs.append(build_convert_job(src, dst, opts, hw))
        except (RuntimeError, OSError) as exc:
            errors.append(f"{os.path.basename(src)}: {exc}")
    return jobs, errors


# --------------------------------------------------------------------------
# download
# --------------------------------------------------------------------------

_DEST_RES = [
    re.compile(r'^\[Merger\] Merging formats into "(.+)"$'),
    re.compile(r"^\[(?:download|ExtractAudio|VideoConvertor)\] Destination: (.+)$"),
    re.compile(r"^\[download\] (.+?) has already been downloaded"),
    re.compile(r'^\[MoveFiles\] Moving file ".+?" to "(.+)"$'),
]


def _ytdlp():
    if not BIN["yt-dlp"]:
        raise RuntimeError(f"yt-dlp not found. {install_hint('yt-dlp')}")
    return list(BIN["yt-dlp"])


def build_download_job(url: str, opts: dict) -> Job:
    cmd = _ytdlp()
    opts = clean_opts(opts)
    opts.setdefault("metadata", True)
    url = url.strip()
    if not url:
        raise RuntimeError("empty URL")
    fmt = opts.get("format") or "best"
    if fmt not in DOWNLOAD_FORMATS:
        raise RuntimeError(f"unknown format {fmt!r}")
    audio_only = fmt.startswith("audio-")
    warn = []
    cmd += list(DOWNLOAD_FORMATS[fmt]["args"])

    outdir = os.path.abspath(os.path.expanduser(
        opts.get("outdir") or SETTINGS.get("download_dir") or os.getcwd()))
    os.makedirs(outdir, exist_ok=True)
    template = opts.get("template") or (
        "%(playlist_index)03d - %(title).80s.%(ext)s" if opts.get("playlist")
        else "%(title).80s.%(ext)s")
    cmd += ["-o", os.path.join(outdir, template)]
    cmd += ["--newline", "--progress", "--no-colors"]
    if BIN["ffmpeg"] and os.path.isabs(BIN["ffmpeg"][0]) and not shutil.which("ffmpeg"):
        cmd += ["--ffmpeg-location", BIN["ffmpeg"][0]]

    if opts.get("playlist"):
        cmd.append("--yes-playlist")
        if opts.get("items"):
            cmd += ["--playlist-items", opts["items"]]
    else:
        cmd.append("--no-playlist")
        if opts.get("items"):
            warn.append("playlist items ignored — tick Whole playlist")
    if opts.get("subs"):
        if audio_only:
            warn.append("subtitles can't be embedded in audio-only downloads")
        else:
            cmd += ["--write-subs", "--write-auto-subs", "--sub-langs",
                    opts.get("sub_langs") or "en,pl", "--embed-subs"]
    if opts.get("thumbnail"):
        cmd.append("--embed-thumbnail")
    if opts.get("metadata"):
        cmd.append("--embed-metadata")
        if not audio_only:
            cmd.append("--embed-chapters")
    if opts.get("cookies_browser"):
        cmd += ["--cookies-from-browser", opts["cookies_browser"]]
    if opts.get("sponsorblock"):
        cmd += ["--sponsorblock-remove", "sponsor,selfpromo,intro,outro"]
    if opts.get("section"):
        a, sep, b = opts["section"].replace(" ", "").partition("-")
        if not sep or parse_timecode(a) is None or (b and parse_timecode(b) is None):
            raise RuntimeError("section: use start-end, e.g. 1:00-2:30")
        cmd += ["--download-sections", f"*{a}-{b or 'inf'}", "--force-keyframes-at-cuts"]
    if opts.get("limit_rate"):
        cmd += ["-r", opts["limit_rate"]]
    if opts.get("fragments"):
        cmd += ["-N", str(_int(opts["fragments"], "parallel fragments", 1, 32))]
    if opts.get("archive"):
        os.makedirs(CONFIG_DIR, exist_ok=True)
        cmd += ["--download-archive", os.path.join(CONFIG_DIR, "download-archive.txt")]
    if opts.get("split_chapters"):
        cmd.append("--split-chapters")
    if opts.get("extra"):
        cmd += shlex.split(opts["extra"], posix=PLATFORM != "windows")
    cmd += ["--", url]

    def summarize(lines):
        saved = []
        for line in lines:
            for rx in _DEST_RES:
                m = rx.match(line)
                if m:
                    name = os.path.basename(m.group(1))
                    stem = os.path.splitext(name)[0]
                    # the merger / extractor line replaces the per-stream ones
                    saved = [s for s in saved if os.path.splitext(s)[0].split(".f")[0] != stem
                             and not s.startswith(stem + ".")]
                    saved.append(name)
        if not saved:
            return ""
        return saved[-1] if len(saved) == 1 else f"{len(saved)} files, last: {saved[-1]}"

    title = re.sub(r"^https?://(www\.)?", "", url)
    title = (title[:60] + "…") if len(title) > 61 else title
    return Job("download", f"{title} [{fmt}]", [Step(cmd)], outdir, summarize, warnings=warn)


def jobs_from_download(urls, opts) -> tuple[list[Job], list[str]]:
    jobs, errors = [], []
    for url in urls:
        try:
            jobs.append(build_download_job(url, opts))
        except RuntimeError as exc:
            errors.append(f"{url}: {exc}")
    return jobs, errors


def split_urls(text: str) -> list[str]:
    return [u for u in re.split(r"\s+", text or "") if u]


def fetch_url_info(url: str, playlist=False, cookies_browser=None) -> str:
    """Ask yt-dlp what's behind a link, without downloading it."""
    cmd = _ytdlp() + ["-J", "--no-warnings"]
    cmd += ["--flat-playlist", "--yes-playlist"] if playlist else ["--no-playlist"]
    if cookies_browser:
        cmd += ["--cookies-from-browser", cookies_browser]
    code, out, err = capture(cmd + ["--", url.strip()], 120)
    if code != 0 or not out.strip():
        msg = [ln for ln in err.splitlines() if ln.strip()]
        raise RuntimeError(msg[-1] if msg else f"yt-dlp failed (exit {code})")
    try:
        d = json.loads(out)
    except ValueError:
        raise RuntimeError("yt-dlp returned something unreadable")
    lines = [d.get("title") or "(no title)"]
    who = d.get("uploader") or d.get("channel")
    if who:
        lines.append(f"by {who}")
    if d.get("_type") == "playlist":
        entries = d.get("entries") or []
        lines.append(f"playlist · {d.get('playlist_count') or len(entries)} items")
        for i, e in enumerate(entries[:8], 1):
            dur = f"  {fmt_dur(e['duration'])}" if e.get("duration") else ""
            lines.append(f"  {i:>3}. {(e.get('title') or e.get('url') or '?')[:70]}{dur}")
        if len(entries) > 8:
            lines.append(f"  … {len(entries) - 8} more")
        return "\n".join(lines)
    facts = []
    if d.get("duration"):
        facts.append(fmt_dur(d["duration"]))
    if d.get("upload_date"):
        u = d["upload_date"]
        facts.append(f"{u[:4]}-{u[4:6]}-{u[6:]}")
    if d.get("view_count"):
        facts.append(f"{d['view_count']:,} views")
    if d.get("is_live"):
        facts.append("LIVE")
    if facts:
        lines.append(" · ".join(facts))
    heights = sorted({f.get("height") for f in d.get("formats") or []
                      if f.get("height") and f.get("vcodec") not in (None, "none")})
    if heights:
        lines.append("video: " + ", ".join(f"{h}p" for h in heights[-6:]))
    best = [f for f in d.get("formats") or [] if f.get("acodec") not in (None, "none")
            and f.get("vcodec") in (None, "none")]
    if best:
        top = max(best, key=lambda f: f.get("abr") or 0)
        if top.get("abr"):
            lines.append(f"best audio: {top.get('acodec')} ~{top['abr']:.0f} kb/s")
    if d.get("chapters"):
        lines.append(f"{len(d['chapters'])} chapters")
    subs = sorted((d.get("subtitles") or {}).keys())
    if subs:
        lines.append("subtitles: " + ", ".join(subs[:10]) + (" …" if len(subs) > 10 else ""))
    return "\n".join(lines)


def build_update_job() -> Job:
    if BIN["yt-dlp"] and BIN["yt-dlp"][1:2] == ["-m"]:
        cmd = [sys.executable, "-m", "pip", "install", "-U", "yt-dlp"]
    elif BIN["yt-dlp"]:
        cmd = BIN["yt-dlp"] + ["-U"]
    else:
        cmd = [sys.executable, "-m", "pip", "install", "-U", "yt-dlp"]
    return Job("update", "update yt-dlp", [Step(cmd)], "")


# --------------------------------------------------------------------------
# tools
# --------------------------------------------------------------------------


def _side(src, suffix, ext, outdir=None, reserve=True) -> str:
    name = os.path.splitext(os.path.basename(src))[0]
    folder = os.path.abspath(os.path.expanduser(outdir)) if outdir \
        else os.path.dirname(os.path.abspath(src))
    return unique_path(os.path.join(folder, f"{name}{suffix}.{ext}"), reserve)


def build_tool_job(tool: str, inputs: list[str], params: dict, outdir=None,
                   reserve=True) -> Job:
    if tool not in TOOLS or tool == "inspect":
        raise RuntimeError(f"{tool} isn't a job tool")
    params = clean_opts(params)
    if not inputs:
        raise RuntimeError("add an input file first")
    inputs = [os.path.abspath(os.path.expanduser(p)) for p in inputs]
    for path in inputs:
        if not os.path.isfile(path):
            raise RuntimeError(f"no such file: {path}")
    src = inputs[0]
    dur = probe_duration(src)
    name = os.path.basename(src)

    if tool == "thumbnail":
        at = _time(params.get("at"), "time")
        if at is None:
            at = dur * 0.1 if dur else 0.0
        width = _int(params.get("width"), "width", 16, 8000)
        dst = _side(src, "_thumb", "jpg", outdir, reserve)
        cmd = ffmpeg_head() + ["-ss", fmt_ts(at), "-i", src, "-frames:v", "1"]
        if width:
            cmd += ["-vf", f"scale={width}:-2:flags=lanczos"]
        cmd += ["-q:v", "2", "-update", "1", dst]
        return Job("tool", f"thumbnail @ {fmt_dur(at)}: {name}", [Step(cmd, None)], dst)

    if tool == "sheet":
        cols = _int(params.get("cols"), "columns", 1, 20) or 4
        rows = _int(params.get("rows"), "rows", 1, 20) or 4
        width = _int(params.get("width"), "sheet width", 200, 10000) or 1600
        n = cols * rows
        tile_w = max(16, (width - 6 * (cols + 1)) // cols)
        rate = f"{(n + 1) / dur:.6f}" if dur else "1/10"
        dst = _side(src, "_sheet", "jpg", outdir, reserve)
        vf = (f"fps={rate},scale={tile_w}:-2,"
              f"tile={cols}x{rows}:padding=6:margin=6:color=0x1b1d21")
        cmd = ffmpeg_head() + ["-i", src, "-an", "-vf", vf, "-frames:v", "1",
                               "-q:v", "3", "-update", "1", dst]
        return Job("tool", f"contact sheet {cols}x{rows}: {name}", [Step(cmd, dur)], dst)

    if tool == "waveform":
        w = _int(params.get("width"), "width", 100, 10000) or 1600
        h = _int(params.get("height"), "height", 50, 4000) or 300
        color = (params.get("color") or "#ff6a00").lstrip("#")
        if not re.match(r"^[0-9a-fA-F]{6}$", color):
            raise RuntimeError("colour: use a hex value like #ff6a00")
        dst = _side(src, "_waveform", "png", outdir, reserve)
        graph = (f"[0:a:0]aformat=channel_layouts=mono,"
                 f"showwavespic=s={w}x{h}:colors=0x{color}[out]")
        cmd = ffmpeg_head() + ["-i", src, "-filter_complex", graph, "-map", "[out]",
                               "-frames:v", "1", "-update", "1", dst]
        return Job("tool", f"waveform: {name}", [Step(cmd, None)], dst)

    if tool == "loudness":
        cmd = ffmpeg_head("info") + ["-i", src, "-map", "0:a:0", "-vn", "-sn",
                                     "-af", "loudnorm=print_format=json",
                                     "-f", "null", os.devnull]

        def summarize(lines):
            text = "\n".join(lines)
            blob = text[text.rfind("{"):text.rfind("}") + 1] if "{" in text else ""
            try:
                d = json.loads(blob)
                i, tp, lra = float(d["input_i"]), float(d["input_tp"]), float(d["input_lra"])
            except (ValueError, KeyError):
                return "couldn't read the measurement"
            advice = f"{-14 - i:+.1f} dB to reach -14 LUFS (YouTube/Spotify)"
            if tp > -1.0:
                advice += " · peaks are hot, watch for clipping"
            return f"{i:.1f} LUFS integrated · {tp:.1f} dBTP peak · {lra:.1f} LU range · {advice}"

        return Job("tool", f"loudness: {name}", [Step(cmd, dur)], src, summarize)

    if tool == "join":
        if len(inputs) < 2:
            raise RuntimeError("join needs at least two files")
        mode = params.get("mode") or "copy"
        ext = os.path.splitext(src)[1].lstrip(".") or "mkv"
        if mode == "mp4":
            ext = "mp4"
        dst = params.get("output")
        dst = unique_path(os.path.abspath(os.path.expanduser(dst)), reserve) if dst \
            else _side(src, "_joined", ext, outdir, reserve)
        tmpdir = tempfile.mkdtemp(prefix="skjmedia-")
        listfile = os.path.join(tmpdir, "list.txt")
        with open(listfile, "w", encoding="utf-8") as fh:
            for path in inputs:
                fh.write("file '" + path.replace("'", "'\\''") + "'\n")
        total = sum(probe_duration(p) or 0 for p in inputs) or None
        cmd = ffmpeg_head() + ["-f", "concat", "-safe", "0", "-i", listfile]
        if mode == "mp4":
            cmd += ["-c:v", "libx264", "-preset", "medium", "-crf", "20",
                    "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k"] + FASTSTART
        else:
            cmd += ["-c", "copy"]
        cmd.append(dst)
        return Job("tool", f"join {len(inputs)} files → {os.path.basename(dst)}",
                   [Step(cmd, total)], dst, cleanup=[tmpdir],
                   warnings=[] if mode == "mp4" else
                   ["fast join needs files with the same codec and size — "
                    "switch Mode to re-encode if it fails"])

    if tool == "split":
        every = _time(params.get("every") or "600", "chunk length")
        if not every or every < 1:
            raise RuntimeError("chunk length must be at least 1 second")
        stem, ext = os.path.splitext(os.path.basename(src))
        folder = os.path.abspath(os.path.expanduser(outdir)) if outdir \
            else os.path.dirname(src)
        pattern = os.path.join(folder, f"{stem}_part%03d{ext or '.mkv'}")
        cmd = ffmpeg_head() + ["-i", src, "-map", "0:v?", "-map", "0:a?", "-c", "copy",
                               "-f", "segment", "-segment_time", f"{every:.3f}",
                               "-reset_timestamps", "1", pattern]
        return Job("tool", f"split every {fmt_dur(every)}: {name}", [Step(cmd, dur)],
                   folder, warnings=["chunks are cut on keyframes, so lengths vary a bit"])

    if tool == "subs":
        track = _int(params.get("track"), "subtitle track", 0, 99) or 0
        dst = _side(src, f"_subs{track}", "srt", outdir, reserve)
        cmd = ffmpeg_head() + ["-i", src, "-map", f"0:s:{track}", "-c:s", "srt", dst]
        if probe_json(src) and not media_has(src, "subtitle"):
            raise RuntimeError(f"{name} has no subtitle tracks")
        return Job("tool", f"subtitles #{track}: {name}", [Step(cmd, dur)], dst)

    raise RuntimeError(f"unknown tool {tool!r}")


# --------------------------------------------------------------------------
# the runner — shared by every interface
# --------------------------------------------------------------------------

_KV_RE = re.compile(r"^([a-z_0-9]+)=\s*(\S*)$")
_YT_RE = re.compile(r"^\[download\]\s+(\d+(?:\.\d+)?)%"
                    r"(?:\s+of\s+~?\s*(\S+))?(?:.*?\s+at\s+(\S+))?(?:.*?\s+ETA\s+(\S+))?")


class Progress:
    __slots__ = ("pct", "speed", "eta", "detail")

    def __init__(self, pct=None, speed="", eta=None, detail=""):
        self.pct, self.speed, self.eta, self.detail = pct, speed, eta, detail

    def text(self) -> str:
        bits = []
        if self.pct is not None:
            bits.append(f"{self.pct:.1f}%")
        if self.speed:
            bits.append(self.speed)
        if self.eta is not None:
            bits.append(f"ETA {fmt_dur(self.eta)}")
        if self.detail:
            bits.append(self.detail)
        return " · ".join(bits)


class ProgressParser:
    """Understands ffmpeg's -progress key=value stream and yt-dlp's [download] lines."""

    def __init__(self, total=None):
        self.total = total
        self.kv = {}

    def feed(self, line: str):
        """-> (Progress or None, hide_line)"""
        m = _KV_RE.match(line)
        if m:
            self.kv[m.group(1)] = m.group(2)
            if m.group(1) == "progress":
                return self._ffmpeg(m.group(2) == "end"), True
            return None, True
        m = _YT_RE.match(line)
        if m:
            pct = float(m.group(1))
            eta = m.group(4)
            eta_s = parse_timecode(eta) if eta and eta[0].isdigit() else None
            speed = m.group(3) if m.group(3) and m.group(3)[0].isdigit() else ""
            detail = f"of {m.group(2)}" if m.group(2) and m.group(2)[0].isdigit() else ""
            return Progress(pct, speed, eta_s, detail), pct < 100.0
        return None, False

    def _ffmpeg(self, end: bool) -> Progress:
        t = None
        for key in ("out_time_us", "out_time_ms"):  # both are µs (ffmpeg quirk)
            try:
                t = int(self.kv.get(key, "")) / 1e6
                break
            except ValueError:
                continue
        try:
            sx = float(self.kv.get("speed", "").rstrip("x"))
        except ValueError:
            sx = None
        pct = None
        if self.total and t is not None:
            pct = max(0.0, min(100.0, t / self.total * 100.0))
        if end:
            pct = 100.0
        eta = (self.total - t) / sx if self.total and t is not None and sx else None
        bits = []
        fps = self.kv.get("fps", "")
        if fps and fps not in ("0", "0.00", "0.0"):
            bits.append(f"{float(fps):.0f} fps" if fps.replace(".", "").isdigit() else fps)
        if self.kv.get("total_size", "").isdigit():
            bits.append(fmt_bytes(int(self.kv["total_size"])))
        if t is not None and not self.total:
            bits.append(fmt_dur(t))
        return Progress(pct, f"{sx:.2f}x" if sx else "", eta, " · ".join(bits))


class Runner:
    """Runs a job's commands, streaming output lines back through callbacks.

    on_line(text)          every visible line of output
    on_progress(Progress)  best effort
    Returns the exit code. Cancellable via .stop().
    """

    def __init__(self):
        self.proc = None
        self.cancelled = False

    def stop(self):
        self.cancelled = True
        proc = self.proc
        if proc and proc.poll() is None:
            try:
                proc.terminate()
            except Exception:
                pass

            def reap():
                try:
                    proc.wait(5)
                except Exception:
                    try:
                        proc.kill()
                    except Exception:
                        pass

            threading.Thread(target=reap, daemon=True).start()

    def run_job(self, job: Job, on_line=None, on_progress=None) -> int:
        on_progress = on_progress or (lambda _p: None)
        n = len(job.steps)
        for i, step in enumerate(job.steps):
            if self.cancelled:
                return 130

            def scaled(pr, i=i, step=step):
                if pr.pct is not None:
                    pr.pct = (i * 100.0 + pr.pct) / n
                if pr.eta is not None and n > 1:
                    pr.eta = pr.eta + (n - 1 - i) * (step.total or 0) * 0.6
                if step.label:
                    pr.detail = step.label + (f" · {pr.detail}" if pr.detail else "")
                on_progress(pr)

            code = self.run(step.cmd, on_line, scaled, step.total, step.cwd)
            if code != 0:
                return code
        return 0

    def run(self, cmd, on_line=None, on_progress=None, total=None, cwd=None) -> int:
        on_line = on_line or (lambda _: None)
        on_progress = on_progress or (lambda _: None)

        on_line("$ " + quote_cmd(cmd))
        parser = ProgressParser(total)

        if NO_SUBPROCESS:
            return self._run_via_system(cmd, on_line, on_progress, parser, cwd)

        env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1")
        try:
            self.proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL, text=True, encoding="utf-8",
                errors="replace", bufsize=1, cwd=cwd, env=env, **popen_kwargs(),
            )
        except OSError as exc:
            on_line(f"!! could not start {cmd[0]}: {exc}")
            return 127

        for raw in self.proc.stdout:
            line = raw.rstrip("\r\n")
            if not line.strip():
                continue
            pr, hide = parser.feed(line)
            if pr is not None:
                on_progress(pr)
            if not hide:
                on_line(line)
        code = self.proc.wait()
        self.proc = None
        if code == 0:
            on_progress(Progress(100.0))
        return code

    def _run_via_system(self, cmd, on_line, on_progress, parser, cwd) -> int:
        """iOS path: no subprocess, so shell out and read the log afterwards."""
        log = os.path.join(tempfile.gettempdir(), f"skjmedia-{int(time.time() * 1000)}.log")
        shell_cmd = quote_cmd(cmd) + f" > {shlex.quote(log)} 2>&1"
        on_line("(a-Shell: running without live output, please wait...)")
        here = os.getcwd()
        try:
            if cwd:
                os.chdir(cwd)
            code = os.system(shell_cmd)
        finally:
            os.chdir(here)
        try:
            with open(log, "r", errors="replace") as fh:
                for line in fh:
                    line = line.rstrip("\r\n")
                    if line.strip() and not parser.feed(line)[1]:
                        on_line(line)
        except OSError:
            pass
        finally:
            try:
                os.remove(log)
            except OSError:
                pass
        on_progress(Progress(100.0 if code == 0 else 0.0))
        return code


class JobQueue:
    """Runs queued jobs one after another (ffmpeg already uses every core)."""

    def __init__(self):
        self.jobs: list[Job] = []
        self.runner = Runner()
        self.running = False
        self.current: Job | None = None
        self._stop = False

    def add(self, job: Job):
        self.jobs.append(job)

    def pending(self) -> list[Job]:
        return [j for j in self.jobs if j.status == "queued"]

    def remove(self, job_ids):
        self.jobs = [j for j in self.jobs if j.id not in job_ids or j.status == "running"]

    def clear_finished(self):
        self.jobs = [j for j in self.jobs if j.status in ("queued", "running")]

    def stop(self):
        self._stop = True
        self.runner.stop()

    def run(self, on_line=None, on_progress=None, on_state=None) -> tuple[int, int]:
        """Blocking — call from a worker thread in GUIs. -> (done, failed)."""
        on_line = on_line or (lambda _j, _t: None)
        on_progress = on_progress or (lambda _j, _p: None)
        on_state = on_state or (lambda _j: None)
        self.running, self._stop = True, False
        done = failed = 0
        try:
            while not self._stop:
                pending = self.pending()
                if not pending:
                    break
                job = self.current = pending[0]
                job.status = "running"
                on_state(job)
                self.runner.cancelled = False
                t0 = time.time()

                def line_cb(text, job=job):
                    job.lines.append(text)
                    on_line(job, text)

                def prog_cb(pr, job=job):
                    job.progress = pr
                    on_progress(job, pr)

                try:
                    code = self.runner.run_job(job, line_cb, prog_cb)
                except Exception as exc:  # a bug shouldn't kill the whole queue
                    line_cb(f"!! {exc}")
                    code = 1
                job.code, job.took = code, time.time() - t0
                if self.runner.cancelled:
                    job.status = "cancelled"
                elif code == 0:
                    job.status = "done"
                    done += 1
                    if job.summarize:
                        try:
                            job.summary = job.summarize(list(job.lines)) or ""
                        except Exception:
                            job.summary = ""
                else:
                    job.status = "failed"
                    failed += 1
                    tail = job.tail(1)
                    job.summary = tail[0] if tail else f"exit {code}"
                job.cleanup()
                _RESERVED.discard(job.target)
                record_history(job)
                on_state(job)
        finally:
            self.current = None
            self.running = False
        return done, failed


# --------------------------------------------------------------------------
# environment report
# --------------------------------------------------------------------------


def tool_version(name: str) -> str:
    if not BIN.get(name):
        return ""
    flag = "--version" if name == "yt-dlp" else "-version"
    _code, out, err = capture(BIN[name] + [flag], 20)
    first = (out or err).strip().splitlines()
    return first[0][:80] if first else ""


def doctor_report() -> list[tuple[str, str, bool | None]]:
    """-> rows of (label, value, ok) — ok=None means informational."""
    rows = [("platform", PLATFORM, None),
            ("python", sys.version.split()[0], None),
            ("subprocess", "no (a-Shell) — using os.system" if NO_SUBPROCESS else "yes", None),
            ("settings", CONFIG_DIR, None)]
    for tool in ("ffmpeg", "ffprobe", "yt-dlp"):
        if BIN[tool]:
            ver = tool_version(tool)
            rows.append((tool, f"{bin_label(tool)}  {ver}".strip(), True))
        else:
            rows.append((tool, f"missing — {install_hint(tool)}", False))
    if BIN["ffmpeg"]:
        encs = ffmpeg_encoders()
        for enc in ("libx264", "libx265", "libsvtav1", "libaom-av1", "libvpx-vp9",
                    "libopus", "libmp3lame", "libvorbis"):
            rows.append((f"  {enc}", "yes" if enc in encs else "no", enc in encs))
        for backend in HW_ORDER.get(PLATFORM, []):
            ok = hw_works(backend)
            rows.append((f"  GPU {backend}", "works" if ok else "not available", ok))
    rows.append(("PySide6", "yes" if have_qt() else "no", None))
    rows.append(("tkinter", "yes" if have_tk() else "no", None))
    return rows


# --------------------------------------------------------------------------
# interface 1 — plain text CLI
# --------------------------------------------------------------------------


def c(text, code):
    if PLATFORM == "ios" or not sys.stdout.isatty() or os.environ.get("NO_COLOR"):
        return text
    return f"\033[{code}m{text}\033[0m"


def head(text):
    print()
    print(c(f"── {text} " + "─" * max(0, 46 - len(text)), "36"))


def ask(prompt, default=""):
    suffix = f" [{default}]" if default else ""
    try:
        val = input(f"{prompt}{suffix}: ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        raise SystemExit(0)
    return val or default


def ask_path(prompt, default=""):
    return os.path.expanduser(ask(prompt, default).strip().strip('"').strip("'"))


def ask_choice(prompt, options: dict, default: str):
    print()
    keys = list(options)
    for i, k in enumerate(keys, 1):
        mark = "*" if k == default else " "
        label = options[k]["label"] if isinstance(options[k], dict) else options[k]
        print(f" {mark}{i:>2}. {c(f'{k:<13}', '1')} {label}")
    raw = ask(prompt, default)
    if raw.isdigit() and 1 <= int(raw) <= len(keys):
        return keys[int(raw) - 1]
    return raw if raw in options else default


def ask_yes(prompt, default=False):
    d = "y/N" if not default else "Y/n"
    val = ask(f"{prompt} ({d})").lower()
    if not val:
        return default
    return val.startswith(("y", "t"))


def ask_field(f: Field, default=None):
    if f.kind == "check":
        return ask_yes(f.label, bool(default if default is not None else f.default))
    if f.kind == "choice":
        opts = f.options()
        shown = " / ".join(f"{v}={lbl}" if v and lbl != v else (v or "none") for v, lbl in opts)
        val = ask(f"{f.label} ({shown})", default or f.default or "")
        valid = [v for v, _ in opts]
        return val if val in valid else ""
    hint = f" ({f.ph})" if f.ph else ""
    if f.kind in ("file", "dir", "save"):
        return ask_path(f"{f.label}{hint}", default or "")
    return ask(f"{f.label}{hint}", default or "")


def ask_some_fields(fields, title="Extra options") -> dict:
    """Show a numbered menu, ask only the ones the user picks."""
    print()
    print(c(f"  {title} — type the numbers to set (blank = none):", "90"))
    for i, f in enumerate(fields, 1):
        end = "\n" if i % 3 == 0 or i == len(fields) else ""
        sys.stdout.write(f"  {i:>2}. {f.label:<22}{end}")
    picks = re.findall(r"\d+", ask("Options"))
    out = {}
    for pick in picks:
        idx = int(pick) - 1
        if 0 <= idx < len(fields):
            f = fields[idx]
            if f.tip:
                print(c(f"      {f.tip}", "90"))
            out[f.key] = ask_field(f)
    return out


class CliProgress:
    def __init__(self, verbose=False):
        self.verbose = verbose
        self.tty = sys.stdout.isatty()
        self.active = False
        self.width = 0
        self.last_draw = 0.0
        self.last_step = -1

    def line(self, _job, text):
        show = self.verbose or text.startswith(("!!", "(")) \
            or "error" in text[:80].lower() or text.startswith("WARNING")
        if text.startswith("$ "):
            self._clear()
            print(c(text if self.verbose else text[:160] + (" …" if len(text) > 160 else ""),
                    "90"))
            return
        if show:
            self._clear()
            print(text)

    def progress(self, _job, pr: Progress):
        if pr.pct is None:
            return
        now = time.time()
        if self.tty:
            if now - self.last_draw < 0.15 and pr.pct < 100:
                return
            self.last_draw = now
            width = 28
            filled = int(width * pr.pct / 100)
            bar = "█" * filled + "░" * (width - filled)
            extra = "  ".join(x for x in (pr.speed, f"ETA {fmt_dur(pr.eta)}"
                                          if pr.eta is not None and pr.pct < 100 else "") if x)
            text = f"  {bar} {pr.pct:5.1f}%  {extra}"
            sys.stdout.write("\r" + text.ljust(self.width))
            sys.stdout.flush()
            self.width = len(text)
            self.active = True
        else:
            step = int(pr.pct // 10)
            if step != self.last_step:
                self.last_step = step
                print(f"  {pr.pct:5.1f}%")

    def _clear(self):
        if self.active:
            sys.stdout.write("\r" + " " * self.width + "\r")
            sys.stdout.flush()
            self.active = False

    def finish(self):
        if self.active:
            sys.stdout.write("\n")
            sys.stdout.flush()
            self.active = False
        self.last_step = -1


def run_jobs_cli(jobs, verbose=False, dry_run=False, errors=()) -> int:
    for err in errors:
        print(c(f"  !! {err}", "31"))
    if not jobs:
        return 1 if errors else 0
    if dry_run:
        for job in jobs:
            print(c(f"# {job.title}", "36"))
            for w in job.warnings:
                print(c(f"#   ! {w}", "33"))
            for step in job.steps:
                print(quote_cmd(step.cmd))
            job.cleanup()
        return 0

    q = JobQueue()
    for job in jobs:
        q.add(job)
    prog = CliProgress(verbose)
    total = len(jobs)
    t_all = time.time()

    def on_state(job):
        if job.status == "running":
            idx = jobs.index(job) + 1
            print()
            print(c(f"  [{idx}/{total}] {job.title}" if total > 1 else f"  {job.title}", "1"))
            for w in job.warnings:
                print(c(f"  ! {w}", "33"))
            return
        prog.finish()
        if job.status == "done":
            where = f" -> {job.target}" if job.target else ""
            print(c(f"  ✓ done in {fmt_dur(job.took)}{where}", "32"))
            if job.summary:
                print(c(f"    {job.summary}", "32"))
        elif job.status == "cancelled":
            print(c("  cancelled", "33"))
        else:
            print(c(f"  ✗ failed (exit {job.code})", "31"))
            if not verbose:
                for ln in job.tail(6):
                    print(c(f"    {ln}", "31"))
                print(c("    re-run with --verbose for the full log", "90"))

    try:
        done, failed = q.run(prog.line, prog.progress, on_state)
    except KeyboardInterrupt:
        q.stop()
        prog.finish()
        print(c("\n  cancelled", "33"))
        return 130
    if total > 1:
        print()
        print(c(f"  {done} done, {failed} failed in {fmt_dur(time.time() - t_all)}",
                "32" if not failed else "33"))
    if time.time() - t_all > 20:
        notify(APP_NAME, f"{done} done" + (f", {failed} failed" if failed else ""))
    return 0 if not failed else 1


def cli_ask_convert(src_for_kind=None) -> tuple[dict, str | None]:
    last = SETTINGS.get("last_convert") or {}
    preset = ask_choice("Preset (number or name)", ALL_PRESETS, last.get("preset") or "mp4")
    opts = {"preset": preset}
    kind = ALL_PRESETS[preset].get("kind")
    if kind == "video" and preset != "copy":
        h = ask("Scale to height (blank = keep)")
        if h.isdigit():
            opts["height"] = int(h)
    start = ask("Trim start (hh:mm:ss, blank = none)")
    if start:
        opts["start"] = start
        end = ask("Trim end (hh:mm:ss or blank)")
        if end:
            opts["end"] = end
    hw = None
    extras = [f for f in CONVERT_FIELDS if f.key not in ("height", "start", "end")
              and (kind == "video" or not f.video)]
    if ask_yes("More options (quality, fit-in-size, filters, GPU…)?", False):
        opts.update(ask_some_fields(extras))
        if kind == "video" and ALL_PRESETS[preset].get("family"):
            hw = ask_choice("Encoder", dict(hw_choices()), SETTINGS.get("hw", "none"))
    SETTINGS.set("last_convert", {"preset": preset})
    return opts, hw


def cli_convert(verbose=False):
    head("Convert")
    src = ask_path("Input file")
    if not os.path.isfile(src):
        print(c(f"  no such file: {src}", "31"))
        return
    info = probe_summary(src)
    if info:
        print(c(f"  {info}", "90"))
    opts, hw = cli_ask_convert()
    dst = ask_path("Output file", output_path(src, opts["preset"], reserve=False))
    try:
        job = build_convert_job(src, dst, opts, hw)
    except RuntimeError as exc:
        print(c(f"  !! {exc}", "31"))
        return
    run_jobs_cli([job], verbose)


def cli_batch(verbose=False):
    head("Batch convert")
    raw = ask("Files, folders or globs (space separated)")
    try:
        items = shlex.split(raw, posix=PLATFORM != "windows")
    except ValueError:
        items = raw.split()
    recursive = any(os.path.isdir(os.path.expanduser(i)) for i in items) \
        and ask_yes("Include sub-folders?", False)
    inputs = expand_inputs(items, recursive)
    if not inputs:
        print(c("  nothing found", "31"))
        return
    print(c(f"  {len(inputs)} file(s)", "90"))
    opts, hw = cli_ask_convert()
    outdir = ask_path("Output folder (blank = next to each file)", SETTINGS.get("outdir", ""))
    jobs, errors = jobs_from_convert(inputs, opts, outdir or None, None, hw)
    run_jobs_cli(jobs, verbose, errors=errors)


def cli_download(verbose=False):
    head("Download")
    urls = split_urls(ask("URL(s), space separated"))
    if not urls:
        return
    if ask_yes("Show info first?", False):
        try:
            print(c("  " + fetch_url_info(urls[0]).replace("\n", "\n  "), "90"))
        except RuntimeError as exc:
            print(c(f"  !! {exc}", "31"))
    last = SETTINGS.get("last_download") or {}
    fmt = ask_choice("Format", DOWNLOAD_FORMATS, last.get("format") or "best")
    opts = {"format": fmt,
            "outdir": ask_path("Save to folder", SETTINGS.get("download_dir") or os.getcwd())}
    opts["playlist"] = ask_yes("Whole playlist?", False)
    opts.update(ask_some_fields([f for f in DOWNLOAD_FIELDS
                                 if f.key not in ("outdir", "playlist")]))
    SETTINGS.set("last_download", {"format": fmt})
    jobs, errors = jobs_from_download(urls, opts)
    run_jobs_cli(jobs, verbose, errors=errors)


def cli_tools(verbose=False):
    head("Tools")
    tool = ask_choice("Tool", TOOLS, "thumbnail")
    spec = TOOLS[tool]
    if spec.get("multi"):
        raw = ask("Files, in order (space separated)")
        try:
            inputs = expand_inputs(shlex.split(raw, posix=PLATFORM != "windows"))
        except ValueError:
            inputs = expand_inputs(raw.split())
    else:
        inputs = [ask_path("File")]
    if tool == "inspect":
        for path in inputs:
            print(probe_details(path))
        return
    params = {}
    for f in spec["fields"]:
        params[f.key] = ask_field(f)
    try:
        job = build_tool_job(tool, inputs, params)
    except RuntimeError as exc:
        print(c(f"  !! {exc}", "31"))
        return
    run_jobs_cli([job], verbose)


def cli_history():
    head("History")
    rows = load_history(25)
    if not rows:
        print("  nothing yet")
        return
    colors = {"done": "32", "failed": "31", "cancelled": "33"}
    for r in rows:
        when = time.strftime("%m-%d %H:%M", time.localtime(r.get("t", 0)))
        status = c(f"{r.get('status', '?'):<9}", colors.get(r.get("status"), "0"))
        print(f"  {when}  {status} {r.get('kind', ''):<8} {r.get('title', '')[:70]}")
        if r.get("summary"):
            print(c(f"                    {r['summary'][:90]}", "90"))


def cli_settings():
    head("Settings")
    print(c(f"  stored in {SETTINGS.path}", "90"))
    hw = ask_choice("Default encoder", dict(hw_choices()), SETTINGS.get("hw", "none"))
    SETTINGS.data["hw"] = hw
    SETTINGS.data["outdir"] = ask_path("Default output folder (blank = next to source)",
                                       SETTINGS.get("outdir", ""))
    SETTINGS.data["download_dir"] = ask_path("Default download folder",
                                             SETTINGS.get("download_dir", ""))
    SETTINGS.data["suffix"] = ask("File name suffix", SETTINGS.get("suffix", "_skj"))
    SETTINGS.data["overwrite"] = ask_yes("Overwrite existing files?",
                                         SETTINGS.get("overwrite", False))
    SETTINGS.data["notify"] = ask_yes("Notify when long jobs finish?",
                                      SETTINGS.get("notify", True))
    SETTINGS.save()
    print(c("  saved", "32"))


def cli_doctor():
    head(f"{APP_NAME} {APP_VERSION} — doctor")
    for label, value, ok in doctor_report():
        text = f"  {label:<16} {value}"
        print(c(text, "32") if ok else c(text, "31") if ok is False else text)
    print()


def cli_main(verbose=False):
    head(f"{APP_NAME} {APP_VERSION}")
    print(f"  {PLATFORM} · ffmpeg {'ok' if BIN['ffmpeg'] else 'MISSING'} · "
          f"yt-dlp {'ok' if BIN['yt-dlp'] else 'MISSING'}")
    menu = [
        ("1", "Convert a file", cli_convert),
        ("2", "Batch convert (folder / several files)", cli_batch),
        ("3", "Download from URL(s)", cli_download),
        ("4", "Tools — thumbnail, contact sheet, join, split, loudness…", cli_tools),
        ("5", "Inspect a file", None),
        ("6", "History", None),
        ("7", "Settings", None),
        ("8", "Doctor", None),
        ("9", "Update yt-dlp", None),
    ]
    while True:
        print()
        for key, label, _fn in menu:
            print(f"  {key}. {label}")
        print("  q. Quit")
        choice = ask("Choose")
        if choice in ("q", "quit", "exit"):
            return 0
        try:
            if choice in ("1", "2", "3", "4"):
                dict((k, fn) for k, _l, fn in menu)[choice](verbose)
            elif choice == "5":
                print(probe_details(ask_path("File")))
            elif choice == "6":
                cli_history()
            elif choice == "7":
                cli_settings()
            elif choice == "8":
                cli_doctor()
            elif choice == "9":
                run_jobs_cli([build_update_job()], True)
        except RuntimeError as exc:
            print(c(f"  !! {exc}", "31"))


def cli_watch(folder, opts, outdir, pattern, hw, verbose=False, interval=3.0) -> int:
    """Convert every new media file that lands in a folder, until Ctrl+C."""
    folder = os.path.abspath(os.path.expanduser(folder))
    if not os.path.isdir(folder):
        print(c(f"  no such folder: {folder}", "31"))
        return 1
    head(f"Watching {folder}")
    print(c(f"  preset {opts.get('preset') or 'mp4'} → {outdir or 'same folder'} · "
            "Ctrl+C to stop", "90"))
    done = set(expand_inputs([folder]))  # leave what's already there alone
    sizes = {}
    try:
        while True:
            for path in expand_inputs([folder]):
                if path in done:
                    continue
                try:
                    st = os.stat(path)
                except OSError:
                    continue
                sig = (st.st_size, st.st_mtime)
                if sizes.get(path) != sig:  # still being copied — wait a round
                    sizes[path] = sig
                    continue
                done.add(path)
                jobs, errors = jobs_from_convert([path], opts, outdir, pattern, hw)
                for job in jobs:
                    done.add(job.target)
                run_jobs_cli(jobs, verbose, errors=errors)
            time.sleep(interval)
    except KeyboardInterrupt:
        print()
        return 0


# --------------------------------------------------------------------------
# toolkit availability
# --------------------------------------------------------------------------


def have_qt() -> bool:
    if IS_MOBILE:
        return False
    try:
        import PySide6.QtWidgets  # noqa: F401

        return True
    except Exception:
        return False


def have_tk() -> bool:
    if IS_MOBILE:
        return False
    try:
        import tkinter  # noqa: F401

        return True
    except Exception:
        return False


def gui_preview(jobs) -> list[str]:
    out = []
    for job in jobs:
        out.append(f"# {job.title}")
        out += [f"#   ! {w}" for w in job.warnings]
        out += [quote_cmd(s.cmd) for s in job.steps]
        job.cleanup()
    return out


# --------------------------------------------------------------------------
# interface 2 — Tkinter
# --------------------------------------------------------------------------


def tk_main() -> int:
    import queue
    import tkinter as tk
    from tkinter import filedialog, messagebox, simpledialog, ttk

    root = tk.Tk()
    root.title(f"{APP_NAME} {APP_VERSION}")
    root.geometry("1000x900")
    root.minsize(760, 600)
    style = ttk.Style()
    try:
        style.theme_use("clam")
    except tk.TclError:
        pass
    style.configure("Accent.TButton", foreground="#151310", background="#ff6a00")
    style.map("Accent.TButton", background=[("active", "#ff8a3d"), ("disabled", "#bbb")])
    style.configure("Dim.TLabel", foreground="#777")

    msgq: "queue.Queue" = queue.Queue()
    jq = JobQueue()

    class TkForm:
        def __init__(self, parent, fields, columns=2):
            self.frame = ttk.Frame(parent)
            self.vars = {}
            r = col = 0
            for f in fields:
                if f.kind == "check":
                    continue
                ttk.Label(self.frame, text=f.label).grid(
                    row=r, column=col * 3, sticky="w", padx=(0 if col == 0 else 14, 4), pady=2)
                if f.kind == "choice":
                    var = tk.StringVar(value=f.default or "")
                    w = ttk.Combobox(self.frame, textvariable=var, state="readonly", width=14,
                                     values=[v for v, _ in f.options()])
                else:
                    var = tk.StringVar(value=f.default or "")
                    w = ttk.Entry(self.frame, textvariable=var, width=18)
                w.grid(row=r, column=col * 3 + 1, sticky="ew", pady=2)
                if f.kind in ("file", "dir", "save"):
                    ttk.Button(self.frame, text="…", width=3,
                               command=lambda f=f, var=var: self._browse(f, var)).grid(
                        row=r, column=col * 3 + 2, padx=(2, 0))
                self.vars[f.key] = var
                col += 1
                if col == columns:
                    col, r = 0, r + 1
            for i in range(columns):
                self.frame.columnconfigure(i * 3 + 1, weight=1)
            checks = [f for f in fields if f.kind == "check"]
            if checks:
                cf = ttk.Frame(self.frame)
                cf.grid(row=r + 1, column=0, columnspan=columns * 3, sticky="w", pady=(6, 0))
                for f in checks:
                    var = tk.BooleanVar(value=bool(f.default))
                    ttk.Checkbutton(cf, text=f.label, variable=var).pack(side="left",
                                                                         padx=(0, 14))
                    self.vars[f.key] = var

        @staticmethod
        def _browse(f, var):
            if f.kind == "dir":
                p = filedialog.askdirectory()
            elif f.kind == "save":
                p = filedialog.asksaveasfilename()
            else:
                p = filedialog.askopenfilename()
            if p:
                var.set(p)

        def get(self) -> dict:
            return {k: v.get() for k, v in self.vars.items()}

        def set(self, values: dict):
            for k, v in values.items():
                if k in self.vars:
                    try:
                        self.vars[k].set(v)
                    except tk.TclError:
                        pass

    def file_list(parent, height=5):
        frame = ttk.Frame(parent)
        lb = tk.Listbox(frame, height=height, selectmode="extended", activestyle="none")
        sb = ttk.Scrollbar(frame, orient="vertical", command=lb.yview)
        lb.configure(yscrollcommand=sb.set)
        lb.pack(side="left", fill="both", expand=True)
        sb.pack(side="left", fill="y")
        return frame, lb

    def add_to_list(lb, paths):
        have = set(lb.get(0, "end"))
        for p in paths:
            if p not in have:
                lb.insert("end", p)
                have.add(p)

    nb = ttk.Notebook(root)

    # ---- convert tab
    conv = ttk.Frame(nb, padding=10)
    nb.add(conv, text="Convert")
    box = ttk.LabelFrame(conv, text="Input files", padding=6)
    box.pack(fill="x")
    lf, inputs = file_list(box, 4)
    lf.pack(side="left", fill="both", expand=True)
    btns = ttk.Frame(box)
    btns.pack(side="left", fill="y", padx=(6, 0))
    probe_var = tk.StringVar()

    def add_files():
        add_to_list(inputs, filedialog.askopenfilenames(title="Choose media files"))
        show_probe()

    def add_folder():
        d = filedialog.askdirectory()
        if d:
            add_to_list(inputs, expand_inputs([d]))
            show_probe()

    def remove_sel():
        for i in reversed(inputs.curselection()):
            inputs.delete(i)

    def show_probe(*_):
        sel = inputs.curselection()
        path = inputs.get(sel[0]) if sel else (inputs.get(0) if inputs.size() else "")
        n = inputs.size()
        probe_var.set((probe_summary(path) if path else "")
                      + (f"   ({n} files)" if n > 1 else ""))

    for text, fn in (("Add files…", add_files), ("Add folder…", add_folder),
                     ("Remove", remove_sel), ("Clear", lambda: inputs.delete(0, "end"))):
        ttk.Button(btns, text=text, command=fn).pack(fill="x", pady=1)
    inputs.bind("<<ListboxSelect>>", show_probe)
    ttk.Label(conv, textvariable=probe_var, style="Dim.TLabel").pack(anchor="w", pady=(4, 6))

    row = ttk.Frame(conv)
    row.pack(fill="x")
    ttk.Label(row, text="Preset").grid(row=0, column=0, sticky="w")
    preset_names = [f"{k}  —  {v['label']}" for k, v in ALL_PRESETS.items()]
    preset_box = ttk.Combobox(row, state="readonly", values=preset_names)
    preset_box.grid(row=0, column=1, columnspan=4, sticky="ew", pady=2)
    preset_box.current(0)
    ttk.Label(row, text="Encoder").grid(row=1, column=0, sticky="w")
    hw_opts = hw_choices()
    hw_box = ttk.Combobox(row, state="readonly", values=[lbl for _, lbl in hw_opts])
    hw_keys = [k for k, _ in hw_opts]
    hw_box.current(hw_keys.index(SETTINGS.get("hw", "none"))
                   if SETTINGS.get("hw", "none") in hw_keys else 0)
    hw_box.grid(row=1, column=1, sticky="ew", pady=2)
    ttk.Label(row, text="Profile").grid(row=1, column=2, sticky="w", padx=(14, 4))
    profile_box = ttk.Combobox(row, state="readonly", width=16)
    profile_box.grid(row=1, column=3, sticky="ew")
    pbtns = ttk.Frame(row)
    pbtns.grid(row=1, column=4, sticky="w")
    ttk.Label(row, text="Output folder").grid(row=2, column=0, sticky="w")
    outdir_var = tk.StringVar(value=SETTINGS.get("outdir", ""))
    ttk.Entry(row, textvariable=outdir_var).grid(row=2, column=1, columnspan=3,
                                                 sticky="ew", pady=2)
    ttk.Button(row, text="Browse", command=lambda: outdir_var.set(
        filedialog.askdirectory() or outdir_var.get())).grid(row=2, column=4, sticky="w")
    ttk.Label(row, text="File name").grid(row=3, column=0, sticky="w")
    pattern_var = tk.StringVar(value="{name}" + SETTINGS.get("suffix", "_skj"))
    ttk.Entry(row, textvariable=pattern_var).grid(row=3, column=1, sticky="ew", pady=2)
    ttk.Label(row, text="{name} {preset} {date} — blank folder = next to source",
              style="Dim.TLabel").grid(row=3, column=2, columnspan=3, sticky="w", padx=(14, 0))
    row.columnconfigure(1, weight=1)
    row.columnconfigure(3, weight=1)

    basic = TkForm(ttk.LabelFrame(conv, text="Basics", padding=8),
                   [f for f in CONVERT_FIELDS if f.basic], 3)
    basic.frame.master.pack(fill="x", pady=(8, 4))
    basic.frame.pack(fill="x")
    adv_box = ttk.LabelFrame(conv, text="Filters, audio & extras", padding=8)
    adv = TkForm(adv_box, [f for f in CONVERT_FIELDS if not f.basic], 3)
    adv.frame.pack(fill="x")
    adv_shown = tk.BooleanVar(value=False)

    def toggle_adv():
        if adv_shown.get():
            adv_box.pack_forget()
            adv_btn.config(text="▸ More options")
        else:
            adv_box.pack(fill="x", pady=4)
            adv_btn.config(text="▾ Fewer options")
            # grow the window so the extra fields aren't clipped (Tk can't scroll a tab)
            root.update_idletasks()
            want = min(root.winfo_reqheight(), root.winfo_screenheight() - 80)
            if want > root.winfo_height():
                root.geometry(f"{root.winfo_width()}x{want}")
        adv_shown.set(not adv_shown.get())

    adv_btn = ttk.Button(conv, text="▸ More options", command=toggle_adv)
    adv_btn.pack(anchor="w", pady=2)

    def preset_key():
        return preset_box.get().split("  —  ")[0]

    def hw_key():
        return hw_keys[hw_box.current()] if hw_box.current() >= 0 else "none"

    def convert_values():
        return {**basic.get(), **adv.get(), "preset": preset_key()}

    def set_convert_values(d):
        if d.get("preset") in ALL_PRESETS:
            preset_box.current(list(ALL_PRESETS).index(d["preset"]))
        basic.set(d)
        adv.set(d)

    def reload_profiles():
        profile_box["values"] = sorted(SETTINGS.get("profiles", {}))

    def load_profile(*_):
        prof = SETTINGS.get("profiles", {}).get(profile_box.get())
        if prof:
            for form in (basic, adv):
                form.set({k: (False if isinstance(v, tk.BooleanVar) else "")
                          for k, v in form.vars.items()})
            set_convert_values(prof)
            append(f"loaded profile {profile_box.get()!r}")

    def save_profile():
        name = simpledialog.askstring("Save profile", "Profile name:", parent=root)
        if name:
            profs = dict(SETTINGS.get("profiles", {}))
            profs[name] = clean_opts(convert_values())
            SETTINGS.set("profiles", profs)
            reload_profiles()
            profile_box.set(name)

    def delete_profile():
        profs = dict(SETTINGS.get("profiles", {}))
        if profs.pop(profile_box.get(), None) is not None:
            SETTINGS.set("profiles", profs)
            profile_box.set("")
            reload_profiles()

    ttk.Button(pbtns, text="Save…", command=save_profile).pack(side="left", padx=(4, 0))
    ttk.Button(pbtns, text="Delete", command=delete_profile).pack(side="left", padx=2)
    profile_box.bind("<<ComboboxSelected>>", load_profile)
    reload_profiles()

    # ---- download tab
    dl = ttk.Frame(nb, padding=10)
    nb.add(dl, text="Download")
    ttk.Label(dl, text="Links (one per line)").pack(anchor="w")
    urls_txt = tk.Text(dl, height=4, wrap="none")
    urls_txt.pack(fill="x")
    frow = ttk.Frame(dl)
    frow.pack(fill="x", pady=6)
    ttk.Label(frow, text="Format").pack(side="left")
    fmt_box = ttk.Combobox(frow, state="readonly",
                           values=[f"{k}  —  {v['label']}" for k, v in DOWNLOAD_FORMATS.items()])
    fmt_box.current(0)
    fmt_box.pack(side="left", fill="x", expand=True, padx=6)
    dinfo_var = tk.StringVar()
    ttk.Button(frow, text="Fetch info", command=lambda: fetch_info()).pack(side="left")
    dform = TkForm(dl, DOWNLOAD_FIELDS, 2)
    dform.frame.pack(fill="x")
    dform.set({"outdir": SETTINGS.get("download_dir") or os.getcwd()})
    ttk.Label(dl, textvariable=dinfo_var, style="Dim.TLabel", justify="left").pack(
        anchor="w", pady=(8, 0))

    def fetch_info():
        urls = split_urls(urls_txt.get("1.0", "end"))
        if not urls:
            dinfo_var.set("paste a link first")
            return
        dinfo_var.set("asking yt-dlp…")
        vals = dform.get()

        def work():
            try:
                text = fetch_url_info(urls[0], bool(vals.get("playlist")),
                                      vals.get("cookies_browser") or None)
            except RuntimeError as exc:
                text = f"!! {exc}"
            msgq.put(("info", text))

        threading.Thread(target=work, daemon=True).start()

    # ---- tools tab
    tl = ttk.Frame(nb, padding=10)
    nb.add(tl, text="Tools")
    tool_keys = list(TOOLS)
    tool_box = ttk.Combobox(tl, state="readonly", values=[t["label"] for t in TOOLS.values()])
    tool_box.current(0)
    tool_box.pack(fill="x")
    tbox = ttk.LabelFrame(tl, text="Input file(s)", padding=6)
    tbox.pack(fill="x", pady=6)
    tlf, tinputs = file_list(tbox, 4)
    tlf.pack(side="left", fill="both", expand=True)
    tb = ttk.Frame(tbox)
    tb.pack(side="left", fill="y", padx=(6, 0))
    ttk.Button(tb, text="Add files…", command=lambda: add_to_list(
        tinputs, filedialog.askopenfilenames())).pack(fill="x", pady=1)
    ttk.Button(tb, text="Clear", command=lambda: tinputs.delete(0, "end")).pack(fill="x")
    tool_holder = ttk.Frame(tl)
    tool_holder.pack(fill="x")
    tool_form = {"form": None}

    def rebuild_tool(*_):
        for child in tool_holder.winfo_children():
            child.destroy()
        tool_form["form"] = TkForm(tool_holder, TOOLS[tool_keys[tool_box.current()]]["fields"], 2)
        tool_form["form"].frame.pack(fill="x")

    tool_box.bind("<<ComboboxSelected>>", rebuild_tool)
    rebuild_tool()

    # ---- queue tab
    qt_ = ttk.Frame(nb, padding=10)
    nb.add(qt_, text="Queue")
    qcols = ("status", "kind", "job", "output", "time")
    qtree = ttk.Treeview(qt_, columns=qcols, show="headings", height=10)
    for col, w in zip(qcols, (80, 70, 360, 260, 60)):
        qtree.heading(col, text=col.title())
        qtree.column(col, width=w, anchor="w", stretch=col in ("job", "output"))
    qtree.pack(fill="both", expand=True)
    qb = ttk.Frame(qt_)
    qb.pack(fill="x", pady=(6, 0))

    def refresh_queue():
        qtree.delete(*qtree.get_children())
        for j in jq.jobs:
            qtree.insert("", "end", iid=str(j.id), values=(
                j.status, j.kind, j.title, j.target,
                fmt_dur(j.took) if j.took is not None else ""))

    def remove_jobs():
        jq.remove({int(i) for i in qtree.selection()})
        refresh_queue()

    def open_selected(tree, rows):
        sel = tree.selection()
        if sel:
            target = rows(sel[0])
            if target:
                open_folder(target)

    ttk.Button(qb, text="Remove selected", command=remove_jobs).pack(side="left")
    ttk.Button(qb, text="Clear finished", command=lambda: (jq.clear_finished(),
                                                           refresh_queue())).pack(side="left",
                                                                                  padx=4)
    ttk.Button(qb, text="Open folder", command=lambda: open_selected(
        qtree, lambda i: next((j.target for j in jq.jobs if str(j.id) == i), ""))).pack(
        side="left")

    # ---- history tab
    ht = ttk.Frame(nb, padding=10)
    nb.add(ht, text="History")
    hcols = ("when", "status", "kind", "job", "result")
    htree = ttk.Treeview(ht, columns=hcols, show="headings", height=10)
    for col, w in zip(hcols, (110, 80, 70, 360, 300)):
        htree.heading(col, text=col.title())
        htree.column(col, width=w, anchor="w", stretch=col in ("job", "result"))
    htree.pack(fill="both", expand=True)
    hist_rows = {}

    def refresh_history():
        htree.delete(*htree.get_children())
        hist_rows.clear()
        for i, r in enumerate(load_history(300)):
            hist_rows[str(i)] = r
            htree.insert("", "end", iid=str(i), values=(
                time.strftime("%m-%d %H:%M", time.localtime(r.get("t", 0))),
                r.get("status"), r.get("kind"), r.get("title"), r.get("summary")))

    hb = ttk.Frame(ht)
    hb.pack(fill="x", pady=(6, 0))
    ttk.Button(hb, text="Refresh", command=refresh_history).pack(side="left")
    ttk.Button(hb, text="Open folder", command=lambda: open_selected(
        htree, lambda i: hist_rows.get(i, {}).get("target", ""))).pack(side="left", padx=4)
    ttk.Button(hb, text="Clear history", command=lambda: (clear_history(),
                                                          refresh_history())).pack(side="left")
    htree.bind("<Double-1>", lambda e: open_selected(
        htree, lambda i: hist_rows.get(i, {}).get("target", "")))
    refresh_history()

    # ---- settings tab
    st = ttk.Frame(nb, padding=10)
    nb.add(st, text="Settings")
    notify_var = tk.BooleanVar(value=SETTINGS.get("notify", True))
    over_var = tk.BooleanVar(value=SETTINGS.get("overwrite", False))
    ttk.Checkbutton(st, text="Notify when the queue finishes", variable=notify_var,
                    command=lambda: SETTINGS.set("notify", notify_var.get())).pack(anchor="w")
    ttk.Checkbutton(st, text="Overwrite existing files instead of adding (2), (3)…",
                    variable=over_var,
                    command=lambda: SETTINGS.set("overwrite", over_var.get())).pack(anchor="w")
    ttk.Label(st, text=f"Settings, profiles and history live in {CONFIG_DIR}",
              style="Dim.TLabel").pack(anchor="w", pady=(8, 8))
    sb_ = ttk.Frame(st)
    sb_.pack(anchor="w")

    # ---- log + controls
    # pinned to the bottom so the buttons stay visible when the tabs grow
    bottom = ttk.Frame(root, padding=(8, 0, 8, 8))
    bottom.pack(side="bottom", fill="x")
    nb.pack(fill="both", expand=True, padx=8, pady=(8, 4))
    bar = ttk.Progressbar(bottom, mode="determinate", maximum=100)
    bar.pack(fill="x", pady=(2, 2))
    status = ttk.Label(bottom, text="ready")
    status.pack(anchor="w")
    logf = ttk.Frame(bottom)
    logf.pack(fill="both", expand=True, pady=(4, 4))
    log = tk.Text(logf, height=8, wrap="none", bg="#111", fg="#ddd",
                  insertbackground="#ddd", font=("TkFixedFont", 9))
    lsb = ttk.Scrollbar(logf, orient="vertical", command=log.yview)
    log.configure(yscrollcommand=lsb.set)
    log.pack(side="left", fill="both", expand=True)
    lsb.pack(side="left", fill="y")
    log.tag_configure("err", foreground="#ff6b6b")
    log.tag_configure("ok", foreground="#5ad17a")
    log.tag_configure("dim", foreground="#888")
    log.tag_configure("warn", foreground="#ffc400")

    ctl = ttk.Frame(bottom)
    ctl.pack(fill="x")
    stop_btn = ttk.Button(ctl, text="Stop", command=jq.stop, state="disabled")
    stop_btn.pack(side="right", padx=4)
    run_btn = ttk.Button(ctl, text="Start", style="Accent.TButton")
    run_btn.pack(side="right")
    add_btn = ttk.Button(ctl, text="Add to queue")
    add_btn.pack(side="right", padx=4)
    prev_btn = ttk.Button(ctl, text="Show command")
    prev_btn.pack(side="right")

    def append(text, tag=None):
        if tag is None:
            tag = ("err" if text.startswith("!!") else "dim" if text.startswith("$ ")
                   else "warn" if text.startswith("! ") else None)
        log.insert("end", text + "\n", tag or ())
        if int(log.index("end-1c").split(".")[0]) > 5000:
            log.delete("1.0", "1000.0")
        log.see("end")

    def current_tab() -> str:
        return nb.tab(nb.select(), "text")

    def collect(preview=False) -> list[Job]:
        tab = current_tab()
        reserve = not preview
        if tab == "Convert":
            files = list(inputs.get(0, "end"))
            if not files:
                append("!! add some input files first")
                return []
            SETTINGS.set("hw", hw_key())
            jobs, errors = jobs_from_convert(files, convert_values(), outdir_var.get() or None,
                                             pattern_var.get() or None, hw_key(), reserve)
        elif tab == "Download":
            urls = split_urls(urls_txt.get("1.0", "end"))
            if not urls:
                append("!! paste a link first")
                return []
            jobs, errors = jobs_from_download(urls, {**dform.get(),
                                                     "format": fmt_box.get().split("  —  ")[0]})
        elif tab == "Tools":
            files = list(tinputs.get(0, "end"))
            tool = tool_keys[tool_box.current()]
            if tool == "inspect":
                for f in files or []:
                    append(probe_details(f))
                if not files:
                    append("!! add a file first")
                return []
            try:
                spec_multi = TOOLS[tool].get("multi")
                groups = [files] if spec_multi else [[f] for f in files]
                jobs = [build_tool_job(tool, g, tool_form["form"].get(), reserve=reserve)
                        for g in groups or [[]]]
                errors = []
            except RuntimeError as exc:
                jobs, errors = [], [str(exc)]
        else:
            append("!! open the Convert, Download or Tools tab to choose what to add")
            return []
        for e in errors:
            append(f"!! {e}")
        if preview:
            for ln in gui_preview(jobs):
                append(ln, "dim")
            return []
        for j in jobs:
            for w in j.warnings:
                append(f"! {w}")
            jq.add(j)
            append(f"+ queued: {j.title}", "ok")
        refresh_queue()
        return jobs

    def start():
        if jq.running:
            return
        if not jq.pending() and not collect():
            return
        run_btn.config(state="disabled")
        stop_btn.config(state="normal")
        bar["value"] = 0

        def work():
            done, failed = jq.run(lambda j, t: msgq.put(("line", t)),
                                  lambda j, p: msgq.put(("pct", (j.title, p))),
                                  lambda j: msgq.put(("state", j)))
            msgq.put(("finished", (done, failed)))

        threading.Thread(target=work, daemon=True).start()

    run_btn.config(command=start)
    add_btn.config(command=collect)
    prev_btn.config(command=lambda: collect(preview=True))
    ttk.Button(sb_, text="Run doctor", command=lambda: [append(
        f"{lbl:<16} {val}", "ok" if ok else "err" if ok is False else None)
        for lbl, val, ok in doctor_report()]).pack(side="left")
    ttk.Button(sb_, text="Update yt-dlp", command=lambda: (jq.add(build_update_job()),
                                                           refresh_queue(), start())).pack(
        side="left", padx=4)

    def pump():
        try:
            while True:
                kind, payload = msgq.get_nowait()
                if kind == "line":
                    append(payload)
                elif kind == "pct":
                    title, pr = payload
                    if pr.pct is not None:
                        bar["value"] = pr.pct
                    status.config(text=f"{title} — {pr.text()}")
                elif kind == "state":
                    job = payload
                    refresh_queue()
                    if job.status == "running":
                        append(f"▶ {job.title}", "ok")
                        bar["value"] = 0
                    elif job.status == "done":
                        append(f"✓ done in {fmt_dur(job.took)}"
                               + (f" — {job.summary}" if job.summary else ""), "ok")
                    elif job.status == "failed":
                        append(f"✗ failed (exit {job.code})", "err")
                    elif job.status == "cancelled":
                        append("cancelled", "warn")
                elif kind == "finished":
                    done, failed = payload
                    run_btn.config(state="normal")
                    stop_btn.config(state="disabled")
                    status.config(text=f"queue finished — {done} done, {failed} failed")
                    root.title(f"{APP_NAME} {APP_VERSION}")
                    refresh_history()
                    notify(APP_NAME, f"{done} done" + (f", {failed} failed" if failed else ""))
                    root.bell()
                elif kind == "info":
                    dinfo_var.set(payload)
        except queue.Empty:
            pass
        root.after(80, pump)

    last = SETTINGS.get("last_convert") or {}
    if last:
        set_convert_values(last)
    lastd = SETTINGS.get("last_download") or {}
    if lastd:
        dform.set(lastd)
        if lastd.get("format") in DOWNLOAD_FORMATS:
            fmt_box.current(list(DOWNLOAD_FORMATS).index(lastd["format"]))

    def on_close():
        if jq.running and not messagebox.askyesno(APP_NAME, "A job is running. Stop it and quit?"):
            return
        jq.stop()
        SETTINGS.data["last_convert"] = clean_opts(convert_values())
        SETTINGS.data["last_download"] = clean_opts(
            {**dform.get(), "format": fmt_box.get().split("  —  ")[0]})
        SETTINGS.data["outdir"] = outdir_var.get()
        SETTINGS.save()
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", on_close)
    append(f"{APP_NAME} {APP_VERSION} — {PLATFORM}")
    append(f"ffmpeg: {bin_label('ffmpeg') or 'MISSING — ' + install_hint('ffmpeg')}")
    append(f"yt-dlp: {bin_label('yt-dlp') or 'MISSING — ' + install_hint('yt-dlp')}")
    pump()
    root.mainloop()
    return 0


# --------------------------------------------------------------------------
# interface 3 — PySide6
# --------------------------------------------------------------------------

QT_STYLE = """
QWidget { font-size: 13px; }
QTabWidget::pane { border: 1px solid #3a3f46; border-radius: 8px; top: -1px; }
QTabBar::tab { background: #24272c; color: #9aa0a8; padding: 8px 16px; margin-right: 2px;
               border: 1px solid #3a3f46; border-bottom: none;
               border-top-left-radius: 8px; border-top-right-radius: 8px; }
QTabBar::tab:selected { background: #2b2f35; color: #e8e6e1; border-top: 2px solid #ff6a00; }
QLineEdit, QPlainTextEdit, QComboBox, QListWidget, QTableWidget {
    background: #24272c; border: 1px solid #3a3f46; border-radius: 6px; padding: 4px; }
QLineEdit:focus, QComboBox:focus, QPlainTextEdit:focus, QListWidget:focus {
    border-color: #ff6a00; }
QPushButton { background: #2b2f35; border: 1px solid #3a3f46; border-radius: 6px;
              padding: 6px 14px; }
QPushButton:hover { border-color: #ff6a00; }
QPushButton:disabled { color: #666; }
QPushButton#primary { background: #ff6a00; color: #151310; font-weight: 700; border: none; }
QPushButton#primary:hover { background: #ff8a3d; }
QPushButton#primary:disabled { background: #3a3f46; color: #888; }
QProgressBar { background: #24272c; border: 1px solid #3a3f46; border-radius: 6px;
               text-align: center; min-height: 18px; }
QProgressBar::chunk { background: #ff6a00; border-radius: 5px; }
QGroupBox { border: 1px solid #3a3f46; border-radius: 8px; margin-top: 14px; padding: 8px; }
QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 4px; color: #ff6a00; }
QHeaderView::section { background: #2b2f35; color: #9aa0a8; border: none; padding: 6px; }
QLabel#dim { color: #9aa0a8; }
QPlainTextEdit#log { background: #111316; font-family: "SF Mono", Menlo, Consolas, monospace;
                     font-size: 12px; }
QToolTip { background: #2b2f35; color: #e8e6e1; border: 1px solid #ff6a00; }
"""


def qt_main() -> int:
    from PySide6.QtCore import QObject, Qt, QUrl, Signal
    from PySide6.QtGui import QColor, QDesktopServices, QPalette
    from PySide6.QtWidgets import (
        QAbstractItemView, QApplication, QCheckBox, QComboBox, QFileDialog, QGridLayout,
        QGroupBox, QHBoxLayout, QHeaderView, QInputDialog, QLabel, QLineEdit, QListWidget,
        QListWidgetItem, QMainWindow, QMessageBox, QPlainTextEdit, QProgressBar,
        QPushButton, QScrollArea, QSplitter, QTableWidget, QTableWidgetItem, QTabWidget,
        QToolButton, QVBoxLayout, QWidget,
    )

    STATUS_COLORS = {"done": "#5ad17a", "failed": "#ff6b6b", "running": "#ff8a3d",
                     "cancelled": "#ffc400", "queued": "#9aa0a8"}

    class Bridge(QObject):
        line = Signal(int, str)
        progress = Signal(int, object)
        state = Signal(int)
        finished = Signal(int, int)
        info = Signal(str)

    class QtForm(QWidget):
        def __init__(self, fields, columns=2):
            super().__init__()
            self.widgets = {}
            grid = QGridLayout(self)
            grid.setContentsMargins(0, 0, 0, 0)
            r = col = 0
            checks = QHBoxLayout()
            for f in fields:
                if f.kind == "check":
                    cb = QCheckBox(f.label)
                    cb.setChecked(bool(f.default))
                    cb.setToolTip(f.tip)
                    checks.addWidget(cb)
                    self.widgets[f.key] = cb
                    continue
                lab = QLabel(f.label)
                lab.setToolTip(f.tip)
                if f.kind == "choice":
                    w = QComboBox()
                    for val, text in f.options():
                        w.addItem(text, val)
                    if f.default:
                        w.setCurrentIndex(max(0, w.findData(f.default)))
                    holder = w
                else:
                    w = QLineEdit(placeholderText=f.ph)
                    holder = w
                    if f.kind in ("file", "dir", "save"):
                        holder = QWidget()
                        hl = QHBoxLayout(holder)
                        hl.setContentsMargins(0, 0, 0, 0)
                        btn = QPushButton("…")
                        btn.setFixedWidth(34)
                        btn.clicked.connect(lambda _=False, f=f, w=w: self._browse(f, w))
                        hl.addWidget(w, 1)
                        hl.addWidget(btn)
                w.setToolTip(f.tip)
                grid.addWidget(lab, r, col * 2)
                grid.addWidget(holder, r, col * 2 + 1)
                self.widgets[f.key] = w
                col += 1
                if col == columns:
                    col, r = 0, r + 1
            for i in range(columns):
                grid.setColumnStretch(i * 2 + 1, 1)
            if checks.count():
                checks.addStretch(1)
                grid.addLayout(checks, r + 1, 0, 1, columns * 2)

        def _browse(self, f, w):
            if f.kind == "dir":
                p = QFileDialog.getExistingDirectory(self)
            elif f.kind == "save":
                p, _ = QFileDialog.getSaveFileName(self)
            else:
                p, _ = QFileDialog.getOpenFileName(self)
            if p:
                w.setText(p)

        def values(self) -> dict:
            out = {}
            for k, w in self.widgets.items():
                if isinstance(w, QCheckBox):
                    out[k] = w.isChecked()
                elif isinstance(w, QComboBox):
                    out[k] = w.currentData() or ""
                else:
                    out[k] = w.text()
            return out

        def set_values(self, d: dict, reset=False):
            for k, w in self.widgets.items():
                if k not in d and not reset:
                    continue
                v = d.get(k)
                if isinstance(w, QCheckBox):
                    w.setChecked(bool(v))
                elif isinstance(w, QComboBox):
                    w.setCurrentIndex(max(0, w.findData(v or "")))
                else:
                    w.setText("" if v is None else str(v))

    def file_list_widget():
        lw = QListWidget()
        lw.setSelectionMode(QAbstractItemView.ExtendedSelection)
        lw.setMinimumHeight(96)
        return lw

    def add_paths(lw, paths):
        have = {lw.item(i).data(Qt.UserRole) for i in range(lw.count())}
        for p in paths:
            if p and p not in have:
                item = QListWidgetItem(os.path.basename(p))
                item.setData(Qt.UserRole, p)
                item.setToolTip(p)
                lw.addItem(item)
                have.add(p)

    def paths_of(lw):
        return [lw.item(i).data(Qt.UserRole) for i in range(lw.count())]

    def table(headers):
        t = QTableWidget(0, len(headers))
        t.setHorizontalHeaderLabels(headers)
        t.verticalHeader().setVisible(False)
        t.setSelectionBehavior(QAbstractItemView.SelectRows)
        t.setEditTriggers(QAbstractItemView.NoEditTriggers)
        t.setAlternatingRowColors(False)
        t.horizontalHeader().setStretchLastSection(True)
        t.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
        return t

    class Window(QMainWindow):
        def __init__(self):
            super().__init__()
            self.setWindowTitle(f"{APP_NAME} {APP_VERSION}")
            self.resize(1060, 860)
            self.setAcceptDrops(True)
            self.queue = JobQueue()
            self.bridge = Bridge()
            self.bridge.line.connect(self.on_line)
            self.bridge.progress.connect(self.on_progress)
            self.bridge.state.connect(self.on_state)
            self.bridge.finished.connect(self.on_finished)
            self.bridge.info.connect(lambda t: self.dinfo.setText(t))
            self.history_rows = []

            central = QWidget()
            self.setCentralWidget(central)
            outer = QVBoxLayout(central)
            ok = lambda k: "✓" if BIN[k] else "✗"  # noqa: E731
            header = QLabel(
                "<span style='font-size:20px;font-weight:800;letter-spacing:3px'>SKJ"
                "<span style='color:#ff6a00'>MEDIA</span></span>"
                f"&nbsp;&nbsp;<span style='color:#9aa0a8'>{APP_VERSION} · {PLATFORM} · "
                f"ffmpeg {ok('ffmpeg')} · yt-dlp {ok('yt-dlp')} · drop files anywhere</span>")
            outer.addWidget(header)

            split = QSplitter(Qt.Vertical)
            self.tabs = QTabWidget()
            self.tabs.addTab(self._scroll(self._convert_tab()), "Convert")
            self.tabs.addTab(self._scroll(self._download_tab()), "Download")
            self.tabs.addTab(self._scroll(self._tools_tab()), "Tools")
            self.tabs.addTab(self._queue_tab(), "Queue")
            self.tabs.addTab(self._history_tab(), "History")
            self.tabs.addTab(self._scroll(self._settings_tab()), "Settings")
            split.addWidget(self.tabs)

            bottom = QWidget()
            bl = QVBoxLayout(bottom)
            bl.setContentsMargins(0, 0, 0, 0)
            self.bar = QProgressBar()
            self.bar.setRange(0, 1000)
            self.bar.setFormat("")
            self.status = QLabel("ready")
            self.status.setObjectName("dim")
            self.log = QPlainTextEdit(readOnly=True)
            self.log.setObjectName("log")
            self.log.setMaximumBlockCount(5000)
            ctl = QHBoxLayout()
            ctl.addWidget(self.status, 1)
            self.preview_btn = QPushButton("Show command")
            self.preview_btn.clicked.connect(lambda: self.collect(preview=True))
            self.add_btn = QPushButton("Add to queue")
            self.add_btn.clicked.connect(lambda: self.collect())
            self.run_btn = QPushButton("Start")
            self.run_btn.setObjectName("primary")
            self.run_btn.clicked.connect(self.start)
            self.stop_btn = QPushButton("Stop", enabled=False)
            self.stop_btn.clicked.connect(self.queue.stop)
            for b in (self.preview_btn, self.add_btn, self.stop_btn, self.run_btn):
                ctl.addWidget(b)
            bl.addWidget(self.bar)
            bl.addLayout(ctl)
            bl.addWidget(self.log, 1)
            split.addWidget(bottom)
            split.setStretchFactor(0, 3)
            split.setStretchFactor(1, 2)
            outer.addWidget(split, 1)

            self.restore_state()
            self.append(f"{APP_NAME} {APP_VERSION} — {PLATFORM}")
            self.append(f"ffmpeg: {bin_label('ffmpeg') or 'MISSING — ' + install_hint('ffmpeg')}")
            self.append(f"yt-dlp: {bin_label('yt-dlp') or 'MISSING — ' + install_hint('yt-dlp')}")

        @staticmethod
        def _scroll(widget):
            area = QScrollArea()
            area.setWidgetResizable(True)
            area.setFrameShape(QScrollArea.NoFrame)
            area.setWidget(widget)
            return area

        # -- tabs
        def _convert_tab(self):
            page = QWidget()
            v = QVBoxLayout(page)
            box = QGroupBox("Input files — drag && drop works")
            bh = QHBoxLayout(box)
            self.inputs = file_list_widget()
            self.inputs.currentItemChanged.connect(lambda *_: self.show_probe())
            bh.addWidget(self.inputs, 1)
            col = QVBoxLayout()
            for text, fn in (("Add files…", self.add_files), ("Add folder…", self.add_folder),
                             ("Remove", lambda: [self.inputs.takeItem(self.inputs.row(i))
                                                 for i in self.inputs.selectedItems()]),
                             ("Clear", self.inputs.clear)):
                b = QPushButton(text)
                b.clicked.connect(fn)
                col.addWidget(b)
            col.addStretch(1)
            bh.addLayout(col)
            v.addWidget(box)
            self.probe = QLabel("")
            self.probe.setObjectName("dim")
            self.probe.setWordWrap(True)
            v.addWidget(self.probe)

            g = QGridLayout()
            self.preset = QComboBox()
            for k, p in ALL_PRESETS.items():
                self.preset.addItem(f"{k}  —  {p['label']}", k)
            self.hw = QComboBox()
            for k, label in hw_choices():
                self.hw.addItem(label, k)
            self.hw.setCurrentIndex(max(0, self.hw.findData(SETTINGS.get("hw", "none"))))
            self.profile = QComboBox()
            self.profile.setMinimumWidth(140)
            self.profile.activated.connect(lambda *_: self.load_profile())
            save_p = QPushButton("Save…")
            save_p.clicked.connect(self.save_profile)
            del_p = QPushButton("Delete")
            del_p.clicked.connect(self.delete_profile)
            self.outdir = QLineEdit(SETTINGS.get("outdir", ""),
                                    placeholderText="blank = next to each source file")
            browse = QPushButton("Browse")
            browse.clicked.connect(lambda: self.outdir.setText(
                QFileDialog.getExistingDirectory(self) or self.outdir.text()))
            self.pattern = QLineEdit("{name}" + SETTINGS.get("suffix", "_skj"))
            self.pattern.setToolTip("{name} = source name, {preset}, {date}")
            g.addWidget(QLabel("Preset"), 0, 0)
            g.addWidget(self.preset, 0, 1, 1, 5)
            g.addWidget(QLabel("Encoder"), 1, 0)
            g.addWidget(self.hw, 1, 1)
            g.addWidget(QLabel("Profile"), 1, 2)
            g.addWidget(self.profile, 1, 3)
            g.addWidget(save_p, 1, 4)
            g.addWidget(del_p, 1, 5)
            g.addWidget(QLabel("Output folder"), 2, 0)
            g.addWidget(self.outdir, 2, 1, 1, 4)
            g.addWidget(browse, 2, 5)
            g.addWidget(QLabel("File name"), 3, 0)
            g.addWidget(self.pattern, 3, 1)
            hint = QLabel("{name} {preset} {date} — extension is added for you")
            hint.setObjectName("dim")
            g.addWidget(hint, 3, 2, 1, 4)
            g.setColumnStretch(1, 2)
            g.setColumnStretch(3, 1)
            v.addLayout(g)

            basic_box = QGroupBox("Basics")
            QVBoxLayout(basic_box).addWidget(
                self._set(QtForm([f for f in CONVERT_FIELDS if f.basic], 3), "conv_basic"))
            v.addWidget(basic_box)
            self.adv_toggle = QToolButton(text="▸ More options — filters, audio, subtitles, "
                                               "watermark…", checkable=True)
            self.adv_toggle.setStyleSheet("QToolButton{border:none;color:#ff8a3d;padding:4px 0}")
            v.addWidget(self.adv_toggle)
            self.adv_box = QGroupBox("Filters, audio & extras")
            QVBoxLayout(self.adv_box).addWidget(
                self._set(QtForm([f for f in CONVERT_FIELDS if not f.basic], 3), "conv_adv"))
            self.adv_box.setVisible(False)
            self.adv_toggle.toggled.connect(self._toggle_adv)
            v.addWidget(self.adv_box)
            v.addStretch(1)
            self.reload_profiles()
            return page

        def _set(self, widget, name):
            setattr(self, name, widget)
            return widget

        def _toggle_adv(self, on):
            self.adv_box.setVisible(on)
            self.adv_toggle.setText(("▾ Fewer options" if on else
                                     "▸ More options — filters, audio, subtitles, watermark…"))

        def _download_tab(self):
            page = QWidget()
            v = QVBoxLayout(page)
            v.addWidget(QLabel("Links — one per line (or drop them here)"))
            self.urls = QPlainTextEdit(placeholderText="https://…")
            self.urls.setMaximumHeight(110)
            v.addWidget(self.urls)
            row = QHBoxLayout()
            row.addWidget(QLabel("Format"))
            self.dfmt = QComboBox()
            for k, f in DOWNLOAD_FORMATS.items():
                self.dfmt.addItem(f"{k}  —  {f['label']}", k)
            row.addWidget(self.dfmt, 1)
            info = QPushButton("Fetch info")
            info.clicked.connect(self.fetch_info)
            row.addWidget(info)
            v.addLayout(row)
            self.dl_form = QtForm(DOWNLOAD_FIELDS, 2)
            self.dl_form.set_values({"outdir": SETTINGS.get("download_dir") or os.getcwd()})
            box = QGroupBox("Options")
            QVBoxLayout(box).addWidget(self.dl_form)
            v.addWidget(box)
            self.dinfo = QLabel("")
            self.dinfo.setObjectName("dim")
            self.dinfo.setWordWrap(True)
            self.dinfo.setTextInteractionFlags(Qt.TextSelectableByMouse)
            v.addWidget(self.dinfo)
            v.addStretch(1)
            return page

        def _tools_tab(self):
            page = QWidget()
            v = QVBoxLayout(page)
            self.tool = QComboBox()
            for k, t in TOOLS.items():
                self.tool.addItem(t["label"], k)
            self.tool.currentIndexChanged.connect(lambda *_: self.rebuild_tool())
            v.addWidget(self.tool)
            box = QGroupBox("Input file(s) — for Join, the order here is the order in the file")
            bh = QHBoxLayout(box)
            self.tinputs = file_list_widget()
            self.tinputs.setDragDropMode(QAbstractItemView.InternalMove)
            bh.addWidget(self.tinputs, 1)
            col = QVBoxLayout()
            add = QPushButton("Add files…")
            add.clicked.connect(lambda: add_paths(
                self.tinputs, QFileDialog.getOpenFileNames(self, "Choose files")[0]))
            clear = QPushButton("Clear")
            clear.clicked.connect(self.tinputs.clear)
            col.addWidget(add)
            col.addWidget(clear)
            col.addStretch(1)
            bh.addLayout(col)
            v.addWidget(box)
            self.tool_box = QGroupBox("Settings")
            self.tool_layout = QVBoxLayout(self.tool_box)
            self.tool_form = None
            v.addWidget(self.tool_box)
            v.addStretch(1)
            self.rebuild_tool()
            return page

        def rebuild_tool(self):
            if self.tool_form is not None:
                self.tool_form.setParent(None)
                self.tool_form.deleteLater()
            fields = TOOLS[self.tool.currentData()]["fields"]
            self.tool_form = QtForm(fields, 2)
            self.tool_layout.addWidget(self.tool_form)
            self.tool_box.setVisible(bool(fields))

        def _queue_tab(self):
            page = QWidget()
            v = QVBoxLayout(page)
            self.qtable = table(["#", "Status", "Type", "Job", "Output", "Time", "Result"])
            self.qtable.setColumnWidth(0, 40)
            self.qtable.setColumnWidth(3, 330)
            self.qtable.setColumnWidth(4, 220)
            v.addWidget(self.qtable, 1)
            row = QHBoxLayout()
            rm = QPushButton("Remove selected")
            rm.clicked.connect(self.remove_jobs)
            clr = QPushButton("Clear finished")
            clr.clicked.connect(lambda: (self.queue.clear_finished(), self.refresh_queue()))
            opn = QPushButton("Open output folder")
            opn.clicked.connect(self.open_queue_target)
            for b in (rm, clr, opn):
                row.addWidget(b)
            row.addStretch(1)
            v.addLayout(row)
            return page

        def _history_tab(self):
            page = QWidget()
            v = QVBoxLayout(page)
            self.htable = table(["When", "Status", "Type", "Job", "Took", "Result"])
            self.htable.setColumnWidth(3, 360)
            self.htable.cellDoubleClicked.connect(
                lambda r, _c: open_folder(self.history_rows[r].get("target", ""))
                if r < len(self.history_rows) else None)
            v.addWidget(self.htable, 1)
            row = QHBoxLayout()
            ref = QPushButton("Refresh")
            ref.clicked.connect(self.refresh_history)
            clr = QPushButton("Clear history")
            clr.clicked.connect(lambda: (clear_history(), self.refresh_history()))
            row.addWidget(ref)
            row.addWidget(clr)
            hint = QLabel("double-click a row to open its folder")
            hint.setObjectName("dim")
            row.addWidget(hint)
            row.addStretch(1)
            v.addLayout(row)
            self.refresh_history()
            return page

        def _settings_tab(self):
            page = QWidget()
            v = QVBoxLayout(page)
            self.notify_cb = QCheckBox("Notify when the queue finishes")
            self.notify_cb.setChecked(SETTINGS.get("notify", True))
            self.notify_cb.toggled.connect(lambda on: SETTINGS.set("notify", on))
            self.over_cb = QCheckBox("Overwrite existing files instead of adding (2), (3)…")
            self.over_cb.setChecked(SETTINGS.get("overwrite", False))
            self.over_cb.toggled.connect(lambda on: SETTINGS.set("overwrite", on))
            v.addWidget(self.notify_cb)
            v.addWidget(self.over_cb)
            where = QLabel(f"Settings, profiles and history live in {CONFIG_DIR}.\n"
                           "Add your own presets under \"presets\" in settings.json.")
            where.setObjectName("dim")
            v.addWidget(where)
            row = QHBoxLayout()
            doc = QPushButton("Run doctor")
            doc.clicked.connect(self.run_doctor)
            upd = QPushButton("Update yt-dlp")
            upd.clicked.connect(lambda: (self.queue.add(build_update_job()),
                                         self.refresh_queue(), self.start()))
            cfg = QPushButton("Open settings folder")
            cfg.clicked.connect(lambda: (os.makedirs(CONFIG_DIR, exist_ok=True),
                                         QDesktopServices.openUrl(QUrl.fromLocalFile(CONFIG_DIR))))
            for b in (doc, upd, cfg):
                row.addWidget(b)
            row.addStretch(1)
            v.addLayout(row)
            v.addStretch(1)
            return page

        # -- helpers
        def append(self, text, color=None):
            if color is None:
                color = ("#ff6b6b" if text.startswith(("!!", "✗")) else
                         "#777" if text.startswith(("$ ", "#")) else
                         "#ffc400" if text.startswith("! ") else
                         "#5ad17a" if text.startswith(("✓", "+", "▶")) else None)
            if color:
                esc = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                self.log.appendHtml(f"<span style='color:{color};white-space:pre'>{esc}</span>")
            else:
                self.log.appendPlainText(text)

        def add_files(self):
            paths, _ = QFileDialog.getOpenFileNames(self, "Choose media files")
            add_paths(self.inputs, paths)
            self.show_probe()

        def add_folder(self):
            d = QFileDialog.getExistingDirectory(self)
            if d:
                add_paths(self.inputs, expand_inputs([d]))
                self.show_probe()

        def show_probe(self):
            item = self.inputs.currentItem() or (self.inputs.item(0) if self.inputs.count()
                                                 else None)
            n = self.inputs.count()
            text = probe_summary(item.data(Qt.UserRole)) if item else ""
            self.probe.setText(text + (f"   ({n} files)" if n > 1 else ""))

        def convert_values(self):
            return {**self.conv_basic.values(), **self.conv_adv.values(),
                    "preset": self.preset.currentData()}

        def set_convert_values(self, d, reset=False):
            if d.get("preset") in ALL_PRESETS:
                self.preset.setCurrentIndex(self.preset.findData(d["preset"]))
            self.conv_basic.set_values(d, reset)
            self.conv_adv.set_values(d, reset)
            if any(d.get(f.key) for f in CONVERT_FIELDS if not f.basic and f.kind != "choice"):
                self.adv_toggle.setChecked(True)

        def reload_profiles(self):
            current = self.profile.currentText()
            self.profile.clear()
            self.profile.addItem("")
            for name in sorted(SETTINGS.get("profiles", {})):
                self.profile.addItem(name)
            self.profile.setCurrentText(current)

        def load_profile(self):
            prof = SETTINGS.get("profiles", {}).get(self.profile.currentText())
            if prof:
                self.set_convert_values(prof, reset=True)
                if prof.get("_hw"):
                    self.hw.setCurrentIndex(max(0, self.hw.findData(prof["_hw"])))
                self.append(f"loaded profile {self.profile.currentText()!r}")

        def save_profile(self):
            name, ok = QInputDialog.getText(self, "Save profile", "Profile name:",
                                            text=self.profile.currentText())
            if ok and name.strip():
                profs = dict(SETTINGS.get("profiles", {}))
                profs[name.strip()] = {**clean_opts(self.convert_values()),
                                       "_hw": self.hw.currentData()}
                SETTINGS.set("profiles", profs)
                self.reload_profiles()
                self.profile.setCurrentText(name.strip())

        def delete_profile(self):
            profs = dict(SETTINGS.get("profiles", {}))
            if profs.pop(self.profile.currentText(), None) is not None:
                SETTINGS.set("profiles", profs)
                self.profile.setCurrentText("")
                self.reload_profiles()

        def fetch_info(self):
            urls = split_urls(self.urls.toPlainText())
            if not urls:
                self.dinfo.setText("paste a link first")
                return
            self.dinfo.setText("asking yt-dlp…")
            vals = self.dl_form.values()

            def work():
                try:
                    text = fetch_url_info(urls[0], bool(vals.get("playlist")),
                                          vals.get("cookies_browser") or None)
                except RuntimeError as exc:
                    text = f"!! {exc}"
                self.bridge.info.emit(text)

            threading.Thread(target=work, daemon=True).start()

        def run_doctor(self):
            self.append("── doctor ──")
            for label, value, ok in doctor_report():
                self.append(f"{label:<16} {value}",
                            "#5ad17a" if ok else "#ff6b6b" if ok is False else None)

        def refresh_queue(self):
            t = self.qtable
            t.setRowCount(len(self.queue.jobs))
            for r, j in enumerate(self.queue.jobs):
                vals = [str(j.id), j.status, j.kind, j.title, j.target,
                        fmt_dur(j.took) if j.took is not None else "", j.summary]
                for col, val in enumerate(vals):
                    item = QTableWidgetItem(val)
                    item.setToolTip(val)
                    if col == 1:
                        item.setForeground(QColor(STATUS_COLORS.get(j.status, "#e8e6e1")))
                    t.setItem(r, col, item)

        def refresh_history(self):
            self.history_rows = load_history(300)
            t = self.htable
            t.setRowCount(len(self.history_rows))
            for r, h in enumerate(self.history_rows):
                vals = [time.strftime("%Y-%m-%d %H:%M", time.localtime(h.get("t", 0))),
                        h.get("status", ""), h.get("kind", ""), h.get("title", ""),
                        fmt_dur(h.get("took")), h.get("summary", "")]
                for col, val in enumerate(vals):
                    item = QTableWidgetItem(str(val))
                    item.setToolTip(str(val))
                    if col == 1:
                        item.setForeground(QColor(STATUS_COLORS.get(h.get("status"), "#e8e6e1")))
                    t.setItem(r, col, item)

        def selected_job_ids(self):
            rows = {i.row() for i in self.qtable.selectedItems()}
            return {self.queue.jobs[r].id for r in rows if r < len(self.queue.jobs)}

        def remove_jobs(self):
            self.queue.remove(self.selected_job_ids())
            self.refresh_queue()

        def open_queue_target(self):
            ids = self.selected_job_ids()
            for j in self.queue.jobs:
                if j.id in ids and j.target:
                    open_folder(j.target)
                    return

        # -- queue
        def collect(self, preview=False):
            tab = self.tabs.tabText(self.tabs.currentIndex())
            reserve = not preview
            if tab == "Convert":
                files = paths_of(self.inputs)
                if not files:
                    self.append("!! add some input files first (or drop them on the window)")
                    return []
                SETTINGS.set("hw", self.hw.currentData())
                jobs, errors = jobs_from_convert(files, self.convert_values(),
                                                 self.outdir.text() or None,
                                                 self.pattern.text() or None,
                                                 self.hw.currentData(), reserve)
            elif tab == "Download":
                urls = split_urls(self.urls.toPlainText())
                if not urls:
                    self.append("!! paste a link first")
                    return []
                jobs, errors = jobs_from_download(
                    urls, {**self.dl_form.values(), "format": self.dfmt.currentData()})
            elif tab == "Tools":
                files = paths_of(self.tinputs)
                tool = self.tool.currentData()
                if not files:
                    self.append("!! add a file first")
                    return []
                if tool == "inspect":
                    for f in files:
                        self.append(probe_details(f))
                    return []
                groups = [files] if TOOLS[tool].get("multi") else [[f] for f in files]
                jobs, errors = [], []
                for group in groups:
                    try:
                        jobs.append(build_tool_job(tool, group, self.tool_form.values(),
                                                   reserve=reserve))
                    except RuntimeError as exc:
                        errors.append(str(exc))
            else:
                self.append("!! open the Convert, Download or Tools tab to choose what to add")
                return []
            for e in errors:
                self.append(f"!! {e}")
            if preview:
                for ln in gui_preview(jobs):
                    self.append(ln)
                return []
            for j in jobs:
                for w in j.warnings:
                    self.append(f"! {w}")
                self.queue.add(j)
                self.append(f"+ queued: {j.title}")
            self.refresh_queue()
            return jobs

        def start(self):
            if self.queue.running:
                return
            if not self.queue.pending() and not self.collect():
                return
            self.run_btn.setEnabled(False)
            self.stop_btn.setEnabled(True)
            self.bar.setValue(0)
            b = self.bridge

            def work():
                done, failed = self.queue.run(lambda j, t: b.line.emit(j.id, t),
                                              lambda j, p: b.progress.emit(j.id, p),
                                              lambda j: b.state.emit(j.id))
                b.finished.emit(done, failed)

            threading.Thread(target=work, daemon=True).start()

        def job(self, job_id):
            return next((j for j in self.queue.jobs if j.id == job_id), None)

        def on_line(self, _job_id, text):
            self.append(text)

        def on_progress(self, job_id, pr):
            job = self.job(job_id)
            if pr.pct is not None:
                self.bar.setValue(int(pr.pct * 10))
                self.bar.setFormat(f"{pr.pct:.1f}%")
                self.setWindowTitle(f"{pr.pct:.0f}% — {APP_NAME}")
            left = len(self.queue.pending())
            more = f"   (+{left} queued)" if left else ""
            self.status.setText(f"{job.title if job else ''} — {pr.text()}{more}")

        def on_state(self, job_id):
            job = self.job(job_id)
            self.refresh_queue()
            if not job:
                return
            if job.status == "running":
                self.bar.setValue(0)
                self.append(f"▶ {job.title}")
            elif job.status == "done":
                self.append(f"✓ done in {fmt_dur(job.took)}"
                            + (f" — {job.summary}" if job.summary else ""))
            elif job.status == "failed":
                self.append(f"✗ failed (exit {job.code})")
            elif job.status == "cancelled":
                self.append("! cancelled")

        def on_finished(self, done, failed):
            self.run_btn.setEnabled(True)
            self.stop_btn.setEnabled(False)
            self.setWindowTitle(f"{APP_NAME} {APP_VERSION}")
            self.status.setText(f"queue finished — {done} done, {failed} failed")
            self.bar.setFormat("done" if not failed else f"{failed} failed")
            self.refresh_history()
            notify(APP_NAME, f"{done} done" + (f", {failed} failed" if failed else ""))
            QApplication.alert(self)

        # -- drag & drop
        def dragEnterEvent(self, e):
            if e.mimeData().hasUrls() or e.mimeData().hasText():
                e.acceptProposedAction()

        def dropEvent(self, e):
            files, links = [], []
            for url in e.mimeData().urls():
                if url.isLocalFile():
                    files.append(url.toLocalFile())
                elif url.scheme() in ("http", "https"):
                    links.append(url.toString())
            if not e.mimeData().urls() and e.mimeData().hasText():
                links = [u for u in split_urls(e.mimeData().text()) if u.startswith("http")]
            if links:
                self.tabs.setCurrentIndex(1)
                existing = self.urls.toPlainText().strip()
                self.urls.setPlainText("\n".join(([existing] if existing else []) + links))
            if files:
                expanded = expand_inputs(files)
                if self.tabs.tabText(self.tabs.currentIndex()) == "Tools":
                    add_paths(self.tinputs, expanded)
                else:
                    self.tabs.setCurrentIndex(0)
                    add_paths(self.inputs, expanded)
                    self.show_probe()

        # -- state
        def restore_state(self):
            last = SETTINGS.get("last_convert") or {}
            if last:
                self.set_convert_values(last)
            lastd = SETTINGS.get("last_download") or {}
            if lastd:
                self.dl_form.set_values(lastd)
                if lastd.get("format") in DOWNLOAD_FORMATS:
                    self.dfmt.setCurrentIndex(self.dfmt.findData(lastd["format"]))

        def save_state(self):
            SETTINGS.data["last_convert"] = clean_opts(self.convert_values())
            SETTINGS.data["last_download"] = clean_opts(
                {**self.dl_form.values(), "format": self.dfmt.currentData()})
            SETTINGS.data["outdir"] = self.outdir.text()
            SETTINGS.data["hw"] = self.hw.currentData()
            SETTINGS.save()

        def closeEvent(self, e):
            if self.queue.running:
                ans = QMessageBox.question(self, APP_NAME, "A job is running. Stop it and quit?")
                if ans != QMessageBox.Yes:
                    e.ignore()
                    return
                self.queue.stop()
            self.save_state()
            e.accept()

    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setStyle("Fusion")
    pal = QPalette()
    for role, color in ((QPalette.Window, "#1b1d21"), (QPalette.WindowText, "#e8e6e1"),
                        (QPalette.Base, "#24272c"), (QPalette.AlternateBase, "#2b2f35"),
                        (QPalette.Text, "#e8e6e1"), (QPalette.Button, "#2b2f35"),
                        (QPalette.ButtonText, "#e8e6e1"), (QPalette.Highlight, "#ff6a00"),
                        (QPalette.HighlightedText, "#151310"),
                        (QPalette.ToolTipBase, "#2b2f35"), (QPalette.ToolTipText, "#e8e6e1"),
                        (QPalette.PlaceholderText, "#6b717a")):
        pal.setColor(role, QColor(color))
    app.setPalette(pal)
    app.setStyleSheet(QT_STYLE)
    win = Window()
    win.show()
    return app.exec()


# --------------------------------------------------------------------------
# entry point
# --------------------------------------------------------------------------

EXAMPLES = """\
examples:
  %(prog)s --convert in.mkv out.mp4 --preset mp4 --height 1080
  %(prog)s --convert talk.mov clip.mp4 --start 12:00 --end 12:45 --target-size 10M
  %(prog)s --convert phone.mp4 short.mp4 --crop 9:16 --height 1920 --normalize
  %(prog)s --batch ~/Videos/*.mov --preset hevc --hw auto --outdir ~/Videos/small
  %(prog)s --download URL1 URL2 --format audio-mp3 --embed-thumbnail
  %(prog)s --download URL --section 1:00-2:30 --format 1080p
  %(prog)s --info URL
  %(prog)s --sheet movie.mkv --grid 5x6
  %(prog)s --join a.mp4 b.mp4 c.mp4 --output all.mp4
  %(prog)s --loudness podcast.wav
  %(prog)s --watch ~/Inbox --preset mp4-small --outdir ~/Done
  %(prog)s --convert in.mp4 out.webm --preset webm --dry-run     (just print the command)
"""


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="skj_media", formatter_class=argparse.RawDescriptionHelpFormatter,
        description=f"{APP_NAME} {APP_VERSION} — ffmpeg + yt-dlp, every device.",
        epilog=EXAMPLES)
    g = p.add_argument_group("interface")
    g.add_argument("--cli", action="store_true", help="force text mode")
    g.add_argument("--tk", action="store_true", help="force Tkinter")
    g.add_argument("--qt", action="store_true", help="force PySide6")
    g.add_argument("--doctor", action="store_true", help="check the environment")
    g.add_argument("--history", action="store_true", help="show recent jobs")
    g.add_argument("--update-ytdlp", action="store_true", help="update yt-dlp")
    g.add_argument("--verbose", "-v", action="store_true", help="full tool output")
    g.add_argument("--dry-run", action="store_true", help="print the commands, don't run")
    g.add_argument("--no-notify", action="store_true", help="no notification when done")

    g = p.add_argument_group("jobs")
    g.add_argument("--convert", nargs=2, metavar=("IN", "OUT"))
    g.add_argument("--batch", nargs="+", metavar="IN", help="files, folders or globs")
    g.add_argument("--download", nargs="+", metavar="URL")
    g.add_argument("--info", metavar="URL", help="show what's behind a link")
    g.add_argument("--inspect", metavar="FILE", help="full stream info")
    g.add_argument("--watch", metavar="DIR", help="convert new files as they appear")
    g.add_argument("--thumbnail", metavar="FILE")
    g.add_argument("--sheet", metavar="FILE", help="contact sheet")
    g.add_argument("--waveform", metavar="FILE")
    g.add_argument("--loudness", metavar="FILE", help="measure LUFS / true peak")
    g.add_argument("--join", nargs="+", metavar="FILE")
    g.add_argument("--split", metavar="FILE")
    g.add_argument("--extract-subs", metavar="FILE")

    g = p.add_argument_group("convert options")
    g.add_argument("--preset", choices=list(ALL_PRESETS))
    g.add_argument("--hw", choices=[k for k, _ in hw_choices()], help="GPU encoder")
    g.add_argument("--height", type=int)
    g.add_argument("--fps")
    g.add_argument("--crf", help="quality, lower = better")
    g.add_argument("--target-size", help="fit in this size, e.g. 25M")
    g.add_argument("--vbitrate", help="e.g. 6M")
    g.add_argument("--abitrate", help="e.g. 192k")
    g.add_argument("--start")
    g.add_argument("--end")
    g.add_argument("--duration")
    g.add_argument("--speed", help="0.5 = slow motion, 2 = double")
    g.add_argument("--crop", help="16:9, 9:16, square, W:H or W:H:X:Y")
    g.add_argument("--rotate", choices=["90", "180", "270"])
    g.add_argument("--flip", choices=["h", "v", "hv"])
    g.add_argument("--volume", help="dB, e.g. 3 or -6")
    g.add_argument("--fade-in", help="seconds")
    g.add_argument("--fade-out", help="seconds")
    g.add_argument("--denoise", action="store_true")
    g.add_argument("--deinterlace", action="store_true")
    g.add_argument("--normalize", action="store_true", help="EBU R128 loudness")
    g.add_argument("--burn-subs", metavar="FILE")
    g.add_argument("--watermark", metavar="IMAGE")
    g.add_argument("--watermark-pos", choices=list(WATERMARK_POS))
    g.add_argument("--audio-track", help="audio stream to keep, 0 = first")
    g.add_argument("--mute", action="store_true", help="drop the audio")
    g.add_argument("--strip-metadata", action="store_true")
    g.add_argument("--extra", help="extra args for ffmpeg / yt-dlp")
    g.add_argument("--outdir")
    g.add_argument("--pattern", help="output name, e.g. '{name}_small'")
    g.add_argument("--recursive", action="store_true", help="batch: include sub-folders")
    g.add_argument("--overwrite", action="store_true")
    g.add_argument("--profile", help="load saved options")
    g.add_argument("--save-profile", metavar="NAME", help="save these options")
    g.add_argument("--profiles", action="store_true", help="list saved profiles")

    g = p.add_argument_group("download options")
    g.add_argument("--format", choices=list(DOWNLOAD_FORMATS))
    g.add_argument("--playlist", action="store_true")
    g.add_argument("--items", help="playlist items, e.g. 1-10,15")
    g.add_argument("--subs", action="store_true")
    g.add_argument("--sub-langs")
    g.add_argument("--embed-thumbnail", action="store_true")
    g.add_argument("--no-metadata", action="store_true")
    g.add_argument("--sponsorblock", action="store_true")
    g.add_argument("--cookies-from", metavar="BROWSER")
    g.add_argument("--section", help="only this part, e.g. 1:00-2:30")
    g.add_argument("--limit-rate", help="e.g. 2M")
    g.add_argument("--fragments", help="parallel fragments")
    g.add_argument("--archive", action="store_true", help="skip already downloaded")
    g.add_argument("--split-chapters", action="store_true")
    g.add_argument("--template", help="yt-dlp output template")

    g = p.add_argument_group("tool options")
    g.add_argument("--at", help="thumbnail time")
    g.add_argument("--grid", default="4x4", help="contact sheet COLSxROWS")
    g.add_argument("--width", help="thumbnail / sheet / waveform width")
    g.add_argument("--every", help="split chunk length, e.g. 10:00")
    g.add_argument("--track", help="subtitle track for --extract-subs")
    g.add_argument("--output", help="output file for --join")
    g.add_argument("--mode", choices=["copy", "mp4"], help="--join: copy or re-encode")
    return p


CONVERT_ARG_KEYS = ["preset", "height", "fps", "crf", "target_size", "vbitrate", "abitrate",
                    "start", "end", "duration", "speed", "crop", "rotate", "flip", "volume",
                    "fade_in", "fade_out", "denoise", "deinterlace", "normalize", "burn_subs",
                    "watermark", "watermark_pos", "audio_track", "mute", "strip_metadata",
                    "extra"]


def convert_opts_from_args(args) -> tuple[dict, str | None]:
    base = {}
    hw = args.hw
    if args.profile:
        prof = SETTINGS.get("profiles", {}).get(args.profile)
        if prof is None:
            raise RuntimeError(f"no profile called {args.profile!r} (see --profiles)")
        base = dict(prof)
        hw = hw or base.pop("_hw", None)
    for key in CONVERT_ARG_KEYS:
        val = getattr(args, key, None)
        if val not in (None, False, ""):
            base[key] = val
    base.setdefault("preset", "mp4")
    base.pop("_hw", None)
    if args.save_profile:
        profs = dict(SETTINGS.get("profiles", {}))
        profs[args.save_profile] = {**base, **({"_hw": hw} if hw else {})}
        SETTINGS.set("profiles", profs)
        print(c(f"  saved profile {args.save_profile!r}", "32"))
    return base, hw


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if args.no_notify:
        SETTINGS.data["notify"] = False
    if args.overwrite:
        SETTINGS.data["overwrite"] = True

    if args.doctor:
        cli_doctor()
        return 0
    if args.history:
        cli_history()
        return 0
    if args.profiles:
        for name, prof in sorted((SETTINGS.get("profiles") or {}).items()):
            print(f"  {c(name, '1')}: " + " ".join(f"{k}={v}" for k, v in prof.items()))
        return 0
    if args.update_ytdlp:
        return run_jobs_cli([build_update_job()], True)
    if args.inspect:
        print(probe_details(args.inspect))
        return 0
    if args.info:
        try:
            print(fetch_url_info(args.info, args.playlist, args.cookies_from))
            return 0
        except RuntimeError as exc:
            print(c(f"  !! {exc}", "31"))
            return 1

    try:
        # one-shot modes never open a window
        if args.convert or args.batch or args.watch:
            opts, hw = convert_opts_from_args(args)
            if args.convert:
                src, dst = args.convert
                job = build_convert_job(src, dst, opts, hw)
                return run_jobs_cli([job], args.verbose, args.dry_run)
            if args.batch:
                inputs = expand_inputs(args.batch, args.recursive)
                if not inputs:
                    print(c("  no media files found", "31"))
                    return 1
                jobs, errors = jobs_from_convert(inputs, opts, args.outdir, args.pattern, hw)
                return run_jobs_cli(jobs, args.verbose, args.dry_run, errors)
            return cli_watch(args.watch, opts, args.outdir, args.pattern, hw, args.verbose)

        if args.download:
            opts = {"format": args.format or "best", "outdir": args.outdir,
                    "playlist": args.playlist, "items": args.items, "subs": args.subs,
                    "sub_langs": args.sub_langs, "thumbnail": args.embed_thumbnail,
                    "metadata": not args.no_metadata, "sponsorblock": args.sponsorblock,
                    "cookies_browser": args.cookies_from, "section": args.section,
                    "limit_rate": args.limit_rate, "fragments": args.fragments,
                    "archive": args.archive, "split_chapters": args.split_chapters,
                    "template": args.template, "extra": args.extra}
            jobs, errors = jobs_from_download(args.download, opts)
            return run_jobs_cli(jobs, args.verbose, args.dry_run, errors)

        tool_args = [("thumbnail", args.thumbnail), ("sheet", args.sheet),
                     ("waveform", args.waveform), ("loudness", args.loudness),
                     ("join", args.join), ("split", args.split), ("subs", args.extract_subs)]
        for tool, value in tool_args:
            if not value:
                continue
            cols, _, rows = (args.grid or "4x4").lower().partition("x")
            params = {"at": args.at, "width": args.width, "cols": cols, "rows": rows,
                      "every": args.every, "track": args.track, "output": args.output,
                      "mode": args.mode}
            inputs = value if isinstance(value, list) else [value]
            job = build_tool_job(tool, inputs, params, args.outdir)
            return run_jobs_cli([job], args.verbose, args.dry_run)
    except RuntimeError as exc:
        print(c(f"  !! {exc}", "31"))
        return 1

    # interface selection
    if args.cli or IS_MOBILE:
        return cli_main(args.verbose)
    if args.qt:
        return qt_main()
    if args.tk:
        return tk_main()
    if have_qt():
        return qt_main()
    if have_tk():
        return tk_main()
    return cli_main(args.verbose)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print()
        sys.exit(130)

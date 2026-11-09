"""Pull one channel of audio, with handles, out of a source media file into a mono WAV."""
from __future__ import annotations

import json
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import wave
from typing import Dict, List, Optional, Tuple

EXTRA_BIN_DIRS = ["/opt/homebrew/bin", "/usr/local/bin", "/opt/local/bin"]


def find_tool(name: str) -> Optional[str]:
    bundled = os.environ.get("FCPXML2AAF_" + name.upper())  # set by the Mac app
    if bundled and os.path.exists(bundled):
        return bundled
    p = shutil.which(name)
    if p:
        return p
    for d in EXTRA_BIN_DIRS:
        c = os.path.join(d, name)
        if os.path.exists(c):
            return c
    return None


class MediaError(Exception):
    pass


_LAYOUTS = {"mono": 1, "stereo": 2, "2.1": 3, "3.0": 3, "quad": 4, "4.0": 4, "4.1": 5, "5.0": 5,
            "5.1": 6, "6.0": 6, "6.1": 7, "7.0": 7, "7.1": 8, "hexadecagonal": 16, "octagonal": 8}


def channels_from_desc(desc: str) -> int:
    """'pcm_s24le, 48000 Hz, 4 channels, s32' -> 4;  'aac (LC), 48000 Hz, stereo, fltp' -> 2"""
    import re
    m = re.search(r"(\d+) channels", desc)
    if m:
        return int(m.group(1))
    for part in desc.split(","):
        key = part.strip().split("(")[0].strip()
        if key in _LAYOUTS:
            return _LAYOUTS[key]
    return 2


class Relinker:
    """Find media that moved: look for the same filename under the given folders."""

    def __init__(self, roots: List[str]):
        self.roots = [r for r in roots if r and os.path.isdir(r)]
        self._index: Optional[Dict[str, str]] = None

    def find(self, path: str) -> Optional[str]:
        if os.path.exists(path):
            return path
        if not self.roots:
            return None
        if self._index is None:
            self._index = {}
            for r in self.roots:
                base_depth = r.rstrip(os.sep).count(os.sep)
                for dp, dn, fns in os.walk(r):
                    if dp.count(os.sep) - base_depth >= 4:
                        dn[:] = []  # don't crawl too deep
                    for fn in fns:
                        self._index.setdefault(fn.lower(), os.path.join(dp, fn))
        return self._index.get(os.path.basename(path).lower())


# ---------------- minimal WAV reader (PCM / extensible, any channel count) ----------------
class PCMWav:
    def __init__(self, path: str):
        self.path = path
        with open(path, "rb") as fh:
            riff, _size, wave_id = struct.unpack("<4sI4s", fh.read(12))
            if riff not in (b"RIFF",) or wave_id != b"WAVE":
                raise MediaError("not a RIFF/WAVE file")
            self.fmt = None
            self.data_offset = None
            self.data_size = None
            while True:
                hdr = fh.read(8)
                if len(hdr) < 8:
                    break
                cid, csize = struct.unpack("<4sI", hdr)
                if cid == b"fmt ":
                    raw = fh.read(csize)
                    tag, ch, rate, _bps, align, bits = struct.unpack("<HHIIHH", raw[:16])
                    if tag == 0xFFFE and len(raw) >= 26:
                        tag = struct.unpack("<H", raw[24:26])[0]
                    self.fmt = (tag, ch, rate, align, bits)
                elif cid == b"data":
                    self.data_offset = fh.tell()
                    self.data_size = csize
                    fh.seek(csize + (csize & 1), 1)
                else:
                    fh.seek(csize + (csize & 1), 1)
        if not self.fmt or self.data_offset is None:
            raise MediaError("WAV missing fmt/data")
        tag, self.channels, self.rate, self.align, self.bits = self.fmt
        if tag not in (1, 3) or (tag == 3 and self.bits not in (32, 64)):
            raise MediaError("unsupported WAV format (format %d, %d-bit)" % (tag, self.bits))
        self.is_float = tag == 3  # 32-bit float recorders (Zoom, Sound Devices…)
        self.width = self.bits // 8
        self.frames = self.data_size // self.align

    def extract_channel(self, channel: int, start_frame: int, n_frames: int, out_path: str) -> int:
        start_frame = max(0, start_frame)
        n_frames = max(0, min(n_frames, self.frames - start_frame))
        with open(self.path, "rb") as fh:
            fh.seek(self.data_offset + start_frame * self.align)
            data = fh.read(n_frames * self.align)
        w = self.width
        o = (channel - 1) * w
        out = bytearray(n_frames * w)
        for b in range(w):
            out[b::w] = data[o + b::self.align][:n_frames]
        if self.is_float:  # convert float samples to 24-bit integers, clipping at full scale
            import array
            fa = array.array("f" if w == 4 else "d")
            fa.frombytes(bytes(out))
            if sys.byteorder != "little":
                fa.byteswap()
            pcm = bytearray(len(fa) * 3)
            for i, v in enumerate(fa):
                x = int(round((1.0 if v > 1.0 else -1.0 if v < -1.0 else v) * 8388607.0))
                pcm[3 * i:3 * i + 3] = (x & 0xFFFFFF).to_bytes(3, "little")
            out, w = pcm, 3
        with wave.open(out_path, "wb") as ww:
            ww.setnchannels(1)
            ww.setsampwidth(w)
            ww.setframerate(self.rate)
            ww.writeframes(bytes(out))
        return n_frames


class Extractor:
    def __init__(self, sample_rate: int, bit_depth: int, workdir: str):
        self.rate = sample_rate
        self.bits = bit_depth
        self.workdir = workdir
        self.ffmpeg = find_tool("ffmpeg")
        self.ffprobe = find_tool("ffprobe")
        self.afconvert = find_tool("afconvert")
        self._probe: Dict[str, List[int]] = {}
        self._converted: Dict[str, str] = {}

    def describe(self) -> str:
        if self.ffmpeg:
            return "ffmpeg (%s)" % self.ffmpeg
        if self.afconvert:
            return "built-in Python + afconvert (no ffmpeg: video files like .mov/.mp4 may fail)"
        return "built-in Python (no ffmpeg: only WAV sources will work)"

    def describe_file(self, path: str) -> str:
        """One line about a source file's audio, for the report."""
        try:
            if self.ffmpeg:
                out = subprocess.run([self.ffmpeg, "-hide_banner", "-i", path], capture_output=True, text=True)
                streams = [l.split("Audio:", 1)[1].strip() for l in out.stderr.splitlines() if "Stream #" in l and "Audio:" in l]
                return " | ".join(streams) or "no audio streams"
            w = PCMWav(path)
            return "WAV %d ch, %d Hz, %d-bit%s" % (w.channels, w.rate, w.bits, " float" if w.is_float else "")
        except Exception as e:
            return "unreadable (%s)" % e

    # -------- ffmpeg path --------
    def _stream_channels(self, path: str) -> List[int]:
        """Channel count of each audio stream, in order."""
        if path in self._probe:
            return self._probe[path]
        chans: List[int] = []
        if self.ffprobe:
            out = subprocess.run([self.ffprobe, "-v", "error", "-select_streams", "a", "-show_entries",
                                  "stream=channels", "-of", "json", path], capture_output=True, text=True)
            try:
                chans = [s["channels"] for s in json.loads(out.stdout)["streams"]]
            except Exception:
                chans = []
        else:  # no ffprobe (the Mac app ships ffmpeg only): read `ffmpeg -i` output
            out = subprocess.run([self.ffmpeg, "-hide_banner", "-i", path], capture_output=True, text=True)
            for line in out.stderr.splitlines():
                if "Stream #" in line and "Audio:" in line:
                    chans.append(channels_from_desc(line.split("Audio:", 1)[1]))
        self._probe[path] = chans
        return chans

    def _ffmpeg_extract(self, path, channel, start_sec, dur_sec, out_path):
        streams = self._stream_channels(path)
        if not streams:
            raise MediaError("no audio streams found")
        total = sum(streams)
        if channel > total:
            raise MediaError("channel %d requested but file has %d" % (channel, total))
        if len(streams) > 1:
            fc = "".join("[0:a:%d]" % i for i in range(len(streams))) + \
                 "amerge=inputs=%d,pan=mono|c0=c%d[out]" % (len(streams), channel - 1)
        else:
            fc = "[0:a:0]pan=mono|c0=c%d[out]" % (channel - 1)
        codec = {16: "pcm_s16le", 24: "pcm_s24le", 32: "pcm_s32le"}[self.bits]
        cmd = [self.ffmpeg, "-nostdin", "-v", "error", "-y", "-ss", "%.6f" % start_sec, "-i", path,
               "-t", "%.6f" % dur_sec, "-filter_complex", fc, "-map", "[out]",
               "-ar", str(self.rate), "-c:a", codec, "-rf64", "never", out_path]
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0:
            raise MediaError("ffmpeg failed: %s" % r.stderr.strip()[-300:])

    # -------- no-ffmpeg path --------
    def _as_wav(self, path: str) -> PCMWav:
        try:
            w = PCMWav(path)
            if w.rate == self.rate:
                return w
        except (MediaError, struct.error):
            pass
        if path not in self._converted:
            if not self.afconvert:
                raise MediaError("needs ffmpeg to read this file (not a %d Hz PCM WAV)" % self.rate)
            tmp = os.path.join(self.workdir, "conv_%d.wav" % len(self._converted))
            r = subprocess.run([self.afconvert, "-f", "WAVE", "-d", "LEI%d@%d" % (self.bits, self.rate), path, tmp],
                               capture_output=True, text=True)
            if r.returncode != 0:
                raise MediaError("could not decode without ffmpeg (install ffmpeg): %s" % r.stderr.strip()[-200:])
            self._converted[path] = tmp
        return PCMWav(self._converted[path])

    def extract(self, path: str, channel: int, start_sec: float, dur_sec: float, out_path: str) -> int:
        """Returns number of sample frames written."""
        if self.ffmpeg:
            self._ffmpeg_extract(path, channel, start_sec, dur_sec, out_path)
        else:
            w = self._as_wav(path)
            if channel > w.channels:
                raise MediaError("channel %d requested but file has %d" % (channel, w.channels))
            w.extract_channel(channel, int(round(start_sec * w.rate)), int(round(dur_sec * w.rate)), out_path)
        with wave.open(out_path, "rb") as r:
            return r.getnframes()

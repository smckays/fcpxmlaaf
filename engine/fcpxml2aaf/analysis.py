"""Listen to the source audio: find clipping, silent channels and duplicate channels.

Each source file is decoded once with ffmpeg into short windows of level statistics per channel
(peak, RMS, flat factor). Checks then look only at the parts of each file the edit actually uses.
"""
from __future__ import annotations

import math
import os
import re
import shutil
import statistics
import subprocess
import tempfile
from typing import Dict, List, Optional

WIN = 0.25           # seconds per analysis window
CLIP_PEAK = -0.1     # dBFS: a window peaking at least this high...
CLIP_FLAT = 0.5      # ...with a flat-topped peak (consecutive samples at full scale) is clipping
SILENT_RMS = -70.0   # dBFS: a channel never louder than this is silent
DUP_STD = 0.12       # dB: channels whose level difference barely moves are copies
DUP_MIN_WINDOWS = 8


def analyze_file(path: str, ffmpeg: str, streams: List[int]) -> dict:
    """Window statistics for every channel of a file: {"win", "channels", "float", "peak", "rms", "flat"}."""
    if not streams:
        raise RuntimeError("no audio streams")
    tmp = tempfile.mkdtemp(prefix="fcpxml2aaf_an_")
    out = os.path.join(tmp, "stats.txt")
    pre = ("".join("[0:a:%d]" % i for i in range(len(streams))) + "amerge=inputs=%d," % len(streams)) if len(streams) > 1 else "[0:a:0]"
    # measure in floating point so 32-bit float peaks above full scale aren't clipped before they're measured
    fc = pre + "aresample=48000,aformat=sample_fmts=fltp,asetnsamples=n=%d:p=0,astats=metadata=1:reset=1,ametadata=print:file='%s'" % (int(48000 * WIN), out)
    try:
        r = subprocess.run([ffmpeg, "-nostdin", "-v", "error", "-i", path, "-filter_complex", fc, "-c:a", "pcm_f32le", "-f", "null", "-"],
                           capture_output=True, text=True)
        if r.returncode != 0 or not os.path.exists(out):
            raise RuntimeError((r.stderr or "ffmpeg failed").strip()[-200:])
        n = sum(streams)
        peak: List[List[float]] = [[] for _ in range(n)]
        rms: List[List[float]] = [[] for _ in range(n)]
        flat: List[List[float]] = [[] for _ in range(n)]
        keys = {"Peak_level": peak, "RMS_level": rms, "Flat_factor": flat}
        with open(out) as fh:
            for line in fh:
                m = re.match(r"lavfi\.astats\.(\d+)\.(Peak_level|RMS_level|Flat_factor)=(\S+)", line)
                if not m:
                    continue
                ch = int(m.group(1)) - 1
                if ch < n:
                    v = m.group(3)
                    keys[m.group(2)][ch].append(-200.0 if "inf" in v or v == "nan" else float(v))
        probe = subprocess.run([ffmpeg, "-hide_banner", "-i", path], capture_output=True, text=True).stderr
        is_float = bool(re.search(r"Audio: pcm_f(32|64)|, flt|, fltp|, dbl", probe))
        return {"win": WIN, "channels": n, "float": is_float, "peak": peak, "rms": rms, "flat": flat}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def residual_db(path: str, ffmpeg: str, streams: List[int], a: int, b: int, off_db: float, start: float, dur: float):
    """How much is left (dB, relative to ch b) after subtracting a scaled ch a from ch b. A copy leaves almost nothing."""
    pre = ("".join("[0:a:%d]" % i for i in range(len(streams))) + "amerge=inputs=%d," % len(streams)) if len(streams) > 1 else "[0:a:0]"
    g = 10 ** (off_db / 20.0)

    def mean(expr):
        r = subprocess.run([ffmpeg, "-nostdin", "-hide_banner", "-ss", "%.3f" % start, "-t", "%.3f" % dur, "-i", path,
                            "-filter_complex", pre + "aformat=sample_fmts=fltp,pan=mono|c0=" + expr + ",volumedetect",
                            "-f", "null", "-"], capture_output=True, text=True)
        m = re.search(r"mean_volume: (-?[\d.]+|-inf) dB", r.stderr)
        if not m:
            raise RuntimeError("volumedetect failed")
        return -200.0 if "inf" in m.group(1) else float(m.group(1))
    return mean("c%d-%.6f*c%d" % (b - 1, g, a - 1)) - mean("c%d" % (b - 1))


def headroom_db(res: dict, ev, ch: int) -> float:
    """dB a 32-bit float source must be lowered so the part this clip uses fits under full scale (0 if it already fits)."""
    if not res or not res.get("float") or ch > res["channels"]:
        return 0.0
    pk = res["peak"][ch - 1]
    top = max((pk[i] for i in _windows(ev, res) if i < len(pk)), default=-200.0)
    return float(math.ceil(top + 0.5)) if top > 0.05 else 0.0


def _windows(ev, res):
    """Window indexes of a file that an event plays."""
    w = res["win"]
    i0 = max(0, int(math.floor(float(ev.src_in) / w)))
    i1 = int(math.ceil(float(ev.src_in + ev.dur) / w))
    return range(i0, min(i1, len(res["peak"][0]) if res["peak"] and res["peak"][0] else 0))


def _tl(ev, res, i):
    """Timeline seconds of window i inside an event."""
    return float(ev.tl_in) + max(0.0, i * res["win"] - float(ev.src_in))


def _label(role):
    p = role.split(".")
    return p[0] + (" · " + ".".join(p[1:]) if len(p) > 1 else "")


def audio_checks(exported, results: Dict[str, dict], fmt_time, verify=None, headroom=True) -> (List[dict], List[str]):
    """exported: [(event, (group, channels))]. results: {asset path: analyze_file() result}.
    fmt_time(seconds) -> timecode string. Returns (issues, ok lines)."""
    issues, ok = [], []
    clip: Dict[tuple, list] = {}
    over: Dict[tuple, list] = {}
    loud: Dict[tuple, float] = {}
    seq: Dict[tuple, list] = {}     # (role, path, ch) -> rms per used window, in order
    roles_ch: Dict[str, set] = {}
    checked = set()
    for ev, (_g, chans) in exported:
        res = results.get(ev.asset.path)
        if not res:
            continue
        for ch in chans:
            if ch > res["channels"]:
                continue
            checked.add((ev.asset.path, ch))
            k = (ev.role, ev.asset.path, ch)
            roles_ch.setdefault(ev.role, set()).add(ch)
            pk, rm, fl = res["peak"][ch - 1], res["rms"][ch - 1], res["flat"][ch - 1]
            for i in _windows(ev, res):
                if i >= len(pk):
                    break
                seq.setdefault(k, []).append((ev, i, rm[i]))
                loud[(ev.role, ch)] = max(loud.get((ev.role, ch), -200.0), rm[i])
                if res["float"] and pk[i] > 0.05:
                    over.setdefault(k, []).append((_tl(ev, res, i), pk[i], ev))
                elif pk[i] >= CLIP_PEAK and fl[i] >= CLIP_FLAT:
                    clip.setdefault(k, []).append((_tl(ev, res, i), ev))

    # clipping in the original recording
    for (role, path, ch), hits in sorted(clip.items(), key=lambda x: x[1][0][0]):
        first = min(h[0] for h in hits)
        add_time = len(hits) * WIN
        issues.append({"id": "clip:%s:%s:%d" % (role, os.path.basename(path), ch), "level": "warn",
                       "title": "Distortion in %s, ch %d" % (os.path.basename(path), ch),
                       "detail": "The original recording clips for about %.1f s (%s), first at %s. The AAF can't repair it, "
                                 "but the mixer should know. Another take or a different mic channel may be clean." %
                                 (add_time, _label(role), fmt_time(first)),
                       "fix": {"label": "Show on timeline", "action": "show:%.3f" % first},
                       "alt": {"label": "OK", "action": "dismiss"}})
    # 32-bit float over full scale (fine in the file, clipped once written as 24-bit)
    for (role, path, ch), hits in over.items():
        top = max(h[1] for h in hits)
        first = min(h[0] for h in hits)
        att = math.ceil(top + 0.5)
        if headroom:
            issues.append({"id": "over:%s:%s:%d" % (role, os.path.basename(path), ch), "level": "info",
                           "title": "%s ch %d goes over full scale (handled)" % (os.path.basename(path), ch),
                           "detail": "This 32-bit float recording peaks %.1f dB above full scale, first at %s. To keep it intact in "
                                     "the 24-bit AAF, its audio is lowered %d dB and %d dB of clip gain is added back, so it plays at "
                                     "the original level without distortion." % (top, fmt_time(first), att, att),
                           "fix": {"label": "Show on timeline", "action": "show:%.3f" % first}})
        else:
            issues.append({"id": "over:%s:%s:%d" % (role, os.path.basename(path), ch), "level": "warn",
                           "title": "%s ch %d will distort in the AAF" % (os.path.basename(path), ch),
                           "detail": "This 32-bit float recording peaks %.1f dB above full scale, first at %s. It's intact in the "
                                     "file, but a 24-bit AAF clips it." % (top, fmt_time(first)),
                           "fix": {"label": "Keep it intact (lower it, add clip gain back)", "action": 'option:float_headroom=true'},
                           "alt": {"label": "Show on timeline", "action": "show:%.3f" % first}})
    # silent channels (dead mic, unused input)
    for (role, ch), lv in sorted(loud.items()):
        if lv < SILENT_RMS and len(roles_ch.get(role, ())) > 1:
            issues.append({"id": "silent:%s:%d" % (role, ch), "level": "warn",
                           "title": "Ch %d of %s is silent" % (ch, _label(role)),
                           "detail": "There's no signal on it anywhere this edit uses it: probably an unused input or a dead mic. "
                                     "It would arrive as an empty track.",
                           "fix": {"label": "Leave ch %d out" % ch, "action": "channels:%s|%d" % (role, ch)},
                           "alt": {"label": "Keep it", "action": "dismiss"}})
    # duplicate channels: level difference constant window after window
    silent = {(r, c) for (r, c), lv in loud.items() if lv < SILENT_RMS}
    for role, chs in roles_ch.items():
        chs = sorted(c for c in chs if (role, c) not in silent)
        for ai, a in enumerate(chs):
            for b in chs[ai + 1:]:
                da = {}
                for (r, p, c), rows in seq.items():
                    if r == role and c in (a, b):
                        for ev, i, v in rows:
                            da.setdefault((id(ev), p, i), {})[c] = v
                diffs = [d[b] - d[a] for d in da.values() if a in d and b in d and d[a] > -60 and d[b] > -60]
                if len(diffs) < DUP_MIN_WINDOWS or statistics.pstdev(diffs) > DUP_STD:
                    continue
                off = statistics.mean(diffs)
                # steady sounds (room tone, hum, noise) can look alike on levels alone: confirm on the waveform
                if verify:
                    sample = next(((p, rows[0][0]) for (r, p, c), rows in seq.items() if r == role and c == a and rows), None)
                    try:
                        left = verify(sample[0], a, b, off, float(sample[1].src_in), min(float(sample[1].dur), 20.0)) if sample else None
                    except Exception:
                        left = None
                    if left is None or left > -30:
                        continue
                drop = b if off <= 0.05 else a  # keep the louder (main) channel
                keep = a if drop == b else b
                if abs(off) < 0.2:
                    title, detail = ("Ch %d is a copy of ch %d (%s)" % (b, a, _label(role)),
                                     "They carry the same audio at the same level all the way through. The mixer only needs one.")
                else:
                    title = "Ch %d is a %+.0f dB safety copy of ch %d (%s)" % (drop, -abs(off), keep, _label(role))
                    detail = ("Recorders often keep a quieter backup of the same mic in case the main channel clips. "
                              "Keep it only if ch %d distorts somewhere." % keep)
                issues.append({"id": "dup:%s:%d:%d" % (role, a, b), "level": "warn", "title": title, "detail": detail,
                               "fix": {"label": "Leave ch %d out" % drop, "action": "channels:%s|%d" % (role, drop)},
                               "alt": {"label": "Keep both", "action": "dismiss"}})
    if checked:
        if not clip and not over:
            ok.append("No distortion in the %d source channel%s used" % (len(checked), "" if len(checked) == 1 else "s"))
    return issues, ok

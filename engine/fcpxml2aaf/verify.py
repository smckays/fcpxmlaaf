"""Check a finished AAF against what the converter meant to write.

Re-opens the file and confirms: track count, names and order; start timecode; every clip's position,
length and clip gain; and that the embedded audio at the start and end of every clip matches the
source file, sample for sample (allowing for ±2 samples of decoder alignment).
"""
from __future__ import annotations

import os
import shutil
import tempfile
import wave
from fractions import Fraction
from typing import Callable, List, Optional

import aaf2

PROBE_SEC = 0.1
TOLERANCE = 2      # least-significant bits of difference allowed between two decodes
MAX_SHIFT = 2      # samples of alignment slack


def _ints(data: bytes, width: int) -> List[int]:
    return [int.from_bytes(data[i:i + width], "little", signed=True) for i in range(0, len(data) - width + 1, width)]


def _match(emb: List[int], ref: List[int]):
    """(ok, shift) comparing embedded samples with reference samples, allowing a small alignment shift."""
    n = min(len(emb), len(ref)) - 2 * MAX_SHIFT
    if n <= 0:
        return True, 0
    for sh in sorted(range(-MAX_SHIFT, MAX_SHIFT + 1), key=abs):
        if all(abs(emb[MAX_SHIFT + sh + i] - ref[MAX_SHIFT + i]) <= TOLERANCE for i in range(n)):
            return True, sh
    return False, None


def _find_offset(emb: List[int], ref: List[int], search: int):
    """If the audio is right but misplaced, by how many samples? None if it isn't the same audio."""
    probe = ref[:480]
    if len(probe) < 480 or not any(probe):
        return None
    for off in range(-search, search + 1):
        st = MAX_SHIFT + off
        if 0 <= st and st + len(probe) <= len(emb) and all(abs(emb[st + i] - probe[i]) <= TOLERANCE for i in range(0, len(probe), 7)):
            return off
    return None


_WAVS = {}


def _direct_wav(path, sr):
    """A PCMWav reader when the source is integer PCM WAV at the session rate (else None)."""
    if path not in _WAVS:
        try:
            from .media import PCMWav
            w = PCMWav(path)
            _WAVS[path] = w if (not w.is_float and w.rate == sr and w.width in (2, 3, 4)) else None
        except Exception:
            _WAVS[path] = None
    return _WAVS[path]


def _gain_of(comp):
    """(kind, value) of a clip's gain: None, ("const", amp) or ("curve", [(t, amp)])."""
    if not isinstance(comp, aaf2.components.OperationGroup):
        return None
    p = list(comp.parameters)[0]
    if isinstance(p, aaf2.misc.ConstantValue):
        return ("const", float(p.value))
    return ("curve", [(float(cp.time), float(cp.value)) for cp in p["PointList"]])


def verify_aaf(aaf_path: str, plan: list, tc_frames: int, rate: Fraction, sr: int,
               extract: Callable, progress: Optional[Callable] = None, fmt_tc: Optional[Callable] = None) -> dict:
    """plan: [{"name", "items": [{"s", "length", "start", "src", "ch", "src_sec", "att", "curve", "clip"}]}].
    extract(path, ch, start_sec, dur_sec, out_wav, gain_db) -> frames written."""
    problems: List[str] = []
    passed: List[str] = []
    n_clips = sum(len(t["items"]) for t in plan)
    probes = matched = gains = 0
    work = tempfile.mkdtemp(prefix="fcpxml2aaf_verify_")
    fmt_tc = fmt_tc or (lambda units: "%.3f s" % float(Fraction(units) / rate))
    try:
        with aaf2.open(aaf_path, "r") as f:
            comp = next(f.content.toplevel())
            slots = [s for s in comp.slots if s.media_kind == "Sound"]
            tcs = [s for s in comp.slots if s.media_kind != "Sound"]
            # timecode
            if tcs and getattr(tcs[0].segment, "start", None) == tc_frames:
                passed.append("Start timecode is right")
            else:
                problems.append("The AAF's start timecode isn't the one planned.")
            # tracks
            names = [s.name for s in slots]
            want = [t["name"] for t in plan]
            if names == want:
                passed.append("%d track%s, names and order as planned" % (len(names), "" if len(names) == 1 else "s"))
            else:
                problems.append("Tracks differ from the plan: the AAF has %s; expected %s." % (", ".join(names), ", ".join(want)))
            done = 0
            for slot, pt in zip(slots, plan):
                pos, found = 0, []
                for c in slot.segment.components:
                    if isinstance(c, aaf2.components.Transition):
                        pos -= c.length
                        continue
                    if isinstance(c, aaf2.components.Filler):
                        pos += c.length
                        continue
                    sc = c.segments[0] if isinstance(c, aaf2.components.OperationGroup) else c
                    found.append((pos, c.length, sc, _gain_of(c)))
                    pos += c.length
                if len(found) != len(pt["items"]):
                    problems.append("%s has %d clips; expected %d." % (pt["name"], len(found), len(pt["items"])))
                for (p, ln, sc, g), it in zip(found, pt["items"]):
                    done += 1
                    if progress:
                        progress(done, n_clips)
                    where = "%s, '%s' at %s" % (pt["name"], it["clip"], fmt_tc(it["s"]))
                    if p != it["s"] or ln != it["length"]:
                        problems.append("%s: placed at %s for %d units; expected %s for %d." %
                                        (where, fmt_tc(p), ln, fmt_tc(it["s"]), it["length"]))
                        continue
                    if sc.start != it["start"]:
                        problems.append("%s: starts at the wrong point in its audio (%d vs %d)." % (where, sc.start, it["start"]))
                        continue
                    # clip gain
                    want_g = it["curve"]
                    if want_g is None and g is not None or want_g is not None and g is None:
                        problems.append("%s: clip gain %s but it %s." % (where, "is present" if g else "is missing",
                                                                        "shouldn't be" if g else "should be"))
                    elif want_g is not None:
                        if isinstance(want_g, (int, float)):
                            ok = g[0] == "const" and abs(g[1] - want_g) < 1e-3
                        else:
                            ok = g[0] == "curve" and len(g[1]) == len(want_g) and all(
                                abs(a[1] - b[1]) < 1e-3 and abs(a[0] - float(b[0])) < 1e-4 for a, b in zip(g[1], want_g))
                        if ok:
                            gains += 1
                        else:
                            problems.append("%s: clip gain differs from Final Cut's levels." % where)
                    # audio, sample for sample, at the start and the end of the clip
                    try:
                        smob = sc.mob.slots[0].segment.mob
                        desc = smob.descriptor
                        width = desc["QuantizationBits"].value // 8
                        stream = smob.essence.open("r")
                    except Exception as e:
                        problems.append("%s: embedded audio can't be read (%s)." % (where, e))
                        continue

                    def emb_read(first, count, stream=stream, width=width):
                        """count samples starting at sample `first` of the embedded audio (zeros before its start)."""
                        pad = max(0, -first)
                        stream.seek(max(0, first) * width)
                        return [0] * pad + _ints(stream.read(max(0, count - pad) * width), width)
                    clip_sec = Fraction(it["length"]) / rate
                    for k, off_sec in enumerate([Fraction(0), max(Fraction(0), clip_sec - Fraction(PROBE_SEC).limit_denominator(1000))]):
                        if k and clip_sec <= Fraction(2 * PROBE_SEC).limit_denominator(1000):
                            break
                        probes += 1
                        ref_wav = os.path.join(work, "ref.wav")
                        start = it["src_sec"] + off_sec
                        lead = Fraction(MAX_SHIFT, sr)
                        n_ref = int(round(PROBE_SEC * sr)) + 2 * MAX_SHIFT
                        direct = _direct_wav(it["src"], sr) if not it["att"] else None
                        try:
                            if direct is not None:
                                # plain PCM WAV at the session rate: read the original file directly, no decoder involved
                                ref = direct.read_channel(it["ch"], int(round(float(start * sr))) - MAX_SHIFT, n_ref)
                                rwidth = direct.width
                            else:
                                extract(it["src"], it["ch"], float(max(Fraction(0), start - lead)), PROBE_SEC + 2 * MAX_SHIFT / sr, ref_wav, -it["att"])
                                with wave.open(ref_wav, "rb") as w:
                                    ref = _ints(w.readframes(w.getnframes()), w.getsampwidth())
                                    rwidth = w.getsampwidth()
                                if start < lead:  # clip starts at the very top of the file: no lead-in to align against
                                    ref = [0] * int(round(float(lead - start) * sr)) + ref
                        except Exception as e:
                            problems.append("%s: couldn't re-read the source to compare (%s)." % (where, e))
                            break
                        if rwidth != width:
                            sh = 8 * (width - rwidth)
                            ref = [v << sh if sh > 0 else v >> -sh for v in ref]
                        e0 = int(round(float(Fraction(it["start"]) / rate * sr + off_sec * sr))) - MAX_SHIFT
                        emb = emb_read(e0, len(ref) + 2 * MAX_SHIFT)
                        ok, _ = _match(emb, ref[: len(ref)])
                        if ok:
                            matched += 1
                            continue
                        e1 = max(0, e0 - sr // 20)
                        wide = emb_read(e1, (e0 - e1) + len(ref) + sr // 20)
                        shift = _find_offset([0] * MAX_SHIFT + wide, ref[MAX_SHIFT:], sr // 20 + e0 - e1)
                        if shift is not None:
                            moved = abs(shift - (e0 - e1) - MAX_SHIFT)
                            ms = moved * 1000.0 / sr
                            problems.append("%s: audio is %d sample%s (%.1f ms) out of sync (%s of the clip)." %
                                            (where, moved, "" if moved == 1 else "s", ms, "start" if k == 0 else "end"))
                        else:
                            problems.append("%s: embedded audio doesn't match %s ch %d (%s of the clip)." %
                                            (where, os.path.basename(it["src"]), it["ch"], "start" if k == 0 else "end"))
                        break
    finally:
        shutil.rmtree(work, ignore_errors=True)
    placed_ok = n_clips - sum(1 for p in problems if "placed at" in p or "clips; expected" in p)
    if not any("placed at" in p or "clips; expected" in p or "wrong point" in p for p in problems):
        passed.append("%d clip%s at the planned positions and lengths" % (n_clips, "" if n_clips == 1 else "s"))
    if gains and not any("clip gain" in p for p in problems):
        passed.append("Clip gain on %d clip%s matches Final Cut's levels" % (gains, "" if gains == 1 else "s"))
    if probes:
        if matched == probes:
            passed.append("Audio matches the source files sample for sample (%d spots in %d clips)" % (probes, n_clips))
    return {"ok": not problems, "problems": problems, "passed": passed, "clips": n_clips, "probes": probes,
            "matched": matched, "placed_ok": placed_ok}

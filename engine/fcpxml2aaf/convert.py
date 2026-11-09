"""FCPXML -> AAF (embedded mono WAV media), driven by an optional preset.

Usage:
  python3 -m fcpxml2aaf  INPUT.fcpxml[d]  [--preset preset.json] [--out FOLDER]
                         [--project NAME] [--media-root FOLDER ...] [--scan] [--dry-run]
"""
from __future__ import annotations

import argparse
import datetime
import json
import math
import os
import re
import shutil
import sys
import tempfile
from fractions import Fraction

from . import __version__
from .levels import Effects, amplitude_curve
from .mapping import MonoEvent, build_tracks, is_crossfade, load_preset
from .media import Extractor, MediaError, Relinker
from .parser import Parser, scan, tc_string


def safe_name(s: str) -> str:
    return re.sub(r'[\\/:*?"<>|]+', "_", s).strip() or "Timeline"


def convert(input_path, preset_path=None, out_dir=None, project=None, media_roots=(), dry_run=False, log=print,
            progress=None, preset=None):
    """progress: optional callback(done, total). preset: a preset dict (instead of a file path)."""
    if preset is not None:
        import json as _json, tempfile as _tf
        with _tf.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
            _json.dump(preset, fh)
            preset_path = fh.name
    preset = load_preset(preset_path)
    opts = preset["options"]
    parser = Parser(input_path, project)
    tl = parser.parse()

    fps = 1 / tl.frame_duration
    sr = int(opts["sample_rate"])
    rate = fps if opts["edit_rate"] == "frame" else Fraction(sr)
    tracks, notes = build_tracks(tl.events, preset, rate)

    out_dir = out_dir or os.path.dirname(os.path.abspath(parser.path if not os.path.isdir(input_path) else input_path))
    os.makedirs(out_dir, exist_ok=True)
    base = safe_name(tl.name)
    aaf_path = os.path.join(out_dir, base + ".aaf")
    report_path = os.path.join(out_dir, base + " - AAF report.txt")

    report = []
    R = report.append
    R("FCPXML -> AAF converter v%s" % __version__)
    R("Run: %s" % datetime.datetime.now().strftime("%Y-%m-%d %H:%M"))
    R("Source: %s  (FCPXML %s)" % (os.path.basename(os.path.normpath(input_path)), tl.fcpxml_version))
    R("Timeline: %s" % tl.name)
    R("Frame rate: %.3f%s   Start TC: %s   Length: %s"
      % (float(fps), " DF" if tl.drop_frame else "", tc_string(tl.tc_start, tl.frame_duration, tl.drop_frame),
         tc_string(tl.duration, tl.frame_duration)))
    R("Preset: %s" % (preset.get("name") if preset_path else "none (default role mapping)"))
    R("Options: handles %ss, %d Hz / %d-bit, edit rate = %s, levels = %s, fades = %s"
      % (opts["handles_sec"], sr, opts["bit_depth"], opts["edit_rate"], opts.get("levels"), opts.get("fades")))
    R("")
    R("TRACKS")
    for tr in tracks:
        roles = sorted({m.ev.role for m in tr.items})
        R("  %-10s %3d clips   %s" % (tr.name, len(tr.items), ", ".join(roles) or "(empty)"))
    R("")

    if dry_run:
        R("(dry run — no media extracted, no AAF written)")
        for tr in tracks:
            for m in tr.items:
                ev = m.ev
                extra = []
                if ev.gain_points:
                    extra.append("gain %+.1fdB" % ev.gain_points[0][1] if len(ev.gain_points) == 1
                                 else "gain %+.1f..%+.1fdB (%d pts)" % (min(v for _, v in ev.gain_points),
                                                                       max(v for _, v in ev.gain_points), len(ev.gain_points)))
                if ev.fade_in:
                    extra.append("fade in %.2fs" % float(ev.fade_in))
                if ev.fade_out:
                    extra.append("fade out %.2fs" % float(ev.fade_out))
                R("  %-8s %s-%s  ch%d  %-22s src %7.3fs  %s" % (
                    tr.name, tc_string(ev.tl_in + tl.tc_start, tl.frame_duration),
                    tc_string(ev.tl_in + ev.dur + tl.tc_start, tl.frame_duration),
                    m.channel, ev.name[:22], float(ev.src_in), "  ".join(extra)))
        _finish(report, tl.warnings + notes, [], report_path, log)
        return None, report_path

    import aaf2  # vendored

    # Also look for moved media in the folder that holds the FCPXML.
    here = os.path.dirname(os.path.abspath(os.path.normpath(input_path)))
    relinker = Relinker(list(media_roots) + [here])
    work = tempfile.mkdtemp(prefix="fcpxml2aaf_")
    ex = Extractor(sr, int(opts["bit_depth"]), work)
    log("Audio engine: " + ex.describe())
    errors = []
    levels_mode = opts.get("levels", "clip_gain")
    fades_mode = opts.get("fades", "aaf")
    stats = {"gain_clips": 0, "fades": 0, "baked_fades": 0, "crossfades": 0}
    handle = Fraction(opts["handles_sec"]).limit_denominator(1000)
    total = sum(len(tr.items) for tr in tracks)
    done = 0

    try:
        with aaf2.open(aaf_path, "w") as f:
            comp = f.create.CompositionMob(tl.name)
            comp.usage = "Usage_TopLevel"
            f.content.mobs.append(comp)

            tl_len = int(math.ceil(tl.duration * rate)) if tl.duration else 0
            tc_slot = comp.create_timeline_slot(edit_rate=fps)
            tc_slot.name = "TC1"
            tc = f.create.Timecode(int(round(fps)), drop=tl.drop_frame, length=int(math.ceil(tl.duration * fps)) or 1)
            tc.start = int(round(tl.tc_start * fps))
            tc_slot.segment = tc

            fx = Effects(f)
            for tnum, tr in enumerate(tracks, 1):
                slot = comp.create_sound_slot(edit_rate=rate)
                slot.name = tr.name
                slot["PhysicalTrackNumber"].value = tnum
                seq = slot.segment
                placed = []
                last_end = 0
                for m in tr.items:
                    done += 1
                    ev = m.ev
                    s = int(round(ev.tl_in * rate))
                    length = int(round((ev.tl_in + ev.dur) * rate)) - s
                    xfade = 0
                    if s < last_end:
                        if fades_mode == "aaf" and placed and is_crossfade(MonoEvent(placed[-1][5], 0, ""), MonoEvent(ev, 0, ""), last_end, s, rate):
                            xfade = last_end - s  # shares the track through a crossfade
                        else:  # rounding overlap guard
                            length -= last_end - s
                            s = last_end
                    if length <= 0:
                        continue
                    src = relinker.find(ev.asset.path)
                    if not src:
                        errors.append("MISSING MEDIA  %s  (clip '%s')" % (ev.asset.path, ev.name))
                        continue
                    pre = int(math.floor(min(handle, ev.src_in) * rate))
                    start_sec = ev.src_in - Fraction(pre) / rate
                    want_sec = Fraction(pre + length) / rate + handle
                    wav = os.path.join(work, "c%05d.wav" % done)
                    try:
                        frames = ex.extract(src, m.channel, float(start_sec), float(want_sec), wav)
                    except MediaError as e:
                        errors.append("MEDIA ERROR  %s ch%d: %s" % (os.path.basename(src), m.channel, e))
                        continue
                    avail = int(math.floor(Fraction(frames, sr) * rate)) - pre
                    if avail < length:
                        if avail <= 0:
                            errors.append("Clip '%s' points past the end of its media; skipped." % ev.name)
                            continue
                        errors.append("Clip '%s' is longer than its media; shortened." % ev.name)
                        length = avail

                    label = ev.name if len(ev.channels) == 1 else "%s.A%d" % (ev.name, m.channel)
                    mm = f.create.MasterMob(label)
                    f.content.mobs.append(mm)
                    smob = f.create.SourceMob(label + ".PHYS")
                    f.content.mobs.append(smob)
                    sslot = smob.import_audio_essence(wav)  # file slot at sample rate
                    mslot = mm.create_timeline_slot(edit_rate=rate)
                    mslot.segment = smob.create_source_clip(sslot.slot_id, media_kind="sound")
                    mslot.segment.length = int(math.floor(Fraction(frames, sr) * rate))
                    mslot["PhysicalTrackNumber"].value = 1
                    os.remove(wav)
                    placed.append((s, length, mm, mslot.slot_id, pre, ev, xfade))
                    last_end = s + length
                    if done % 25 == 0:
                        log("  %d / %d clips" % (done, total))
                    if progress:
                        progress(done, total)

                # Lay the clips out with filler, clip gain, fades and crossfades.
                pos, owed = 0, 0
                for i, (s, length, mm, slot_id, pre, ev, xfade) in enumerate(placed):
                    nxt = placed[i + 1] if i + 1 < len(placed) else None
                    xfade_out = nxt[6] if nxt is not None else 0
                    fi = fo = 0
                    if fades_mode != "off":
                        fi = 0 if xfade else (max(1, int(round(ev.fade_in * rate))) if ev.fade_in else 0)
                        fo = 0 if xfade_out else (max(1, int(round(ev.fade_out * rate))) if ev.fade_out else 0)
                        if fi + fo > length:  # fades longer than the clip: scale them down
                            fi, fo = fi * length // (fi + fo), fo * length // (fi + fo)
                    as_trans_in = fi and fades_mode == "aaf" and not (i > 0 and s == pos)
                    as_trans_out = fo and fades_mode == "aaf" and not (nxt is not None and nxt[0] == s + length)
                    bake_in = fi if (fi and not as_trans_in) else 0
                    bake_out = fo if (fo and not as_trans_out) else 0
                    if fades_mode == "aaf" and (bake_in or bake_out):
                        stats["baked_fades"] += 1
                    component = mm.create_source_clip(slot_id=slot_id, start=pre, length=length)
                    curve = amplitude_curve(ev.gain_points if levels_mode != "off" else None, s, length, rate,
                                            bake_in, bake_out)
                    if curve is not None:
                        component = fx.gain(component, length, curve)
                        stats["gain_clips"] += 1

                    gap = s - pos
                    if xfade:
                        # cross-dissolve straight from the previous clip (AAF transitions overlap both sides)
                        seq.components.append(fx.fade(xfade))
                        stats["crossfades"] += 1
                    elif as_trans_in:
                        seq.components.append(f.create.Filler("sound", gap + owed + fi))
                        seq.components.append(fx.fade(fi))
                        stats["fades"] += 1
                    elif gap + owed > 0:
                        seq.components.append(f.create.Filler("sound", gap + owed))
                    seq.components.append(component)
                    owed = 0
                    if as_trans_out:
                        seq.components.append(fx.fade(fo))
                        stats["fades"] += 1
                        owed = fo
                    pos = s + length
                rest = max(tl_len - pos, 0) + owed
                if rest > 0:
                    seq.components.append(f.create.Filler("sound", rest))
    finally:
        shutil.rmtree(work, ignore_errors=True)

    R("Audio engine: " + ex.describe())
    R("")
    R("SOURCE AUDIO (file: channels used -> format)")
    used = {}
    for tr in tracks:
        for m in tr.items:
            used.setdefault(m.ev.asset.path, set()).add(m.channel)
    for pth in sorted(used):
        found = relinker.find(pth)
        R("  %s: ch %s -> %s" % (os.path.basename(pth), ",".join(map(str, sorted(used[pth]))),
                                 ex.describe_file(found) if found else "MISSING"))
    R("")
    R("AAF written: %s  (%.1f MB)" % (aaf_path, os.path.getsize(aaf_path) / 1e6))
    R("Levels: " + ("%d clips carry clip gain." % stats["gain_clips"] if levels_mode != "off"
                    else "left out (preset setting)."))
    if fades_mode == "aaf":
        R("Fades: %d fades and %d crossfades written as AAF transitions%s.  (%d cross-dissolves in the FCP timeline.)"
          % (stats["fades"], stats["crossfades"],
             (", %d drawn into clip gain where there was no room" % stats["baked_fades"]) if stats["baked_fades"] else "",
             parser.n_transitions))
    else:
        R("Fades: " + ("drawn into each clip's gain line (preset setting)." if fades_mode == "gain"
                       else "left out (preset setting)."))
    _finish(report, tl.warnings + notes, errors, report_path, log)
    return aaf_path, report_path


def _finish(report, warnings, errors, report_path, log):
    if warnings:
        report += ["", "NOTES / NOT YET SUPPORTED"] + ["  - " + w for w in warnings]
    if errors:
        report += ["", "PROBLEMS (%d)" % len(errors)] + ["  - " + e for e in errors]
    if not warnings and not errors:
        report += ["", "No problems found."]
    with open(report_path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(report) + "\n")
    log("\n".join(report))


def main(argv=None):
    ap = argparse.ArgumentParser(prog="fcpxml2aaf", description="Convert a Final Cut Pro FCPXML timeline to an AAF for audio post.")
    ap.add_argument("input", help=".fcpxml file or .fcpxmld bundle")
    ap.add_argument("--preset", help="preset .json from the mapping page")
    ap.add_argument("--out", help="output folder (default: next to the input)")
    ap.add_argument("--project", help="project name, if the FCPXML holds several")
    ap.add_argument("--media-root", action="append", default=[], help="folder to search for moved media (repeatable)")
    ap.add_argument("--scan", action="store_true", help="print roles/timeline info as JSON and exit")
    ap.add_argument("--dry-run", action="store_true", help="show the track layout without extracting audio")
    a = ap.parse_args(argv)
    try:
        if a.scan:
            print(json.dumps(scan(a.input, a.project), indent=2))
            return 0
        convert(a.input, a.preset, a.out, a.project, a.media_root, a.dry_run)
        return 0
    except Exception as e:  # friendly top-level error for testers
        print("\nERROR: %s" % e, file=sys.stderr)
        return 1

"""Preflight: look at an export before it happens and say what's wrong, in plain words, with a fix.

Each issue: {"id", "level": "error"|"warn"|"info", "title", "detail", "fix": {"label", "action"}?, "alt": {...}?}
"error" blocks export in the app until fixed or explicitly overridden. Actions are applied by the app:
  "option:<name>=<json value>"   change an export option
  "locate_media"                 ask for a folder with the missing media
  "dismiss"                      hide this issue for this session
"""
from __future__ import annotations

import json
import os
from fractions import Fraction
from typing import List

from .mapping import camera_overlaps, is_muted, route, routed_events
from .parser import tc_string


def _names(evs, n=3):
    seen = []
    for e in evs:
        if e.name not in seen:
            seen.append(e.name)
    more = len(seen) - n
    return ", ".join(seen[:n]) + (" and %d more" % more if more > 0 else "")


def _opt(name, value):
    return "option:%s=%s" % (name, json.dumps(value))


def run(tl, parser, preset: dict, tracks, notes, relinker) -> dict:
    opts = preset["options"]
    issues: List[dict] = []
    ok: List[str] = []
    add = issues.append

    all_routed = [(e, r) for e in tl.events for r in [route(e, preset)] if r]
    exported = routed_events(tl.events, preset)
    n_clips = sum(len(t.items) for t in tracks)

    # --- nothing to export
    if not n_clips:
        add({"id": "nothing", "level": "error", "title": "Nothing would be exported",
             "detail": "Every role is switched off or routed nowhere. Send at least one role to a track group."})

    # --- camera scratch audio alongside a separate recorder
    cam_mode = opts.get("camera_audio", "include")
    cams = camera_overlaps(all_routed)
    if cams and cam_mode == "include":
        add({"id": "camera_audio", "level": "error",
             "title": "Camera audio is going out alongside your sound recorder",
             "detail": "%d clip%s of camera audio (%s) %s at the same moments as separately recorded sound. "
                       "The mixer would get the camera's scratch mic on extra tracks, usually distorted and out of phase "
                       "with the real recording." % (len(cams), "" if len(cams) == 1 else "s", _names(cams),
                                                     "plays" if len(cams) == 1 else "play"),
             "fix": {"label": "Leave camera audio out where the recorder covers it", "action": _opt("camera_audio", "auto")},
             "alt": {"label": "Keep both", "action": _opt("camera_audio", "keep")},
             "roles": sorted({e.role for e in cams})})
    elif cams and cam_mode == "auto":
        ok.append("Camera audio left out for %d clip%s where the recorder covers it" % (len(cams), "" if len(cams) == 1 else "s"))
    elif cams and cam_mode == "keep":
        ok.append("Camera audio kept alongside the recorder (your choice)")
    lone_cam = [e for e, _ in exported if e.asset.has_video]
    if lone_cam and cam_mode != "exclude" and not (cams and cam_mode == "include"):
        add({"id": "camera_only", "level": "info", "title": "Some dialogue comes from camera audio",
             "detail": "%d clip%s (%s) %s no separate recording, so the camera's own audio is used." %
                       (len(lone_cam), "" if len(lone_cam) == 1 else "s", _names(lone_cam), "has" if len(lone_cam) == 1 else "have")})

    # --- offline media
    missing = sorted({e.asset.path for e, _ in exported if e.asset.path and not relinker.find(e.asset.path)})
    if missing:
        add({"id": "offline", "level": "error",
             "title": "%d media file%s offline" % (len(missing), "" if len(missing) == 1 else "s"),
             "detail": "Can't find %s%s. Plug in the drive it's on, or point to the folder it's in now." %
                       (os.path.basename(missing[0]), " and others" if len(missing) > 1 else ""),
             "fix": {"label": "Locate media…", "action": "locate_media"}})
    else:
        files = {e.asset.path for e, _ in exported}
        if files:
            ok.append("The media file is online" if len(files) == 1 else "All %d media files online" % len(files))

    # --- muted clips
    muted = [e for e, _ in exported if is_muted(e)]
    if muted:
        add({"id": "muted", "level": "warn",
             "title": "%d muted clip%s would be exported" % (len(muted), "" if len(muted) == 1 else "s"),
             "detail": "%s %s turned all the way down in Final Cut. They'd arrive silent, adding clutter to the mixer's tracks." %
                       (_names(muted), "is" if len(muted) == 1 else "are"),
             "fix": {"label": "Leave muted clips out", "action": _opt("skip_muted", True)},
             "alt": {"label": "Keep them", "action": "dismiss"}})

    # --- track template exceeded
    for n in notes:
        add({"id": "template:" + n.split(" ")[1], "level": "warn", "title": "More tracks than the template allows",
             "detail": n + " Overlapping clips need their own tracks. Raise the group's minimum, or route a role to another group.",
             "alt": {"label": "OK", "action": "dismiss"}})
    if not notes and preset.get("groups") and any(g.get("min_tracks") for g in preset["groups"]):
        ok.append("Fits the track template")

    # --- parser warnings (retimes, unresolved clips, …)
    for i, w in enumerate(parser.warnings):
        add({"id": "parse:%d" % i, "level": "warn", "title": "Not carried over", "detail": w,
             "alt": {"label": "OK", "action": "dismiss"}})

    # --- handles
    handle = Fraction(opts.get("handles_sec", 5)).limit_denominator(1000)
    short = [e for e, _ in exported if e.src_in < handle or
             (e.asset.duration and e.asset.duration - (e.src_in + e.dur) < handle)]
    if short and handle:
        add({"id": "handles", "level": "info", "title": "Shorter handles on %d clip%s" % (len(short), "" if len(short) == 1 else "s"),
             "detail": "%s run%s out of media before %s s. The mixer gets whatever is there." %
                       (_names(short), "s" if len(short) == 1 else "", opts.get("handles_sec", 5))})
    elif exported and handle:
        ok.append("Every clip has %s s of handles" % opts.get("handles_sec", 5))

    # --- sample rate conversion
    sr = int(opts.get("sample_rate", 48000))
    conv = sorted({os.path.basename(e.asset.path or "") for e, _ in exported if e.asset.rate and e.asset.rate != sr})
    if conv:
        add({"id": "rate", "level": "info", "title": "Sample rate conversion",
             "detail": "%s %s converted to %d kHz." % (", ".join(conv[:3]) + (" and others" if len(conv) > 3 else ""),
                                                       "is" if len(conv) == 1 else "are", sr // 1000)})

    # --- fades
    shaped = [e for e, _ in exported if (e.fade_in and e.fade_in_shape not in ("crossfade", "linear")) or
              (e.fade_out and e.fade_out_shape not in ("crossfade", "linear"))]
    if shaped and opts.get("fades", "aaf") != "off":
        add({"id": "fadeshape", "level": "info", "title": "Fade shapes become straight fades",
             "detail": "%d fade%s a curved shape in Final Cut; the AAF carries %s as linear fade%s of the same length." %
                       (len(shaped), " uses" if len(shaped) == 1 else "s use", "it" if len(shaped) == 1 else "them",
                        "" if len(shaped) == 1 else "s")})
    if parser.n_transitions:
        ok.append("1 cross dissolve becomes a crossfade" if parser.n_transitions == 1 else
                  "%d cross dissolves become crossfades" % parser.n_transitions)

    ok.append("Starts at %s, %.3f fps%s" % (tc_string(tl.tc_start, tl.frame_duration, tl.drop_frame),
                                             float(1 / tl.frame_duration), " drop-frame" if tl.drop_frame else ""))
    order = {"error": 0, "warn": 1, "info": 2}
    issues.sort(key=lambda i: order[i["level"]])
    return {"issues": issues, "ok": ok,
            "counts": {k: sum(1 for i in issues if i["level"] == k) for k in ("error", "warn", "info")}}

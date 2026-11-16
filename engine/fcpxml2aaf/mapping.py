"""Turn audio events into named tracks, following a preset (or sensible defaults).

Preset format (JSON) — this is what the mapping web page saves:

{
  "preset_version": 1,
  "name": "My Show - Mix Template",
  "groups": [                                   # track groups, in output order
    {"name": "DX", "min_tracks": 4},            # always create at least DX 1..DX 4
    {"name": "MX", "min_tracks": 2},
    {"name": "FX", "min_tracks": 2}
  ],
  "roles": {                                    # keys: "Main" or "Main.Subrole" (case-insensitive)
    "Dialogue":        {"group": "DX"},
    "Dialogue.Boom":   {"group": "DX", "channels": [1]},   # only use channel 1
    "Music":           {"group": "MX"},
    "Temp VO":         {"skip": true}
  },
  "options": {
    "handles_sec": 5,
    "sample_rate": 48000,
    "bit_depth": 24,
    "edit_rate": "frame",       # "frame" (Avid-style) or "sample"
    "unmapped": "by_main_role", # "by_main_role" | "by_subrole" | "skip"
    "levels": "clip_gain",      # "clip_gain" | "off"
    "fades": "aaf"              # "aaf" | "gain" | "off"
  }
}
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from typing import Dict, List, Optional

from .parser import AudioEvent, tc_string

DEFAULT_GROUP_NAMES = {"dialogue": "DX", "music": "MX", "effects": "FX"}

DEFAULT_OPTIONS = {
    "handles_sec": 5,
    "sample_rate": 48000,
    "bit_depth": 24,
    "edit_rate": "frame",
    "unmapped": "by_main_role",
    "levels": "clip_gain",   # "clip_gain" | "off"
    "fades": "aaf",          # "aaf" (real fades) | "gain" (draw fades into clip gain) | "off"
    "camera_audio": "include",  # "include" | "keep" (include, confirmed) | "auto" (drop where a recorder covers it) | "exclude"
    "skip_muted": False,     # leave out clips muted in Final Cut (-96 dB)
    "float_headroom": True,  # 32-bit float sources over full scale: lower the embedded audio, add the gain back as clip gain
}


@dataclass
class MonoEvent:
    """One channel of one clip, ready to place on a mono track."""
    ev: AudioEvent
    channel: int
    group: str
    pin: Optional[str] = None   # named track this channel always goes to


@dataclass
class Track:
    name: str
    group: str
    items: List[MonoEvent] = field(default_factory=list)
    named: bool = False         # a fixed, named track (e.g. "Boom") rather than an overflow track


def load_preset(path: Optional[str]) -> dict:
    preset = {"preset_version": 1, "name": "Default", "groups": [], "roles": {}, "options": {}}
    if path:
        with open(path, "r", encoding="utf-8") as fh:
            preset.update(json.load(fh))
    opts = dict(DEFAULT_OPTIONS)
    opts.update(preset.get("options") or {})
    preset["options"] = opts
    return preset


def _role_rule(preset: dict, role: str) -> Optional[dict]:
    roles = {k.lower(): v for k, v in (preset.get("roles") or {}).items()}
    if role.lower() in roles:
        return roles[role.lower()]
    main = role.split(".")[0].lower()
    return roles.get(main)


def _default_group(role: str, how: str) -> Optional[str]:
    main, _, sub = role.partition(".")
    if how == "skip":
        return None
    if how == "by_subrole" and sub:
        return sub
    return DEFAULT_GROUP_NAMES.get(main.lower(), main)


def is_crossfade(prev: MonoEvent, nxt: MonoEvent, prev_end: int, s: int, rate) -> bool:
    """True when two clips overlap only by a crossfade, so they can share a track."""
    if prev.ev.fade_out_shape != "crossfade" or nxt.ev.fade_in_shape != "crossfade":
        return False
    overlap = prev_end - s
    return overlap > 0 and abs(overlap - int(round(nxt.ev.fade_in * rate))) <= 1 \
        and abs(overlap - int(round(prev.ev.fade_out * rate))) <= 1


def route(ev: AudioEvent, preset: dict):
    """(group, channels) an event is sent to, or None when it isn't exported."""
    rule = _role_rule(preset, ev.role)
    if rule and rule.get("skip"):
        return None
    group = (rule or {}).get("group") or _default_group(ev.role, preset["options"]["unmapped"])
    if not group:
        return None
    chans = (rule or {}).get("channels")
    use = [c for c in ev.channels if (not chans or chans == "all" or c in chans)]
    return (group, use) if use else None


def is_muted(ev: AudioEvent) -> bool:
    return bool(ev.gain_points) and all(v <= -90 for _, v in ev.gain_points)


def _union(intervals):
    out = []
    for a, b in sorted(intervals):
        if out and a <= out[-1][1]:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return out


def covered_fraction(ev: AudioEvent, spans) -> float:
    """How much of ev's time is also covered by the (merged) spans."""
    a, b = ev.tl_in, ev.tl_in + ev.dur
    tot = sum(max(0, min(b, y) - max(a, x)) for x, y in spans)
    return float(tot / ev.dur) if ev.dur else 0.0


def camera_overlaps(routed):
    """Camera-audio events that play at the same time as separately recorded (audio-only) sound."""
    def main(e):
        return e.role.split(".")[0].lower()
    rec = {}
    for e, _ in routed:
        if not e.asset.has_video:
            rec.setdefault(main(e), []).append([e.tl_in, e.tl_in + e.dur])
    rec = {k: _union(v) for k, v in rec.items()}
    # only sound of the same kind counts: camera dialogue vs recorded dialogue, not vs the music
    return [e for e, _ in routed if e.asset.has_video and covered_fraction(e, rec.get(main(e), [])) >= 0.5]


def routed_events(events: List[AudioEvent], preset: dict):
    """Events that will be exported, after routing and the filter options. [(event, (group, channels))]"""
    opts = preset["options"]
    routed = [(ev, r) for ev in events for r in [route(ev, preset)] if r]
    if opts.get("skip_muted"):
        routed = [(e, r) for e, r in routed if not is_muted(e)]
    cam = opts.get("camera_audio", "include")
    if cam == "exclude":
        routed = [(e, r) for e, r in routed if not e.asset.has_video]
    elif cam == "auto":
        drop = {id(e) for e in camera_overlaps(routed)}
        routed = [(e, r) for e, r in routed if id(e) not in drop]
    return routed


def _nudged(ev: AudioEvent, sec):
    """Move a clip later (sec > 0) or earlier on the timeline; its audio stays the same."""
    if not sec:
        return ev
    tl_in, src_in, dur = ev.tl_in + sec, ev.src_in, ev.dur
    if tl_in < 0:  # pushed before the start: trim the head
        src_in, dur, tl_in = src_in - tl_in, dur + tl_in, 0
    gp = [(x + sec, v) for x, v in ev.gain_points] if ev.gain_points else ev.gain_points
    return replace(ev, tl_in=tl_in, src_in=src_in, dur=dur, gain_points=gp)


def _fmt_time(sec, tc):
    if tc:
        start, fd, drop = tc
        return tc_string(start + sec, fd, drop)
    return "%.2f s" % float(sec)


def build_tracks(events: List[AudioEvent], preset: dict, rate, frame_dur=None, tc=None) -> (List[Track], List[str]):
    """Lay routed clips onto tracks.
    rate: edit units per second (Fraction) used to decide overlaps.
    frame_dur: seconds per frame, needed for nudges. tc: (start, frame_dur, drop) for readable notes.
    Per group: named tracks first (pinned channels always land there), then numbered overflow tracks,
    filled first-free ("pack") or alternating ("checkerboard")."""
    notes: List[str] = []
    monos: List[MonoEvent] = []
    for ev, (group, use) in routed_events(events, preset):
        rule = _role_rule(preset, ev.role) or {}
        nf = int(rule.get("nudge_frames") or 0)
        if nf and frame_dur:
            ev = _nudged(ev, nf * frame_dur)
            if ev.dur <= 0:
                continue
        pins = {int(k): v for k, v in (rule.get("pins") or {}).items() if v}
        for c in use:
            monos.append(MonoEvent(ev=ev, channel=c, group=group, pin=pins.get(c)))

    share_xfades = preset["options"].get("fades", "aaf") == "aaf"
    cfgs = {g["name"]: g for g in preset.get("groups") or []}
    order = [g["name"] for g in preset.get("groups") or []]
    for m in monos:
        if m.group not in order:
            order.append(m.group)

    tracks: List[Track] = []
    for g in order:
        cfg = cfgs.get(g, {})
        named = []
        for n in cfg.get("tracks") or []:
            if n and n not in named:
                named.append(n)
        checker = cfg.get("layout") == "checkerboard"
        mine = sorted((m for m in monos if m.group == g), key=lambda m: (m.ev.tl_in, m.ev.role, m.channel))
        lanes: List[Track] = [Track(name=n, group=g, named=True) for n in named]
        ends: List[int] = [-1] * len(lanes)

        def add_lane():
            lanes.append(Track(name="%s %d" % (g, len(lanes) + 1), group=g))
            ends.append(-1)
            return len(lanes) - 1

        minimum = max(int(cfg.get("min_tracks", 0) or 0), len(named))
        while len(lanes) < minimum:
            add_lane()
        if checker and any(not (m.pin in named) for m in mine):
            while sum(1 for l in lanes if not l.named) < 2:
                add_lane()

        def fits(i, m, s):
            return ends[i] <= s or (share_xfades and lanes[i].items and is_crossfade(lanes[i].items[-1], m, ends[i], s, rate))

        last_auto = -1
        for m in mine:
            s = int(round(m.ev.tl_in * rate))
            e = int(round((m.ev.tl_in + m.ev.dur) * rate))
            target = None
            if m.pin in named:
                i = named.index(m.pin)
                if fits(i, m, s):
                    target = i
                else:
                    clash = lanes[i].items[-1].ev
                    prev = lanes[i].items[-1]
                    notes.append("Pinned track %s: %s (ch %d) and %s (ch %d) overlap at %s." %
                                 (m.pin, clash.name, prev.channel, m.ev.name, m.channel, _fmt_time(m.ev.tl_in, tc)))
            if target is None:
                autos = [i for i, l in enumerate(lanes) if not l.named]
                if checker and last_auto in autos:
                    k = autos.index(last_auto)
                    autos = autos[k + 1:] + autos[:k + 1]
                target = next((i for i in autos if fits(i, m, s)), None)
                if target is None:
                    target = add_lane()
                last_auto = target
            lanes[target].items.append(m)
            ends[target] = max(ends[target], e)
        if minimum and len(lanes) > minimum and not named:
            notes.append("Group %s needed %d tracks (preset asked for %d)." % (g, len(lanes), minimum))
        elif named and int(cfg.get("min_tracks", 0) or 0) and len(lanes) > int(cfg["min_tracks"]):
            notes.append("Group %s needed %d tracks (preset asked for %d)." % (g, len(lanes), int(cfg["min_tracks"])))
        tracks.extend(lanes)
    # de-duplicate notes, keep the last (highest) count per group
    seen = {}
    for n in notes:
        seen[n.split(" needed")[0] if n.startswith("Group ") else n] = n
    return tracks, list(seen.values())

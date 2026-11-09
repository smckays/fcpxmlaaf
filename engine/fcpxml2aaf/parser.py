"""Read an FCPXML (or .fcpxmld bundle) and flatten its magnetic timeline
into a flat list of audio events with absolute timeline positions.

Coordinate model
----------------
Every story element E sits in its parent's *local* time at [offset, offset+duration).
E's own local time t maps to parent-local time  offset + (t - start).
We carry `delta` = (timeline time) - (local time) down the tree, plus a visible
window [w0, w1) in timeline time, so nested clips get trimmed to what is
actually heard.
"""
from __future__ import annotations

import os
import urllib.parse
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from fractions import Fraction
from typing import Dict, List, Optional, Tuple


def t(value: Optional[str], default: Fraction = Fraction(0)) -> Fraction:
    """Parse FCPXML rational time like '1001/30000s', '10s', '0s'."""
    if value is None or value == "":
        return default
    v = value.strip()
    if v.endswith("s"):
        v = v[:-1]
    if "/" in v:
        n, d = v.split("/")
        return Fraction(int(n), int(d))
    return Fraction(v)


def pretty_role(raw: str) -> str:
    """'dialogue.Boom' -> 'Dialogue.Boom';  'effects' -> 'Effects'."""
    if not raw:
        return "Dialogue"
    main, _, sub = raw.partition(".")
    main = main[:1].upper() + main[1:]
    return main + ("." + sub if sub else "")


def parse_db(value: Optional[str]) -> float:
    """'-6dB' -> -6.0; '-inf' -> -144."""
    if not value:
        return 0.0
    v = value.strip().lower().replace("db", "").strip()
    if "inf" in v or "\u221e" in v:
        return -144.0
    try:
        return float(v)
    except ValueError:
        return 0.0


@dataclass(frozen=True)
class Mix:
    """Volume context inherited down the tree: gain envelopes and edge fades."""
    envs: tuple = ()   # each: tuple of (timeline_time or None, dB)
    fades: tuple = ()  # each: ("in"|"out", timeline_time_of_edge, length, shape)

    def add(self, env=None, fades=()):
        return Mix(self.envs + ((env,) if env else ()), self.fades + tuple(fades))


def env_at(env, x) -> float:
    if env[0][0] is None:
        return env[0][1]
    if x <= env[0][0]:
        return env[0][1]
    if x >= env[-1][0]:
        return env[-1][1]
    for (t0, v0), (t1, v1) in zip(env, env[1:]):
        if t0 <= x <= t1:
            return v1 if t1 == t0 else v0 + (v1 - v0) * float((x - t0) / (t1 - t0))
    return env[-1][1]


def combine_envelopes(envs, a, b, max_step_db=1.0):
    """Sum all envelopes (in dB) over [a, b]. Returns None (unity), [(a, dB)] (constant),
    or a list of points dense enough that straight lines in amplitude stay close to FCP's dB ramps."""
    if not envs:
        return None
    xs = {a, b}
    for e in envs:
        for x, _ in e:
            if x is not None and a < x < b:
                xs.add(x)
    xs = sorted(xs)
    pts = [(x, sum(env_at(e, x) for e in envs)) for x in xs]
    if all(abs(v - pts[0][1]) < 0.01 for _, v in pts):
        return None if abs(pts[0][1]) < 0.01 else [(a, pts[0][1])]
    dense = [pts[0]]
    for (x0, v0), (x1, v1) in zip(pts, pts[1:]):
        n = max(1, int(abs(v1 - v0) / max_step_db + 0.999))
        for k in range(1, n + 1):
            dense.append((x0 + (x1 - x0) * Fraction(k, n), v0 + (v1 - v0) * k / n))
    return dense


@dataclass
class Asset:
    id: str
    name: str
    path: Optional[str]
    start: Fraction
    duration: Fraction
    has_audio: bool
    channels: int
    rate: Optional[int]


@dataclass
class AudioEvent:
    role: str               # pretty role, e.g. "Dialogue.Boom"
    asset: Asset
    channels: List[int]     # 1-based source channels this event plays
    src_in: Fraction        # seconds from the start of the media file
    tl_in: Fraction         # seconds from the start of the timeline
    dur: Fraction
    name: str
    path_desc: str = ""     # where in the edit it came from (for the report)
    gain_points: Optional[List[Tuple[Fraction, float]]] = None  # (timeline time, dB); one point = constant
    fade_in: Fraction = Fraction(0)
    fade_out: Fraction = Fraction(0)
    fade_in_shape: str = ""
    fade_out_shape: str = ""


@dataclass
class Timeline:
    name: str
    frame_duration: Fraction
    tc_start: Fraction
    drop_frame: bool
    duration: Fraction
    audio_rate: int
    events: List[AudioEvent] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    fcpxml_version: str = ""


AUDIO_LEAVES = {"asset-clip", "audio"}
CONTAINERS = {"clip", "gap", "title", "video", "sync-clip", "spine", "ref-clip", "mc-clip"}
STORY = AUDIO_LEAVES | CONTAINERS | {"transition"}


def resolve_input(path: str) -> str:
    """Accept .fcpxml, .fcpxmld bundle folder, or a zip-extracted bundle."""
    if os.path.isdir(path):
        info = os.path.join(path, "Info.fcpxml")
        if os.path.exists(info):
            return info
        for f in os.listdir(path):
            if f.lower().endswith(".fcpxml"):
                return os.path.join(path, f)
        raise FileNotFoundError("No Info.fcpxml found inside %s" % path)
    return path


def url_to_path(src: Optional[str]) -> Optional[str]:
    if not src:
        return None
    p = urllib.parse.urlparse(src)
    if p.scheme in ("file", ""):
        return urllib.parse.unquote(p.path)
    return urllib.parse.unquote(src)


class Parser:
    def __init__(self, path: str, project_name: Optional[str] = None):
        self.path = resolve_input(path)
        self.root = ET.parse(self.path).getroot()
        self.assets: Dict[str, Asset] = {}
        self.media: Dict[str, ET.Element] = {}
        self.formats: Dict[str, ET.Element] = {}
        self.project_name = project_name
        self.warnings: List[str] = []
        self._warned = set()
        self.n_transitions = 0
        self.n_keyframed = 0
        self._read_resources()

    # ---------- resources ----------
    def _read_resources(self):
        res = self.root.find("resources")
        if res is None:
            return
        for el in res:
            if el.tag == "format":
                self.formats[el.get("id")] = el
            elif el.tag == "media":
                self.media[el.get("id")] = el
            elif el.tag == "asset":
                src = el.get("src")
                rep = el.find("media-rep")
                if rep is not None and rep.get("kind", "original-media") == "original-media":
                    src = rep.get("src") or src
                ch = el.get("audioChannels")
                rate = el.get("audioRate")
                self.assets[el.get("id")] = Asset(
                    id=el.get("id"),
                    name=el.get("name", el.get("id")),
                    path=url_to_path(src),
                    start=t(el.get("start")),
                    duration=t(el.get("duration")),
                    has_audio=el.get("hasAudio") == "1",
                    channels=int(ch) if ch else (2 if el.get("hasAudio") == "1" else 0),
                    rate=int(rate.rstrip("k")) * 1000 if rate and rate.endswith("k") else (int(rate) if rate and rate.isdigit() else None),
                )

    def warn(self, msg: str):
        if msg not in self._warned:
            self._warned.add(msg)
            self.warnings.append(msg)

    # ---------- projects ----------
    def project_names(self) -> List[str]:
        return [p.get("name", "Untitled") for p in self.root.iter("project")]

    def parse(self) -> Timeline:
        projects = list(self.root.iter("project"))
        if not projects:
            raise ValueError("No project (timeline) found in this FCPXML. Export a project, not just an event/library.")
        proj = projects[0]
        if self.project_name:
            match = [p for p in projects if p.get("name") == self.project_name]
            if not match:
                raise ValueError("Project %r not found. Available: %s" % (self.project_name, ", ".join(self.project_names())))
            proj = match[0]
        elif len(projects) > 1:
            self.warn("FCPXML contains %d projects; converting the first one (%s). Use --project to pick another."
                      % (len(projects), proj.get("name")))

        seq = proj.find("sequence")
        fmt = self.formats.get(seq.get("format"))
        frame_dur = t(fmt.get("frameDuration")) if fmt is not None and fmt.get("frameDuration") else Fraction(1001, 24000)
        tc_start = t(seq.get("tcStart"))
        rate = seq.get("audioRate", "48k")
        audio_rate = int(rate.rstrip("k")) * 1000 if rate.endswith("k") else int(rate)

        tl = Timeline(
            name=proj.get("name", "Untitled"),
            frame_duration=frame_dur,
            tc_start=tc_start,
            drop_frame=seq.get("tcFormat") == "DF",
            duration=t(seq.get("duration")),
            audio_rate=audio_rate,
            fcpxml_version=self.root.get("version", ""),
        )
        spine = seq.find("spine")
        if spine is not None:
            kids = [k for k in spine if k.tag in STORY]
            # Primary storyline offsets normally begin at tcStart.
            first = t(kids[0].get("offset")) if kids else Fraction(0)
            delta = -tc_start if (tc_start and first >= tc_start) else Fraction(0)
            end = tl.duration if tl.duration else Fraction(10 ** 7)
            self._walk_children(spine, delta, Fraction(0), end, tl.events, path=tl.name, inactive_roles=set())
        tl.events.sort(key=lambda e: (e.tl_in, e.role))
        tl.warnings = self.warnings
        return tl

    # ---------- traversal ----------
    def _transition_ext(self, parent) -> Dict[int, list]:
        """For a storyline, find cross-dissolves and work out how far the clips on
        either side must reach into the transition, plus the fades that make the crossfade.
        Values are in the storyline's local time: {id(el): [before, after, fade_in, fade_out]}
        where fade_* = (start_local, length)."""
        kids = [k for k in parent if k.tag in STORY and k.get("lane") is None]
        ext: Dict[int, list] = {}
        for i, k in enumerate(kids):
            if k.tag != "transition":
                continue
            prev = next((x for x in reversed(kids[:i]) if x.tag != "transition"), None)
            nxt = next((x for x in kids[i + 1:] if x.tag != "transition"), None)
            t0 = t(k.get("offset"))
            length = t(k.get("duration"))
            t1 = t0 + length
            if nxt is not None:
                cut = t(nxt.get("offset"))
            elif prev is not None:
                cut = t(prev.get("offset")) + t(prev.get("duration"))
            else:
                continue
            if prev is not None and prev.tag != "gap":
                e = ext.setdefault(id(prev), [Fraction(0), Fraction(0), None, None])
                e[1] = max(Fraction(0), t1 - cut)
                e[3] = (t0, length)
            if nxt is not None and nxt.tag != "gap":
                e = ext.setdefault(id(nxt), [Fraction(0), Fraction(0), None, None])
                e[0] = max(Fraction(0), cut - t0)
                e[2] = (t0, length)
            self.n_transitions += 1
        return ext

    @staticmethod
    def _merge_ext(a, b):
        if not a:
            return b
        if not b:
            return a
        return [max(a[0], b[0]), max(a[1], b[1]), a[2] or b[2], a[3] or b[3]]

    @staticmethod
    def _child_ext(k, ext, lo, hi):
        """When a container reaches into a crossfade, the clips inside it that sit on
        that edge must reach too."""
        if not ext or (not ext[0] and not ext[1]) or k.get("duration") is None:
            return None
        ko, kd = t(k.get("offset")), t(k.get("duration"))
        before = ext[0] if (ext[0] and ko <= lo < ko + kd) else Fraction(0)
        after = ext[1] if (ext[1] and hi is not None and ko < hi <= ko + kd) else Fraction(0)
        return [before, after, None, None] if (before or after) else None

    def _walk_children(self, parent, delta, w0, w1, out, path, inactive_roles, mix=None, ext=None, lo=None, hi=None):
        mix = mix or Mix()
        trans = self._transition_ext(parent)
        for el in parent:
            if el.tag in STORY:
                e = self._merge_ext(trans.get(id(el)), self._child_ext(el, ext, lo, hi) if lo is not None else None)
                self._walk(el, delta, w0, w1, out, path, inactive_roles, mix, e)

    def _walk(self, el, pdelta, w0, w1, out, path, inactive_roles, mix=None, ext=None):
        mix = mix or Mix()
        tag = el.tag
        if tag == "transition" or el.get("enabled") == "0":
            return

        offset = t(el.get("offset"))
        start = t(el.get("start"))
        if tag == "spine" and el.get("start") is None:
            kids = [k for k in el if k.tag in STORY]
            start = t(kids[0].get("offset")) if kids else Fraction(0)
        dur = t(el.get("duration")) if el.get("duration") else None
        before, after = (ext[0], ext[1]) if ext else (Fraction(0), Fraction(0))

        delta = pdelta + offset - start
        if dur is not None:
            a = max(w0, pdelta + offset - before)
            b = min(w1, pdelta + offset + dur + after)
        else:  # spine: no duration of its own
            a, b = max(w0, pdelta + offset), w1
        if b <= a:
            return
        name = el.get("name", tag)
        here = "%s > %s" % (path, name)

        if el.find("timeMap") is not None:
            self.warn("Retimed (speed-changed) clip '%s' skipped — retimes are not supported yet." % name)
            return
        if el.get("srcEnable") == "video":
            return

        # audioStart/audioDuration = split edit (J/L cut): audio has its own range
        aw0, aw1 = a, b
        if el.get("audioStart") is not None or el.get("audioDuration") is not None:
            ast = t(el.get("audioStart"), start)
            adur = t(el.get("audioDuration"), dur or Fraction(0))
            aw0 = max(w0, delta + ast - before)
            aw1 = min(w1, delta + ast + adur + after)

        # This element's own volume / fades apply to its own audio and its contents,
        # but NOT to clips connected to it.
        env, fades = self._volume(el, delta, aw0, aw1)
        fades = list(fades)
        if ext and ext[2]:
            fades.append(("in", pdelta + ext[2][0], ext[2][1], "crossfade"))
        if ext and ext[3]:
            fades.append(("out", pdelta + ext[3][0] + ext[3][1], ext[3][1], "crossfade"))
        own = mix.add(env, fades)

        if tag == "asset-clip":
            self._emit_asset_clip(el, delta, aw0, aw1, out, here, inactive_roles, own)
        elif tag == "audio":
            self._emit_audio(el, delta, aw0, aw1, out, here, inactive_roles, own)
        elif tag == "ref-clip":
            media = self.media.get(el.get("ref"))
            seq = media.find("sequence") if media is not None else None
            inner = seq.find("spine") if seq is not None else None
            if inner is None:
                self.warn("Compound clip '%s' could not be resolved; skipped." % name)
            else:
                self._walk_children(inner, delta, aw0, aw1, out, here, inactive_roles, own,
                                    ext, start, start + dur if dur is not None else None)
        elif tag == "mc-clip":
            self._walk_multicam(el, delta, aw0, aw1, out, here, inactive_roles, own,
                                ext, start, start + dur if dur is not None else None)

        if tag == "sync-clip":
            roles = set(inactive_roles)
            for ss in el.iter("audio-role-source"):
                if ss.get("active") == "0":
                    roles.add(pretty_role(ss.get("role", "")).lower())
            inactive_roles = roles

        # Children. Connected clips (with a lane) may hang past their parent and still play,
        # so they keep the parent's window. A clip's *own* contents are trimmed to the clip.
        kid_ext = self._transition_ext(el) if tag == "spine" else {}
        for k in el:
            if k.tag not in STORY:
                continue
            inherit = False
            if tag == "clip":
                if k.tag == "audio":
                    win, inherit = (aw0, aw1), True
                elif k.tag == "video" or k.get("lane") is None:
                    win, inherit = (min(a, aw0), max(b, aw1)), True
                else:
                    win = (w0, w1)
            elif tag in ("sync-clip", "spine"):
                win, inherit = ((aw0, aw1) if tag == "sync-clip" else (a, b)), True
            elif tag == "video" and k.tag == "audio" and k.get("lane") is not None and el.get("lane") is None:
                win, inherit = (w0, w1), True  # a clip's video+audio component pair
            else:
                win = (w0, w1)
            e = kid_ext.get(id(k))
            if inherit and dur is not None:
                e = self._merge_ext(e, self._child_ext(k, ext, start, start + dur))
            self._walk(k, delta, win[0], win[1], out, here, inactive_roles, own if inherit else mix, e)

    def _walk_multicam(self, el, delta, a, b, out, here, inactive_roles, mix, ext=None, lo=None, hi=None):
        media = self.media.get(el.get("ref"))
        mc = media.find("multicam") if media is not None else None
        if mc is None:
            self.warn("Multicam clip '%s' could not be resolved; skipped." % el.get("name"))
            return
        audio_angles = [s.get("angleID") for s in el.findall("mc-source") if s.get("srcEnable") in ("audio", "all")]
        if not audio_angles:
            return
        for angle in mc.findall("mc-angle"):
            if angle.get("angleID") in audio_angles:
                self._walk_children(angle, delta, a, b, out, "%s [angle %s]" % (here, angle.get("name")),
                                    inactive_roles, mix, ext, lo, hi)

    # ---------- volume ----------
    def _volume(self, el, delta, aw0, aw1):
        """Return (envelope, fades) for an element's <adjust-volume>.
        envelope: tuple of (timeline_time, dB); a single (None, dB) means constant."""
        av = el.find("adjust-volume")
        if av is None:
            return None, ()
        env = None
        fades = []
        params = av.findall("param")
        p = next((x for x in params if (x.get("name") or "amount").lower() in ("amount", "volume")), None)
        if p is not None:
            kfs = p.findall("keyframeAnimation/keyframe")
            if kfs:
                env = tuple(sorted((t(k.get("time")) + delta, parse_db(k.get("value"))) for k in kfs))
                self.n_keyframed += 1
            elif p.get("value"):
                v = parse_db(p.get("value"))
                env = ((None, v),) if v else None
            fi, fo = p.find("fadeIn"), p.find("fadeOut")
            if fi is not None and t(fi.get("duration")) > 0:
                fades.append(("in", aw0, t(fi.get("duration")), fi.get("type", "easeIn")))
            if fo is not None and t(fo.get("duration")) > 0:
                fades.append(("out", aw1, t(fo.get("duration")), fo.get("type", "easeIn")))
        if env is None and av.get("amount"):
            v = parse_db(av.get("amount"))
            env = ((None, v),) if v else None
        return env, tuple(fades)

    # ---------- leaves ----------
    def _push(self, out, role, asset, chans, delta, a, b, name, here, inactive_roles, mix):
        if b <= a or not chans:
            return
        if role.lower() in inactive_roles or role.split(".")[0].lower() in inactive_roles:
            return
        if asset.path is None:
            self.warn("Asset '%s' has no media path; skipped." % asset.name)
            return
        src_in = (a - delta) - asset.start
        if src_in < 0:
            a -= src_in  # clip reaches before the media starts (e.g. crossfade with no handle)
            src_in = Fraction(0)
            if b <= a:
                return
        ev = AudioEvent(role=role, asset=asset, channels=chans, src_in=src_in, tl_in=a, dur=b - a,
                        name=name, path_desc=here)
        tol = Fraction(1, 1000)
        for kind, when, length, shape in mix.fades:
            if kind == "in" and abs(when - a) < tol:
                if length > ev.fade_in:
                    ev.fade_in, ev.fade_in_shape = length, shape
            elif kind == "out" and abs(when - b) < tol:
                if length > ev.fade_out:
                    ev.fade_out, ev.fade_out_shape = length, shape
            elif (kind == "in" and a < when + length and when < b) or (kind == "out" and when - length < b and a < when):
                self.warn("Some fades on compound/sync clips land mid-clip and were left out (v0.2).")
        ev.gain_points = combine_envelopes(mix.envs, a, b)
        out.append(ev)

    def _channels_for(self, asset: Asset, spec: Optional[str]) -> List[int]:
        if spec:
            chans = []
            for part in spec.replace(" ", "").split(","):
                if "-" in part:
                    lo, hi = part.split("-")
                    chans.extend(range(int(lo), int(hi) + 1))
                elif part:
                    chans.append(int(part))
            return chans
        return list(range(1, max(asset.channels, 1) + 1))

    def _emit_asset_clip(self, el, delta, a, b, out, here, inactive_roles, mix):
        asset = self.assets.get(el.get("ref"))
        if asset is None or not asset.has_audio:
            return
        name = el.get("name", asset.name)
        acs = el.findall("audio-channel-source")
        if acs:
            for s in acs:
                if s.get("enabled") == "0" or s.get("active") == "0":
                    continue
                role = pretty_role(s.get("role") or el.get("audioRole") or "dialogue")
                env, fades = self._volume(s, delta, a, b)
                self._push(out, role, asset, self._channels_for(asset, s.get("srcCh")), delta, a, b, name, here,
                           inactive_roles, mix.add(env, fades))
        else:
            role = pretty_role(el.get("audioRole") or "dialogue")
            self._push(out, role, asset, self._channels_for(asset, None), delta, a, b, name, here, inactive_roles, mix)

    def _emit_audio(self, el, delta, a, b, out, here, inactive_roles, mix):
        asset = self.assets.get(el.get("ref"))
        if asset is None:
            return
        if el.get("srcID") not in (None, "1"):
            self.warn("Some clips use multiple audio streams (srcID>1); channel numbering may be off — check those clips.")
        role = pretty_role(el.get("role") or "dialogue")
        self._push(out, role, asset, self._channels_for(asset, el.get("srcCh")), delta, a, b,
                   el.get("name", asset.name), here, inactive_roles, mix)

def scan(path: str, project: Optional[str] = None) -> dict:
    """Summary used by the mapping UI / --scan."""
    p = Parser(path, project)
    tl = p.parse()
    roles: Dict[str, dict] = {}
    for e in tl.events:
        r = roles.setdefault(e.role, {"clips": 0, "max_channels": 0, "seconds": 0.0})
        r["clips"] += 1
        r["max_channels"] = max(r["max_channels"], len(e.channels))
        r["seconds"] += float(e.dur)
    fps = 1 / tl.frame_duration
    return {
        "timeline": tl.name,
        "projects": p.project_names(),
        "fps": round(float(fps), 3),
        "drop_frame": tl.drop_frame,
        "start_timecode": tc_string(tl.tc_start, tl.frame_duration, tl.drop_frame),
        "duration_seconds": round(float(tl.duration), 3),
        "audio_rate": tl.audio_rate,
        "fcpxml_version": tl.fcpxml_version,
        "roles": roles,
        "warnings": tl.warnings,
        "missing_media": sorted({e.asset.path for e in tl.events if e.asset.path and not os.path.exists(e.asset.path)}),
    }


def tc_string(seconds: Fraction, frame_dur: Fraction, drop: bool = False) -> str:
    fps = round(1 / frame_dur)
    total = int(round(seconds / frame_dur))
    if drop and fps in (30, 60):
        d = 2 if fps == 30 else 4
        per10 = fps * 600 - d * 9
        tens, rem = divmod(total, per10)
        total += d * 9 * tens + (d * ((rem - d) // (fps * 60 - d)) if rem > d else 0)
    f = total % fps
    s = (total // fps) % 60
    m = (total // (fps * 60)) % 60
    h = total // (fps * 3600)
    return "%02d:%02d:%02d%s%02d" % (h, m, s, ";" if drop else ":", f)

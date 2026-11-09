"""Clip gain and fades in AAF terms.

- Volume (static or keyframed) -> OperationGroup "Audio Gain" (Avid MonoAudioGain) wrapping the clip,
  with an Amplitude parameter: ConstantValue, or VaryingValue with linear control points.
  AAF control-point times are 0..1 across the clip.
- Fades -> AAF Transitions (Avid MonoAudioDissolve) against Filler, the way Media Composer writes them.
  A fade with no room for a transition (clip butted against another on the same track) is baked
  into the clip's gain curve instead.
"""
from __future__ import annotations

from fractions import Fraction
from typing import List, Optional, Tuple

from aaf2.auid import AUID
from aaf2.misc import LinearInterp
from aaf2.rational import AAFRational

OP_MONO_AUDIO_GAIN = AUID("9d2ea894-0968-11d3-8a38-0050040ef7d2")
OP_MONO_AUDIO_DISSOLVE = AUID("0c3bea41-fc05-11d2-8a29-0050040ef7d2")
PARAM_AMPLITUDE = AUID("e4962321-2267-11d3-8a4c-0050040ef7d2")
PARAM_LEVEL = AUID("e4962320-2267-11d3-8a4c-0050040ef7d2")


def db_to_amp(db: float) -> float:
    return 0.0 if db <= -143 else 10 ** (db / 20.0)


def rat(x: float) -> AAFRational:
    return AAFRational(Fraction(x).limit_denominator(1000000))


class Effects:
    def __init__(self, f):
        self.f = f
        self._gain = self._dissolve = self._amp = self._level = self._interp = None

    # ----- definitions (created once per file) -----
    def _param(self, auid, name):
        p = self.f.create.ParameterDef(auid, name, "", "Rational")
        self.f.dictionary.register_def(p)
        return p

    def _interpdef(self):
        if self._interp is None:
            self._interp = self.f.create.InterpolationDef(LinearInterp, "LinearInterp", "Linear keyframe interpolation")
            self.f.dictionary.register_def(self._interp)
        return self._interp

    def _opdef(self, auid, name, inputs, param):
        d = self.f.create.OperationDef(auid, name, "")
        self.f.dictionary.register_def(d)
        d.media_kind = "sound"
        d["NumberInputs"].value = inputs
        d["IsTimeWarp"].value = False
        d["ParametersDefined"].append(param)
        return d

    def gain_def(self):
        if self._gain is None:
            self._amp = self._param(PARAM_AMPLITUDE, "Amplitude")
            self._gain = self._opdef(OP_MONO_AUDIO_GAIN, "Audio Gain", 1, self._amp)
        return self._gain

    def dissolve_def(self):
        if self._dissolve is None:
            self._level = self._param(PARAM_LEVEL, "Level")
            self._dissolve = self._opdef(OP_MONO_AUDIO_DISSOLVE, "Mono Audio Dissolve", 2, self._level)
        return self._dissolve

    # ----- objects -----
    def gain(self, component, length: int, curve):
        """curve: float (constant amplitude) or list of (t 0..1, amplitude)."""
        d = self.gain_def()  # create defs first: pyaaf2's factory can't be nested
        op = self.f.create.OperationGroup(d, length, "sound")
        op.segments.append(component)
        if isinstance(curve, (int, float)):
            p = self.f.create.ConstantValue(self._amp, rat(curve))
        else:
            interp = self._interpdef()
            p = self.f.create.VaryingValue(self._amp)
            p.interpolationdef = interp
            for tt, amp in curve:
                cp = self.f.create.ControlPoint()
                cp.time = rat(float(tt))
                cp.value = rat(amp)
                cp["EditHint"].value = "Proportional"
                p["PointList"].append(cp)
        op.parameters.append(p)
        return op

    def fade(self, length: int):
        d = self.dissolve_def()
        interp = self._interpdef()
        op = self.f.create.OperationGroup(d, length, "sound")
        lv = self.f.create.VaryingValue(self._level)
        lv.interpolationdef = interp
        for tt, v in ((0, 0), (1, 1)):
            cp = self.f.create.ControlPoint()
            cp.time = rat(tt)
            cp.value = rat(v)
            cp["EditHint"].value = "Proportional"
            lv["PointList"].append(cp)
        op.parameters.append(lv)
        tr = self.f.create.Transition("sound", length)
        tr["OperationGroup"].value = op
        tr.cutpoint = length // 2
        return tr


def amplitude_curve(gain_points: Optional[List[Tuple[Fraction, float]]], s: int, length: int, rate: Fraction,
                    bake_in: int = 0, bake_out: int = 0):
    """Build the clip-gain curve for a clip at timeline units [s, s+length).
    gain_points are (timeline seconds, dB). bake_in/out are fade lengths (units) to fold in.
    Returns None (unity), a float (constant), or [(t 0..1, amplitude)]."""
    if not gain_points and not bake_in and not bake_out:
        return None

    def db_at(u):  # u in timeline units
        if not gain_points:
            return 0.0
        x = Fraction(u) / rate
        if len(gain_points) == 1 or x <= gain_points[0][0]:
            return gain_points[0][1]
        if x >= gain_points[-1][0]:
            return gain_points[-1][1]
        for (x0, v0), (x1, v1) in zip(gain_points, gain_points[1:]):
            if x0 <= x <= x1:
                return v1 if x1 == x0 else v0 + (v1 - v0) * float((x - x0) / (x1 - x0))
        return gain_points[-1][1]

    if not bake_in and not bake_out and len(gain_points) == 1:
        return db_to_amp(gain_points[0][1])

    us = {s, s + length}
    if gain_points and len(gain_points) > 1:
        for x, _ in gain_points:
            u = x * rate
            if s < u < s + length:
                us.add(u)
    if bake_in:
        us.add(Fraction(s + bake_in))
    if bake_out:
        us.add(Fraction(s + length - bake_out))
    pts = []
    for u in sorted(us):
        amp = db_to_amp(db_at(u))
        if bake_in and u < s + bake_in:
            amp *= float((u - s) / bake_in)
        if bake_out and u > s + length - bake_out:
            amp *= float((s + length - u) / bake_out)
        pts.append((Fraction(u - s) / length, amp))
    return pts

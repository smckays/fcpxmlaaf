"""Build a synthetic FCP-style test project: media files + Info.fcpxml in a .fcpxmld bundle.
Usage: python3 make_test_project.py OUTDIR
"""
import os
import subprocess
import sys
from fractions import Fraction

out = os.path.abspath(sys.argv[1])
FRIENDLY = "--friendly" in sys.argv  # gentle test tones, 16-bit, portable paths (for testers)
media = os.path.join(out, "Media")
os.makedirs(media, exist_ok=True)
FD = Fraction(1001, 24000)  # 23.976
PCM = "pcm_s16le" if "--friendly" in sys.argv else "pcm_s24le"


def ff(*args):
    subprocess.run(["ffmpeg", "-v", "error", "-y", *args], check=True)


TONES = [1000, 600, 800, 1200, 400, 480, 1500, 300, 750, 960]


def noise(seed, ch, secs):
    if FRIENDLY:
        return sum([["-f", "lavfi", "-i", "sine=f=%d:r=48000:d=%d:samples_per_frame=4800,volume=-18dB"
                     % (TONES[(seed // 100 * 3 + i) % len(TONES)], secs)] for i in range(ch)], [])
    return ["-f", "lavfi", "-i", "anoisesrc=seed=%d:d=%d:r=48000:a=0.3" % (seed, secs)] * 0 + \
        sum([["-f", "lavfi", "-i", "anoisesrc=seed=%d:d=%d:r=48000:a=0.3" % (seed + i, secs)] for i in range(ch)], [])


def merge(ch):
    return ["-filter_complex", "".join("[%d:a]" % i for i in range(ch)) + "amerge=inputs=%d[a]" % ch, "-map", "[a]"] if ch > 1 else ["-map", "0:a"]


# interview.mov: video + 2ch audio (PCM so it's lossless for verification)
ff("-f", "lavfi", "-i", "testsrc=d=40:s=320x180:r=24000/1001", *noise(100, 2, 40),
   "-filter_complex", "[1:a][2:a]amerge=inputs=2[a]", "-map", "0:v", "-map", "[a]",
   "-c:v", "libx264", "-crf", "35", "-pix_fmt", "yuv420p", "-c:a", PCM, os.path.join(media, "Interview A001.mov"))
ff(*noise(200, 4, 60), *merge(4), "-c:a", PCM, os.path.join(media, "Recorder 4ch.wav"))
ff(*noise(300, 2, 60), *merge(2), "-c:a", PCM, os.path.join(media, "Music Cue.wav"))
ff(*noise(400, 1, 20), "-c:a", PCM, os.path.join(media, "SFX Door.wav"))
ff(*noise(500, 1, 20), "-c:a", PCM, os.path.join(media, "SFX Wind.wav"))


def T(frames):
    x = Fraction(frames) * FD
    return "%d/%ds" % (x.numerator, x.denominator) if x.denominator != 1 else "%ds" % x.numerator


def url(name):
    import urllib.parse
    root = "/Users/Shared/FCPXML2AAF Sample/Media" if FRIENDLY else media
    return "file://" + urllib.parse.quote(os.path.join(root, name))


TC = 86400  # 3600s = 86313.6 frames is not integral; use frame-exact hour: 86400 frames @23.976 = 3603.6s
xml = f'''<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE fcpxml>
<fcpxml version="1.11">
  <resources>
    <format id="r1" name="FFVideoFormat1080p2398" frameDuration="1001/24000s" width="1920" height="1080"/>
    <asset id="r2" name="Interview A001" start="0s" duration="40s" hasVideo="1" hasAudio="1" audioSources="1" audioChannels="2" audioRate="48000" format="r1">
      <media-rep kind="original-media" src="{url('Interview A001.mov')}"/>
    </asset>
    <asset id="r3" name="Recorder 4ch" start="0s" duration="60s" hasAudio="1" audioSources="1" audioChannels="4" audioRate="48000">
      <media-rep kind="original-media" src="{url('Recorder 4ch.wav')}"/>
    </asset>
    <asset id="r4" name="Music Cue" start="0s" duration="60s" hasAudio="1" audioSources="1" audioChannels="2" audioRate="48000">
      <media-rep kind="original-media" src="{url('Music Cue.wav')}"/>
    </asset>
    <asset id="r5" name="SFX Door" start="0s" duration="20s" hasAudio="1" audioSources="1" audioChannels="1" audioRate="48000">
      <media-rep kind="original-media" src="{url('SFX Door.wav')}"/>
    </asset>
    <asset id="r6" name="SFX Wind" start="0s" duration="20s" hasAudio="1" audioSources="1" audioChannels="1" audioRate="48000">
      <media-rep kind="original-media" src="{url('SFX Wind.wav')}"/>
    </asset>
    <media id="r7" name="SFX Compound">
      <sequence format="r1" duration="{T(240)}" tcStart="0s">
        <spine>
          <asset-clip ref="r5" offset="0s" name="SFX Door" start="{T(24)}" duration="{T(240)}" audioRole="effects.Doors">
            <asset-clip ref="r6" lane="1" offset="{T(24+48)}" name="SFX Wind" start="{T(0)}" duration="{T(96)}" audioRole="effects.Ambience"/>
          </asset-clip>
        </spine>
      </sequence>
    </media>
  </resources>
  <library>
    <event name="Test Event">
      <project name="Test Cut v1">
        <sequence format="r1" duration="{T(1200)}" tcStart="{T(TC)}" tcFormat="NDF" audioLayout="stereo" audioRate="48k">
          <spine>
            <asset-clip ref="r2" offset="{T(TC)}" name="Interview A001" start="{T(48)}" duration="{T(120)}" format="r1" tcFormat="NDF">
              <adjust-volume><param name="amount"><fadeIn type="easeIn" duration="{T(12)}"/></param></adjust-volume>
              <audio-channel-source srcCh="1" role="dialogue.Boom"/>
              <audio-channel-source srcCh="2" role="dialogue.Lav"/>
              <asset-clip ref="r4" lane="-1" offset="{T(48+24)}" name="Music Cue" start="{T(240)}" duration="{T(480)}" audioRole="music.Score">
                <adjust-volume amount="-6dB"><param name="amount"><fadeOut type="easeIn" duration="{T(48)}"/></param></adjust-volume>
              </asset-clip>
            </asset-clip>
            <transition name="Cross Dissolve" offset="{T(TC+108)}" duration="{T(24)}"/>
            <sync-clip offset="{T(TC+120)}" name="Synced Interview" start="{T(0)}" duration="{T(192)}" tcFormat="NDF">
              <asset-clip ref="r2" offset="{T(0)}" name="Interview A001" start="{T(300)}" duration="{T(240)}" audioRole="dialogue.Camera"/>
              <asset-clip ref="r3" lane="-1" offset="{T(0)}" name="Recorder 4ch" start="{T(312)}" duration="{T(240)}" audioRole="dialogue.Recorder">
                <adjust-volume><param name="amount"><keyframeAnimation><keyframe time="{T(312)}" value="-20dB"/><keyframe time="{T(360)}" value="0dB"/></keyframeAnimation></param></adjust-volume>
              </asset-clip>
              <sync-source sourceID="storyline">
                <audio-role-source role="dialogue.Camera" active="0"/>
              </sync-source>
            </sync-clip>
            <ref-clip ref="r7" offset="{T(TC+312)}" name="SFX Compound" start="{T(24)}" duration="{T(192)}"><adjust-volume amount="-3dB"/></ref-clip>
            <asset-clip ref="r2" offset="{T(TC+504)}" name="Disabled Clip" start="{T(0)}" duration="{T(48)}" enabled="0" audioRole="dialogue"/>
            <gap name="Gap" offset="{T(TC+552)}" start="{T(86400)}" duration="{T(96)}">
              <asset-clip ref="r5" lane="1" offset="{T(86400+12)}" name="SFX Door 2" start="{T(0)}" duration="{T(60)}" audioRole="effects.Doors"/>
            </gap>
            <asset-clip ref="r2" offset="{T(TC+648)}" name="J-cut Interview" start="{T(400)}" duration="{T(96)}" audioStart="{T(376)}" audioDuration="{T(120)}" audioRole="dialogue.Boom"/>
          </spine>
        </sequence>
      </project>
    </event>
  </library>
</fcpxml>
'''
bundle = os.path.join(out, "Test Cut v1.fcpxmld")
os.makedirs(bundle, exist_ok=True)
with open(os.path.join(bundle, "Info.fcpxml"), "w") as fh:
    fh.write(xml)
print(bundle)

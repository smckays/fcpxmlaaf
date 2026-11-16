# FCPXML to AAF

Turns a Final Cut Pro timeline export (FCPXML) into an AAF for Pro Tools and other DAWs, with the audio embedded.

- **Roles become tracks.** Map FCP roles (Dialogue, Music, Effects and subroles) to track groups like DX, MX and FX. Overlapping clips spill onto extra tracks, and a minimum track count keeps a mixer's template layout.
- **Flattens the magnetic timeline:** connected clips, compound clips, multicam, sync clips, J/L cuts and disabled roles.
- **Levels and fades:** clip volume and keyframes become clip gain. Fade handles become fades, and cross dissolves become crossfades on the same track.
- **Audio:** each channel is extracted to a mono 48 kHz/24-bit clip with handles (5 seconds by default) and embedded in the AAF.
- **Channel-level routing:** pin any channel to a named track ("Boom", "Lav 1") so every export lines up the same way; overflow tracks fill by packing or checkerboarding; nudge a role by frames; override the start timecode.
- **Preflight** checks each export first and explains problems with one-click fixes.
- **Verified exports:** after writing, the app re-opens the AAF and checks track names and order, start timecode, every clip's position, length and clip gain, and compares the embedded audio at the start and end of every clip with the source file, sample for sample (WAV sources are read directly, without the decoder used for the export). Problems are reported with clip, track, timecode and how far out of sync.
- **Listens to the source audio:** finds distortion in the original recordings (with timecode), silent channels, and duplicate or −10 dB safety-copy channels (confirmed on the waveform, not just levels). 32-bit float recordings that go over full scale are kept intact: the embedded audio is lowered to fit 24-bit and the level restored as clip gain.
- **Presets** save a show's track layout for reuse.

Status: test build. Sample-accurate placement is verified against the source audio, but import into Pro Tools hasn't been confirmed yet.

## Mac app

`app/` is a Mac app: a patchbay that wires Final Cut audio to AAF track groups, a **Preflight** panel that checks every export and offers one-click fixes (camera scratch audio, offline media, muted clips, track templates…), and a preview of the AAF. Updates install from inside the app. It bundles Python and ffmpeg, so there's nothing to install. Unsigned test builds need a one-time approval in System Settings ▸ Privacy & Security; see `app/macos/READ ME FIRST.txt`.

Build it (on any machine with Python 3, pip, curl and zip):

```bash
dev/fetch_parts.sh /tmp/parts
python3 dev/make_test_project.py "/tmp/sample/FCPXML2AAF Sample" --friendly
dev/build_app.sh /tmp/parts /tmp/out "/tmp/sample/FCPXML2AAF Sample"
```

## Command line

```bash
PYTHONPATH=engine python3 -m fcpxml2aaf "My Cut.fcpxmld" --preset presets/example-mix-template.json --out ~/Desktop/AAF
```

Options: `--dry-run` (layout only), `--scan` (list roles as JSON), `--media-root FOLDER` (find moved media), `--project NAME`.
Needs ffmpeg for audio inside video files. WAV/AIFF works without it.

## Layout

| Path | What |
|---|---|
| `engine/fcpxml2aaf/` | Converter: FCPXML parser, role-to-track mapping, audio extraction, AAF writer, levels/fades |
| `engine/aaf2/` | Vendored [pyaaf2](https://github.com/markreidvfx/pyaaf2) (MIT) |
| `app/` | Mac app: local service (`server.py`), window (`main.py`), interface (`ui/index.html`; the earlier pro-editor look is `ui/classic.html`), bundle files (`macos/`) |
| `engine/fcpxml2aaf/preflight.py` | Preflight checks: what's wrong, why, and the fix |
| `engine/fcpxml2aaf/verify.py` | Post-export check of the finished AAF against the plan and the source audio |
| `engine/fcpxml2aaf/analysis.py` | Audio analysis: clipping, silence, duplicate channels, float headroom |
| `design/mockups/` | Design directions explored for the interface |
| `presets/` | Example presets |
| `mapper/` | Standalone web page version of the role mapper |
| `dev/` | Test-project generator, icon, build and download scripts |
| `Convert to AAF.command` | Older double-click script version (needs Python on the Mac) |

## Not yet supported

Fade shapes (they come through as linear), pan, audio effects, retimed clips, and stereo tracks (stereo clips become two mono tracks).

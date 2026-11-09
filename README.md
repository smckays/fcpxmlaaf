# FCPXML to AAF

Turns a Final Cut Pro timeline export (FCPXML) into an AAF for Pro Tools and other DAWs, with the audio embedded.

- **Roles become tracks.** Map FCP roles (Dialogue, Music, Effects and subroles) to track groups like DX, MX and FX. Overlapping clips spill onto extra tracks, and a minimum track count keeps a mixer's template layout.
- **Flattens the magnetic timeline:** connected clips, compound clips, multicam, sync clips, J/L cuts and disabled roles.
- **Levels and fades:** clip volume and keyframes become clip gain. Fade handles become fades, and cross dissolves become crossfades on the same track.
- **Audio:** each channel is extracted to a mono 48 kHz/24-bit clip with handles (5 seconds by default) and embedded in the AAF.
- **Presets** save a show's track layout for reuse.

Status: test build. Sample-accurate placement is verified against the source audio, but import into Pro Tools hasn't been confirmed yet.

## Mac app

`app/` is a Mac app with a classic pro-editor interface: Browser, Viewer and a Timeline that previews the AAF. It bundles Python and ffmpeg, so there's nothing to install. Unsigned test builds need a one-time approval in System Settings ▸ Privacy & Security; see `app/macos/READ ME FIRST.txt`.

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
| `app/` | Mac app: local service (`server.py`), window (`main.py`), interface (`ui/index.html`), bundle files (`macos/`) |
| `presets/` | Example presets |
| `mapper/` | Standalone web page version of the role mapper |
| `dev/` | Test-project generator, icon, build and download scripts |
| `Convert to AAF.command` | Older double-click script version (needs Python on the Mac) |

## Not yet supported

Fade shapes (they come through as linear), pan, audio effects, retimed clips, and stereo tracks (stereo clips become two mono tracks).

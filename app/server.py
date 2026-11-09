"""Local service behind the app window: file dialogs, scanning, live track preview, export jobs, presets.
Listens on 127.0.0.1 only, and every API call must carry the per-launch token."""
from __future__ import annotations

import hashlib
import json
import os
import platform
import secrets
import shutil
import tempfile
import subprocess
import sys
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from fractions import Fraction

from fcpxml2aaf import __version__
from fcpxml2aaf.convert import convert
from fcpxml2aaf import preflight as preflight_mod
from fcpxml2aaf.mapping import build_tracks, load_preset
from fcpxml2aaf.media import Relinker, find_tool
from fcpxml2aaf.parser import Parser, resolve_input, tc_string

HERE = os.path.dirname(os.path.abspath(__file__))
RESOURCES = os.path.dirname(HERE)
UI = os.path.join(HERE, "ui", "index.html")
SUPPORT = os.path.expanduser("~/Library/Application Support/FCPXML to AAF")
PRESETS = os.path.join(SUPPORT, "Presets")
STATE = os.path.join(SUPPORT, "state.json")
SAMPLE = os.path.join(RESOURCES, "Sample Project", "FCPXML2AAF Sample", "Test Cut v1.fcpxmld")
BUNDLED_PRESETS = os.path.join(RESOURCES, "presets")


# ---------------- small helpers ----------------
def load_state() -> dict:
    try:
        with open(STATE) as fh:
            return json.load(fh)
    except Exception:
        return {"recent": []}


def save_state(st: dict):
    os.makedirs(SUPPORT, exist_ok=True)
    with open(STATE, "w") as fh:
        json.dump(st, fh, indent=2)


def remember(path: str):
    st = load_state()
    rec = [p for p in st.get("recent", []) if p != path]
    st["recent"] = [path] + rec[:5]
    save_state(st)


def ensure_presets():
    os.makedirs(PRESETS, exist_ok=True)
    if not os.listdir(PRESETS) and os.path.isdir(BUNDLED_PRESETS):
        for fn in os.listdir(BUNDLED_PRESETS):
            if fn.endswith(".json") and fn.startswith("example"):
                with open(os.path.join(BUNDLED_PRESETS, fn)) as a, open(os.path.join(PRESETS, fn), "w") as b:
                    b.write(a.read())


def safe_preset_file(name: str) -> str:
    keep = "".join(c for c in name if c.isalnum() or c in " -_.").strip() or "Preset"
    return os.path.join(PRESETS, keep[:80] + ".json")


def applescript_string(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def dialog(kind: str, prompt: str, default_dir: str = None):
    """Native macOS open dialog via osascript. Returns a POSIX path or None if cancelled.
    FCPXML2AAF_FAKE_DIALOG (JSON {kind: path}) is used for automated tests."""
    fake = os.environ.get("FCPXML2AAF_FAKE_DIALOG")
    if fake:
        return json.loads(fake).get(kind)
    what = "choose folder" if kind in ("folder", "bundle", "output", "media") else "choose file"
    loc = ""
    if default_dir and os.path.isdir(default_dir):
        loc = " default location (POSIX file %s)" % applescript_string(default_dir)
    script = 'POSIX path of (%s with prompt %s%s)' % (what, applescript_string(prompt), loc)
    r = subprocess.run(["osascript", "-e", "activate", "-e", script], capture_output=True, text=True)
    if r.returncode != 0:
        return None
    return r.stdout.strip().rstrip("/") or None


# ---------------- timeline cache ----------------
class Timelines:
    def __init__(self):
        self._cache = {}
        self._lock = threading.Lock()

    def get(self, path: str, project: str = None):
        real = resolve_input(path)
        key = (real, os.path.getmtime(real), project)
        with self._lock:
            if key not in self._cache:
                p = Parser(path, project)
                self._cache = {key: (p, p.parse())}
            return self._cache[key]


TL = Timelines()


def scan_info(path: str, project: str = None, roots=()) -> dict:
    p, tl = TL.get(path, project)
    roles = {}
    for e in tl.events:
        r = roles.setdefault(e.role, {"clips": 0, "max_channels": 0, "levels": 0, "fades": 0, "camera": 0})
        r["clips"] += 1
        r["camera"] += 1 if e.asset.has_video else 0
        r["max_channels"] = max(r["max_channels"], len(e.channels))
        r["levels"] += 1 if e.gain_points else 0
        r["fades"] += 1 if (e.fade_in or e.fade_out) else 0
    here = os.path.dirname(os.path.abspath(os.path.normpath(path)))
    rl = Relinker([here] + list(roots or []))
    paths = sorted({e.asset.path for e in tl.events if e.asset.path})
    missing = [m for m in paths if not rl.find(m)]
    fps = 1 / tl.frame_duration
    return {
        "path": path, "name": os.path.basename(os.path.normpath(path)),
        "timeline": tl.name, "projects": p.project_names(),
        "fps": round(float(fps), 3), "drop_frame": tl.drop_frame,
        "start_timecode": tc_string(tl.tc_start, tl.frame_duration, tl.drop_frame),
        "length": tc_string(tl.duration, tl.frame_duration), "fcpxml_version": tl.fcpxml_version,
        "audio_rate": tl.audio_rate, "roles": roles, "warnings": tl.warnings,
        "media_files": len(paths), "missing_media": missing,
        "crossfades": p.n_transitions,
        "default_out": default_out(path),
        "is_sample": os.path.abspath(path).startswith(RESOURCES),
    }


def default_out(path: str) -> str:
    if os.path.abspath(path).startswith(RESOURCES):  # never write inside the app
        return os.path.expanduser("~/Desktop/FCPXML to AAF Sample Export")
    return os.path.join(os.path.dirname(os.path.abspath(os.path.normpath(path))), "AAF Export")


def preview(path: str, preset: dict, project: str = None, roots=()) -> dict:
    """The exact track layout the AAF will get, with every clip's position, fades and gain for the timeline view."""
    parser, tl = TL.get(path, project)
    pr = load_preset(None)
    pr.update({k: v for k, v in preset.items() if k != "options"})
    pr["options"].update(preset.get("options") or {})
    opts = pr["options"]
    fps = 1 / tl.frame_duration
    rate = fps if opts["edit_rate"] == "frame" else Fraction(int(opts["sample_rate"]))
    tracks, notes = build_tracks(tl.events, pr, rate)
    here = os.path.dirname(os.path.abspath(os.path.normpath(path)))
    rl = Relinker([here] + list(roots or []))
    found = {}

    def online(p):
        if p not in found:
            found[p] = bool(p and rl.find(p))
        return found[p]

    out = []
    for t in tracks:
        items = []
        for m in t.items:
            ev = m.ev
            it = {"s": round(float(ev.tl_in), 4), "e": round(float(ev.tl_in + ev.dur), 4), "name": ev.name,
                  "ch": m.channel, "role": ev.role, "offline": not online(ev.asset.path)}
            if opts.get("fades", "aaf") != "off":
                it["fi"] = round(float(ev.fade_in), 4)
                it["fo"] = round(float(ev.fade_out), 4)
                it["xin"] = ev.fade_in_shape == "crossfade"
                it["xout"] = ev.fade_out_shape == "crossfade"
            if opts.get("levels", "clip_gain") != "off" and ev.gain_points:
                it["gain"] = [[round(float(x), 4), round(v, 2)] for x, v in ev.gain_points]
            items.append(it)
        out.append({"name": t.name, "group": t.group, "clips": len(t.items),
                    "roles": sorted({m.ev.role for m in t.items}), "items": items})
    pf = preflight_mod.run(tl, parser, pr, tracks, notes, rl)
    return {"tracks": out, "notes": notes, "preflight": pf, "duration": round(float(tl.duration), 4),
            "tc_start": round(float(tl.tc_start), 4), "fps": float(fps), "drop": tl.drop_frame,
            "offline_clips": sum(1 for t in out for i in t["items"] if i["offline"])}


# ---------------- export jobs ----------------
class Job:
    def __init__(self):
        self.state = "idle"
        self.done = self.total = 0
        self.log = []
        self.report = ""
        self.aaf = self.report_path = None
        self.error = None
        self.started = None

    def to_dict(self):
        return {"state": self.state, "done": self.done, "total": self.total, "log": self.log[-8:],
                "report": self.report, "aaf": self.aaf, "report_path": self.report_path, "error": self.error,
                "elapsed": round(time.time() - self.started, 1) if self.started else 0}


JOB = Job()


def run_export(path, preset, out_dir, project, media_roots):
    global JOB
    job = Job()
    job.state, job.started = "running", time.time()
    JOB = job

    def prog(d, t):
        job.done, job.total = d, t

    try:
        aaf, rep = convert(path, None, out_dir, project, media_roots, False, log=lambda s: job.log.append(s),
                           progress=prog, preset=preset)
        job.aaf, job.report_path = aaf, rep
        with open(rep, encoding="utf-8") as fh:
            job.report = fh.read()
        job.state = "done"
    except Exception as e:  # shown in the window
        job.error = str(e) or e.__class__.__name__
        job.log.append(traceback.format_exc())
        job.state = "failed"


# ---------------- updates ----------------
UPDATE_URL = os.environ.get("FCPXML2AAF_UPDATE_URL",
                            "https://raw.githubusercontent.com/smckays/fcpxmlaaf/downloads/latest.json")
CACHE = os.path.expanduser("~/Library/Caches/FCPXML to AAF")


def app_bundle():
    """The .app folder this copy runs from, or None when running from source."""
    b = os.path.dirname(os.path.dirname(RESOURCES))
    return b if b.endswith(".app") else None


def mac_flavor() -> str:
    """Which download fits this Mac. Apple Silicon is detected even when running under Rosetta."""
    try:
        r = subprocess.run(["sysctl", "-in", "hw.optional.arm64"], capture_output=True, text=True)
        if r.stdout.strip() == "1":
            return "apple-silicon"
    except Exception:
        pass
    return "apple-silicon" if platform.machine() == "arm64" else "intel"


def vtuple(v: str):
    return tuple(int(x) if x.isdigit() else 0 for x in str(v).split("."))


def fetch(url: str, dest: str = None, timeout: int = 30) -> bytes:
    """Use the Mac's own curl (it trusts the system's certificates); fall back to urllib."""
    curl = shutil.which("curl") or "/usr/bin/curl"
    if os.path.exists(curl):
        cmd = [curl, "-fsSL", "--max-time", str(timeout), url]
        if dest:
            cmd[1:1] = ["-o", dest]
        r = subprocess.run(cmd, capture_output=True)
        if r.returncode != 0:
            raise RuntimeError("Couldn't reach GitHub (%s)" % (r.stderr.decode(errors="ignore").strip()[-160:] or r.returncode))
        return b"" if dest else r.stdout
    import urllib.request
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        data = resp.read()
    if dest:
        with open(dest, "wb") as fh:
            fh.write(data)
        return b""
    return data


def check_update() -> dict:
    info = json.loads(fetch(UPDATE_URL).decode())
    flavor = mac_flavor()
    f = (info.get("files") or {}).get(flavor)
    bundle = app_bundle()
    can = bool(bundle) and "/AppTranslocation/" not in bundle and os.access(os.path.dirname(bundle), os.W_OK)
    return {"current": __version__, "latest": info.get("version"), "notes": info.get("notes", ""),
            "newer": bool(f) and vtuple(info.get("version")) > vtuple(__version__),
            "size": (f or {}).get("size"), "flavor": flavor, "can_install": can,
            "why_not": None if can else ("This copy isn't running from a .app bundle." if not bundle else
                                         "Move the app into your Applications folder first, then update.")}


class Update:
    state = "idle"   # idle | downloading | installing | restarting | failed
    done = total = 0
    error = None


def run_update():
    U = Update
    try:
        U.state, U.error, U.done = "downloading", None, 0
        info = json.loads(fetch(UPDATE_URL).decode())
        f = info["files"][mac_flavor()]
        U.total = int(f.get("size") or 0)
        os.makedirs(CACHE, exist_ok=True)
        work = tempfile.mkdtemp(prefix="update-", dir=CACHE)
        zpath = os.path.join(work, f["name"])
        th = threading.Thread(target=fetch, args=(f["url"], zpath, 1800))
        th.start()
        while th.is_alive():
            U.done = os.path.getsize(zpath) if os.path.exists(zpath) else 0
            time.sleep(0.3)
        if not os.path.exists(zpath) or (U.total and os.path.getsize(zpath) != U.total):
            raise RuntimeError("The download didn't finish. Check your internet connection and try again.")
        U.done = U.total
        U.state = "installing"
        h = hashlib.sha256()
        with open(zpath, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
        if f.get("sha256") and h.hexdigest() != f["sha256"]:
            raise RuntimeError("The download was damaged (checksum mismatch). Try again.")
        out = os.path.join(work, "unzipped")
        os.makedirs(out)
        ditto = shutil.which("ditto") or ("/usr/bin/ditto" if os.path.exists("/usr/bin/ditto") else None)
        r = subprocess.run([ditto, "-x", "-k", zpath, out] if ditto else ["unzip", "-q", zpath, "-d", out], capture_output=True)
        new_app = os.path.join(out, "FCPXML to AAF.app")
        if r.returncode != 0 or not os.path.isdir(new_app):
            raise RuntimeError("Couldn't unpack the update.")
        old_app = app_bundle()
        helper = os.path.join(work, "install.sh")
        with open(helper, "w") as fh:
            fh.write(INSTALL_SCRIPT)
        os.chmod(helper, 0o755)
        U.state = "restarting"
        subprocess.Popen(["/bin/bash", helper, str(os.getpid()), new_app, old_app, work],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        time.sleep(1.2)  # let the window show "Restarting…"
        os._exit(0)
    except Exception as e:
        U.state, U.error = "failed", str(e) or e.__class__.__name__


# Runs after the app quits: swap the app bundle, keep the old one if anything fails, relaunch.
INSTALL_SCRIPT = r"""#!/bin/bash
PID="$1"; NEW="$2"; OLD="$3"; WORK="$4"
for i in $(seq 1 120); do kill -0 "$PID" 2>/dev/null || break; sleep 0.5; done
BACKUP="$OLD.previous"
rm -rf "$BACKUP"
if mv "$OLD" "$BACKUP" && mv "$NEW" "$OLD"; then
  rm -rf "$BACKUP"
else
  [ -d "$OLD" ] || mv "$BACKUP" "$OLD"
fi
xattr -dr com.apple.quarantine "$OLD" 2>/dev/null
rm -rf "$WORK"
command -v open >/dev/null && open "$OLD"
"""


# ---------------- HTTP ----------------
class App:
    token = secrets.token_urlsafe(16)
    last_beat = time.time()


class Handler(BaseHTTPRequestHandler):
    server_version = "FCPXMLtoAAF"

    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path.split("?")[0] in ("/", "/index.html"):
            with open(UI, "rb") as fh:
                return self._send(200, fh.read(), "text/html; charset=utf-8")
        self._send(404, {"error": "not found"})

    def do_POST(self):
        if self.headers.get("X-Token") != App.token:
            return self._send(403, {"error": "forbidden"})
        n = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            return self._send(400, {"error": "bad json"})
        route = self.path.split("?")[0].strip("/").replace("api/", "")
        try:
            out = getattr(self, "api_" + route.replace("-", "_"))(body)
            self._send(200, out if out is not None else {})
        except AttributeError:
            self._send(404, {"error": "unknown call"})
        except Exception as e:
            self._send(200, {"error": str(e) or e.__class__.__name__})

    # ----- api -----
    def api_hello(self, b):
        ensure_presets()
        st = load_state()
        return {"version": __version__, "ffmpeg": bool(find_tool("ffmpeg")), "ui": st.get("ui") or {},
                "check_updates": time.time() - st.get("last_update_check", 0) > 6 * 3600, "recent":
                [p for p in load_state().get("recent", []) if os.path.exists(p)],
                "sample": SAMPLE if os.path.exists(SAMPLE) else None}

    def api_ui_state(self, b):
        """Get (no body) or save ({"ui": {...}}) appearance and window settings."""
        st = load_state()
        if "ui" in b:
            st["ui"] = dict(st.get("ui") or {}, **b["ui"])
            save_state(st)
        return {"ui": st.get("ui") or {}}

    def api_check_update(self, b):
        st = load_state()
        st["last_update_check"] = time.time()
        save_state(st)
        return check_update()

    def api_install_update(self, b):
        if JOB.state == "running":
            return {"error": "Wait for the export to finish first."}
        if Update.state in ("downloading", "installing", "restarting"):
            return {"ok": True}
        threading.Thread(target=run_update, daemon=True).start()
        return {"ok": True}

    def api_update_status(self, b):
        return {"state": Update.state, "done": Update.done, "total": Update.total, "error": Update.error}

    def api_beat(self, b):
        App.last_beat = time.time()

    def api_choose(self, b):
        kind = b.get("kind", "file")
        prompts = {"file": "Choose the FCPXML exported from Final Cut Pro (File > Export XML)",
                   "bundle": "Choose the .fcpxmld exported from Final Cut Pro",
                   "output": "Choose where to save the AAF",
                   "media": "Choose a folder that contains the missing media",
                   "preset": "Choose a preset (.json)"}
        p = dialog(kind, prompts.get(kind, "Choose"), b.get("start"))
        return {"path": p}

    def api_scan(self, b):
        info = scan_info(b["path"], b.get("project"), b.get("media_roots"))
        remember(b["path"])
        return info

    def api_preview(self, b):
        return preview(b["path"], b.get("preset") or {}, b.get("project"), b.get("media_roots"))

    def api_export(self, b):
        if JOB.state == "running":
            return {"error": "An export is already running."}
        out_dir = b.get("out") or default_out(b["path"])
        th = threading.Thread(target=run_export, daemon=True,
                              args=(b["path"], b.get("preset") or {}, out_dir, b.get("project"), b.get("media_roots") or []))
        th.start()
        return {"ok": True, "out": out_dir}

    def api_job(self, b):
        return JOB.to_dict()

    def api_reveal(self, b):
        p = b.get("path")
        if p and os.path.exists(p):
            subprocess.run(["open", "-R", p] if not b.get("open") else ["open", p])

    def api_presets(self, b):
        ensure_presets()
        out = []
        for fn in sorted(os.listdir(PRESETS)):
            if fn.endswith(".json"):
                try:
                    with open(os.path.join(PRESETS, fn)) as fh:
                        d = json.load(fh)
                    out.append({"file": fn, "name": d.get("name") or fn[:-5], "preset": d})
                except Exception:
                    pass
        return {"presets": out, "folder": PRESETS}

    def api_save_preset(self, b):
        pr = b["preset"]
        path = safe_preset_file(pr.get("name") or "Preset")
        with open(path, "w") as fh:
            json.dump(pr, fh, indent=2)
        return {"file": os.path.basename(path)}

    def api_delete_preset(self, b):
        path = os.path.join(PRESETS, os.path.basename(b["file"]))
        if os.path.exists(path):
            os.remove(path)

    def api_import_preset(self, b):
        p = dialog("preset", "Choose a preset (.json)")
        if not p:
            return {"cancelled": True}
        with open(p) as fh:
            d = json.load(fh)
        return {"preset": d}


def start(port: int = 0):
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, "http://127.0.0.1:%d/#%s" % (srv.server_address[1], App.token)


if __name__ == "__main__":  # dev: python3 server.py [port]
    s, url = start(int(sys.argv[1]) if len(sys.argv) > 1 else 0)
    print(url, flush=True)
    while True:
        time.sleep(3600)

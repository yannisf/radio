#!/usr/bin/env python3
"""A browser-controlled mpv radio. Requires Python 3.9+ and mpv."""

import argparse
import json
import logging
import os
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit


LOG = logging.getLogger("radio")


def load_streams(path):
    with open(path, encoding="utf-8") as file:
        streams = json.load(file)
    if not isinstance(streams, dict) or not streams:
        raise ValueError("Configuration must be a non-empty object of stream IDs.")
    for stream_id, stream in streams.items():
        if not stream_id or not isinstance(stream, dict):
            raise ValueError("Each stream needs an ID and an object with name and url.")
        if not isinstance(stream.get("name"), str) or not stream["name"].strip():
            raise ValueError(f"Stream {stream_id!r} needs a non-empty name.")
        url = stream.get("url")
        if not isinstance(url, str) or urlsplit(url).scheme not in ("http", "https"):
            raise ValueError(f"Stream {stream_id!r} needs an HTTP or HTTPS URL.")
        if not urlsplit(url).hostname:
            raise ValueError(f"Stream {stream_id!r} needs a URL with a hostname.")
    return streams


class PlayerError(Exception):
    pass


class Player:
    """One mpv process and one serialized JSON IPC connection."""

    def __init__(self, runtime_dir, streams, audio_device=None):
        self.executable = shutil.which("mpv")
        if not self.executable:
            raise PlayerError("mpv is not installed. Install it with: sudo apt install mpv")
        self.socket_path = str(Path(runtime_dir) / "mpv.sock")
        self.streams = streams
        self.audio_device = audio_device
        self.lock = threading.RLock()
        self.process = None
        self.connection = None
        self.buffer = b""
        self.request_id = 0
        self.selected = None
        self.error = None
        self.closed = False

    def _stop_process(self):
        owned_socket = self.process is not None
        if self.connection:
            self.connection.close()
            self.connection = None
        if self.process:
            if self.process.poll() is None:
                self.process.terminate()
                try:
                    self.process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait(timeout=3)
            self.process = None
        if owned_socket:
            Path(self.socket_path).unlink(missing_ok=True)
        self.buffer = b""

    def _ensure_running(self):
        if self.closed:
            raise PlayerError("The player is shutting down.")
        if self.process and self.process.poll() is None and self.connection:
            return
        if self.process:
            self.error = "mpv exited; restarted the player. Select a stream to play again."
            LOG.warning(self.error)
        self._stop_process()
        self.selected = None
        Path(self.socket_path).unlink(missing_ok=True)
        args = [self.executable, "--no-config", "--no-video", "--idle=yes",
                "--ao=alsa", "--input-terminal=no", "--volume=100",
                f"--input-ipc-server={self.socket_path}"]
        if self.audio_device:
            args.append(f"--audio-device={self.audio_device}")
        self.process = subprocess.Popen(args, stdin=subprocess.DEVNULL)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                break
            connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            connection.settimeout(2)
            try:
                connection.connect(self.socket_path)
            except OSError:
                connection.close()
                time.sleep(0.05)
            else:
                self.connection = connection
                LOG.info("Started mpv (PID %s)", self.process.pid)
                return
        self._stop_process()
        raise PlayerError("mpv did not start its control socket. Check the service logs.")

    def _receive(self):
        while b"\n" not in self.buffer:
            chunk = self.connection.recv(65536)
            if not chunk:
                raise OSError("mpv closed its control socket")
            self.buffer += chunk
            if len(self.buffer) > 2 * 1024 * 1024:
                raise OSError("mpv response is too large")
        line, self.buffer = self.buffer.split(b"\n", 1)
        return json.loads(line)

    def _command(self, *command, optional=False):
        self._ensure_running()
        self.request_id += 1
        request_id = self.request_id
        try:
            message = {"command": list(command), "request_id": request_id}
            self.connection.sendall(json.dumps(message).encode() + b"\n")
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                reply = self._receive()
                if reply.get("event") == "end-file" and reply.get("reason") == "error":
                    self.error = reply.get("file_error", "Stream playback failed.")
                    LOG.warning("Playback failed: %s", self.error)
                if reply.get("request_id") != request_id:
                    continue
                if reply.get("error") != "success":
                    if optional and reply.get("error") in ("property unavailable", "property not found"):
                        return None
                    raise PlayerError(f"mpv: {reply.get('error', 'unknown error')}")
                return reply.get("data")
            raise OSError("mpv response timed out")
        except (OSError, ValueError) as error:
            self._stop_process()
            raise PlayerError(f"Lost connection to mpv: {error}") from error

    def start(self):
        with self.lock:
            self._ensure_running()

    def play(self, stream_id):
        with self.lock:
            self._ensure_running()
            self.error = None
            self._command("loadfile", self.streams[stream_id]["url"], "replace")
            # A previously paused stream must not leave the new stream paused.
            self._command("set_property", "pause", False)
            self.selected = stream_id

    def stop(self):
        with self.lock:
            self._command("stop")
            self.selected = None
            self.error = None

    def pause(self, paused):
        with self.lock:
            self._command("set_property", "pause", paused)

    def volume(self, volume):
        with self.lock:
            self._command("set_property", "volume", volume)

    def status(self):
        with self.lock:
            idle = self._command("get_property", "idle-active")
            paused = self._command("get_property", "pause")
            core_idle = self._command("get_property", "core-idle")
            title = self._command("get_property", "media-title", optional=True)
            volume = self._command("get_property", "volume")
            state = "stopped" if idle else "paused" if paused else "buffering" if core_idle else "playing"
            return {"state": state, "stream": None if idle else self.selected,
                    "title": title, "volume": volume, "error": self.error}

    def close(self):
        with self.lock:
            self.closed = True
            self._stop_process()


_ICON = (
    "data:image/png;base64,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"
    "t2FuL/AZ0A58na01zpAAAAAElFTkSuQmCC"
)

HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>Radio</title>
<meta name="theme-color" content="#F6F4FA" media="(prefers-color-scheme: light)">
<meta name="theme-color" content="#110E1B" media="(prefers-color-scheme: dark)">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-title" content="Radio">
<link rel="icon" type="image/png" href="__ICON__">
<link rel="apple-touch-icon" href="__ICON__">
<style>
:root{
  --bg:#F6F4FA;--fg:#2A2160;--line:#E3DEF0;--card:#ECE8F7;
  --inv-bg:linear-gradient(135deg,#3A2E9E,#7B3FB0);--inv-solid:#3A2E9E;--inv-fg:#FFFFFF;--inv-mute:#CDC3F2;
  --ok:#6BF0B5;--warn:#FFD166;--err:#FF8FA3;
  color-scheme:light;box-sizing:border-box;
  padding-top:env(safe-area-inset-top,0px);
  padding-bottom:env(safe-area-inset-bottom,0px);
}
@media (prefers-color-scheme:dark){
  :root:not([data-theme="light"]){
    --bg:#110E1B;--fg:#E4DFF7;--line:#271F3A;--card:#1D1830;
    --inv-bg:linear-gradient(135deg,#5340D6,#8E3FBF);--inv-solid:#4B38C4;--inv-fg:#FFFFFF;--inv-mute:#D3CAF7;
    --ok:#6BF0B5;--warn:#FFD166;--err:#FF8FA3;color-scheme:dark;
  }
}
:root[data-theme="dark"]{
  --bg:#110E1B;--fg:#E4DFF7;--line:#271F3A;--card:#1D1830;
  --inv-bg:linear-gradient(135deg,#5340D6,#8E3FBF);--inv-solid:#4B38C4;--inv-fg:#FFFFFF;--inv-mute:#D3CAF7;
  --ok:#6BF0B5;--warn:#FFD166;--err:#FF8FA3;color-scheme:dark;
}
*{box-sizing:border-box}
html{scroll-padding-top:env(safe-area-inset-top,0px)}
body{margin:0;background:var(--bg);color:var(--fg);font:400 15px/1.3 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
main{max-width:640px;margin:0 auto;padding:24px 16px 130px}
ul{list-style:none;margin:0;padding:0;display:flex;flex-direction:column;gap:10px}
.row{all:unset;box-sizing:border-box;display:block;width:100%;min-height:56px;padding:17px 18px;
  border-radius:14px;background:var(--card);font-size:18px;font-weight:500;
  cursor:pointer;-webkit-tap-highlight-color:transparent}
.row:focus-visible{outline:2px solid var(--fg);outline-offset:-4px}
@media (hover:hover){.row:hover{background:rgba(84,60,200,.08)}}
.row[aria-pressed="true"]{background:var(--inv-bg);color:var(--inv-fg);font-weight:600}

.player{position:fixed;left:0;right:0;bottom:0;background:var(--inv-bg);color:var(--inv-fg);
  padding:14px 16px calc(14px + env(safe-area-inset-bottom,0px));border-radius:20px 20px 0 0;
  box-shadow:0 -10px 30px rgba(30,15,80,.3),inset 0 1px 0 rgba(255,255,255,.08);transform:translateY(110%);transition:transform .25s ease}
body:not([data-status="idle"]) .player{transform:none}
.player-in{max-width:608px;margin:0 auto;display:grid;grid-template-columns:1fr auto;gap:14px;align-items:center}
.player-in>div{min-width:0}
.name,.title{overflow:hidden;white-space:nowrap}
.name{font-size:16px;font-weight:600}
.name:empty{display:none}
.status{display:flex;align-items:center;gap:8px;font-size:13px;color:var(--inv-mute);margin-top:2px}
#status{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.dot{width:8px;height:8px;border-radius:50%;background:var(--inv-mute);flex:none}
[data-status="playing"] .dot{background:var(--ok);box-shadow:0 0 0 3px rgba(94,242,192,.22)}
[data-status="connecting"] .dot,[data-status="buffering"] .dot{background:var(--warn);animation:pulse 1s infinite}
[data-status="error"] .dot,[data-status="offline"] .dot{background:var(--err)}
@keyframes pulse{50%{opacity:.3}}
.title{font-size:14px;opacity:.8;margin-top:4px}
#titletext{display:inline-block}
#titletext.scroll{animation:marquee var(--marquee-duration,12s) ease-in-out infinite alternate}
@keyframes marquee{from{transform:translateX(0)}to{transform:translateX(var(--marquee-shift,-100px))}}
.stop{all:unset;box-sizing:border-box;width:52px;height:52px;border-radius:50%;background:var(--inv-fg);color:var(--inv-solid);display:grid;place-items:center;cursor:pointer}
.stop:focus-visible{outline:2px solid var(--inv-fg);outline-offset:3px}
.stop::before{content:"";width:16px;height:16px;border-radius:3px;background:currentColor}
[data-status="offline"] .stop{display:none}
@media (prefers-reduced-motion:reduce){
  *{animation:none!important;transition:none!important}
  .title{overflow-x:auto;scrollbar-width:none}
  .title::-webkit-scrollbar{display:none}
}
</style>
</head>
<body data-status="idle">
<main><ul id="list"></ul></main>

<footer class="player" aria-live="polite">
  <div class="player-in">
    <div>
      <div class="name" id="name"></div>
      <div class="status"><span class="dot"></span><span id="status"></span></div>
      <div class="title" id="title"><span id="titletext"></span></div>
    </div>
    <button class="stop" id="stop" aria-label="Stop"></button>
  </div>
</footer>

<script>
const labels = {connecting:"Connecting…", buffering:"Buffering…", playing:"Live",
                paused:"Paused", error:"Can’t play this stream", offline:"Backend unreachable"};
let stations = [];
let view = {state:"idle", id:null};
let lastRequested = null;
let actionError = "";
const $ = id => document.getElementById(id);
const list = $("list");

async function call(path, body){
  const options = body === undefined ? {signal: AbortSignal.timeout(10000)} :
    {method: "POST", headers: {"Content-Type": "application/json"},
     body: JSON.stringify(body), signal: AbortSignal.timeout(10000)};
  const response = await fetch(path, options);
  const data = await response.json().catch(() => null);
  if (!response.ok) {
    // The backend always reports errors as JSON; anything else is an
    // intermediary or a proxy, so treat it as an unreachable server.
    const problem = new Error(data && data.error ? data.error : "Server returned " + response.status);
    problem.status = data && data.error ? response.status : 0;
    throw problem;
  }
  return data || {};
}

function failure(error){
  return {state: error.status ? "error" : "offline", id: lastRequested, detail: error.message};
}

function present(status){
  if (status.error)
    return {state: "error", id: status.stream || lastRequested, detail: status.error};
  if (status.state === "stopped")
    return {state: "idle", id: null};
  return {state: status.state, id: status.stream || lastRequested, title: status.title};
}

async function refresh(){
  try {
    view = present(await call("/api/status"));
    actionError = view.state === "error" ? "" : actionError;
  } catch (error) {
    view = failure(error);
  }
  if (actionError && view.state !== "error" && view.state !== "offline")
    view = {state: "error", id: lastRequested, detail: actionError};
  render();
}

async function act(path, body, optimistic){
  view = optimistic;
  render();
  try {
    await call(path, body);
    actionError = "";
  } catch (error) {
    actionError = error.message;
  }
  await refresh();
}

let lastTitle = null;
function setTitle(text){
  const box = $("title"), track = $("titletext");
  if (text === lastTitle) return;
  lastTitle = text;
  track.classList.remove("scroll");
  track.style.removeProperty("--marquee-shift");
  track.style.removeProperty("--marquee-duration");
  track.textContent = text;
  box.hidden = !text;
  if (!text) return;
  // Measure after layout; only scroll when the text does not fit.
  requestAnimationFrame(() => {
    const shift = track.getBoundingClientRect().width - box.clientWidth;
    if (shift > 4){
      track.style.setProperty("--marquee-shift", -Math.ceil(shift) + "px");
      track.style.setProperty("--marquee-duration", Math.max(6, Math.round(shift / 25)) + "s");
      track.classList.add("scroll");
    }
  });
}
addEventListener("resize", () => {
  const text = lastTitle;
  lastTitle = null;
  setTitle(text);
});

function render(){
  document.body.dataset.status = view.state;
  const shown = view.state !== "idle" && view.state !== "offline";
  const station = stations.find(s => s.id === view.id);
  // mpv reports the stream URL or file name as media-title when a station
  // provides no ICY title.
  const extension = (view.title || "").split(".").pop().toLowerCase();
  const title = view.title && !view.title.startsWith("http://") &&
    !view.title.startsWith("https://") && !["aac", "mp3", "m3u8"].includes(extension)
    ? view.title : "";
  $("name").textContent = shown && station ? station.name : "";
  $("status").textContent = labels[view.state] || "";
  setTitle(view.state === "error" ? (view.detail || "") :
    view.state === "playing" ? title : "");
  list.querySelectorAll(".row").forEach(row =>
    row.setAttribute("aria-pressed", String(shown && row.dataset.id === view.id)));
}

function renderList(){
  list.textContent = "";
  for (const station of stations){
    const item = document.createElement("li");
    const row = document.createElement("button");
    row.className = "row";
    row.type = "button";
    row.dataset.id = station.id;
    row.textContent = station.name;
    item.appendChild(row);
    list.appendChild(item);
  }
  render();
}

list.addEventListener("click", event => {
  const row = event.target.closest(".row");
  if (!row) return;
  lastRequested = row.dataset.id;
  act("/api/play", {stream: lastRequested}, {state: "connecting", id: lastRequested});
});

$("stop").onclick = () => {
  lastRequested = null;
  act("/api/stop", {}, {state: "idle", id: null});
};

async function start(){
  try {
    stations = (await call("/api/streams")).streams;
  } catch (error) {
    view = failure(error);
    render();
    setTimeout(start, 3000);
    return;
  }
  renderList();
  async function poll(){
    await refresh();
    setTimeout(poll, 3000);
  }
  poll();
}

start();
</script>
</body>
</html>
""".replace("__ICON__", _ICON)


class RadioServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, player, streams):
        super().__init__(address, RadioHandler)
        self.player = player
        self.streams = streams


class RadioHandler(BaseHTTPRequestHandler):
    def setup(self):
        super().setup()
        self.connection.settimeout(10)

    def log_message(self, format, *args):
        if self.path == "/api/status":
            return
        LOG.info("%s %s", self.client_address[0], format % args)

    def respond(self, status, data, html=False):
        payload = data.encode() if html else json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8" if html else "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        path = urlsplit(self.path).path
        if path == "/":
            self.respond(200, HTML, html=True)
        elif path == "/api/streams":
            self.respond(200, {"streams": [{"id": key, "name": value["name"]}
                                          for key, value in self.server.streams.items()]})
        elif path == "/api/status":
            try:
                self.respond(200, self.server.player.status())
            except PlayerError as error:
                self.respond(503, {"error": str(error)})
        else:
            self.respond(404, {"error": "Not found"})

    def do_POST(self):
        # JSON-only POSTs and same-origin checks prevent other websites controlling
        # a localhost radio through a visitor's browser. No CORS is enabled.
        origin = self.headers.get("Origin")
        if (origin is not None and origin != "http://" + self.headers.get("Host", "")) or \
                self.headers.get("Sec-Fetch-Site") == "cross-site":
            self.respond(403, {"error": "Cross-origin requests are not allowed."})
            return
        if self.headers.get("Content-Type", "").split(";", 1)[0].strip() != "application/json":
            self.respond(415, {"error": "Expected application/json."})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 4096:
                raise ValueError("Expected a JSON body of at most 4096 bytes.")
            body = json.loads(self.rfile.read(length))
            if not isinstance(body, dict):
                raise ValueError("Expected a JSON object.")
            path = urlsplit(self.path).path
            if path == "/api/play":
                stream_id = body.get("stream")
                if not isinstance(stream_id, str) or stream_id not in self.server.streams:
                    raise ValueError("Unknown stream ID.")
                self.server.player.play(stream_id)
            elif path == "/api/stop":
                self.server.player.stop()
            elif path == "/api/pause":
                if not isinstance(body.get("paused"), bool):
                    raise ValueError("paused must be true or false.")
                self.server.player.pause(body["paused"])
            elif path == "/api/volume":
                volume = body.get("volume")
                if isinstance(volume, bool) or not isinstance(volume, (int, float)) or not 0 <= volume <= 100:
                    raise ValueError("volume must be a number from 0 to 100.")
                self.server.player.volume(volume)
            else:
                self.respond(404, {"error": "Not found"})
                return
            self.respond(200, {"ok": True})
        except (ValueError, UnicodeError) as error:
            self.respond(400, {"error": str(error)})
        except PlayerError as error:
            self.respond(503, {"error": str(error)})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=str(Path.home() / ".config/radio/streams.json"))
    parser.add_argument("--host", default="0.0.0.0", help="Bind IPv4 address/hostname (default: all interfaces)")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--runtime-dir", help="Private directory for mpv's control socket")
    parser.add_argument("--audio-device",  default="alsa/plughw:CARD=Headphones,DEV=0", help="mpv audio device, e.g. alsa/default")
    args = parser.parse_args()
    # if args.host in ("0.0.0.0", "::", ""):
    #     parser.error("Use a specific local IP/hostname, not a wildcard bind address.")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    def terminate(signum, frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, terminate)
    player = None
    server = None
    temporary = None
    try:
        streams = load_streams(args.config)
        if args.runtime_dir:
            runtime = Path(args.runtime_dir)
            runtime.mkdir(mode=0o700, parents=True, exist_ok=True)
            stat = runtime.stat()
            if stat.st_uid != os.getuid() or stat.st_mode & 0o077:
                raise ValueError("Runtime directory must be owned by you with permissions 0700.")
        else:
            temporary = tempfile.TemporaryDirectory(prefix="radio-")
            runtime = Path(temporary.name)
        player = Player(runtime, streams, args.audio_device)
        # Bind before starting mpv so a duplicate service cannot launch a player.
        server = RadioServer((args.host, args.port), player, streams)
        player.start()
        LOG.info("Radio UI: http://%s:%s", args.host, server.server_port)
        if server.server_address[0] != "127.0.0.1":
            LOG.warning("No authentication: anyone who can reach this address can control playback.")
        server.serve_forever()
    except KeyboardInterrupt:
        LOG.info("Stopping radio")
    except (OSError, ValueError, PlayerError) as error:
        LOG.error("%s", error)
        return 1
    finally:
        if server:
            server.server_close()
        if player:
            player.close()
        if temporary:
            temporary.cleanup()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

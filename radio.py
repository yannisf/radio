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


HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Radio</title>
  <style>
    body { font: 18px system-ui, sans-serif; max-width: 480px; margin: 3rem auto;
           padding: 0 1rem; color: #202830; background: #f4f6f8; }
    main { background: white; padding: 1.5rem; border-radius: 12px; }
    select, button { font: inherit; padding: .65rem; }
    select { width: 100%; margin: .5rem 0 1rem; }
    .buttons { display: flex; flex-wrap: wrap; gap: .5rem; }
    button { cursor: pointer; }
    input { width: 100%; margin-top: .75rem; }
    #status, #title { overflow-wrap: anywhere; }
    #title { color: #52616b; min-height: 1.5em; }
    #error { color: #a01818; overflow-wrap: anywhere; }
  </style>
</head>
<body>
<main>
  <h1>Radio</h1>
  <label for="streams">Stream</label>
  <select id="streams" disabled></select>
  <div class="buttons">
    <button id="play" disabled>Play</button>
    <button id="pause" disabled>Pause</button>
    <button id="stop">Stop</button>
  </div>
  <p id="status" role="status">Connecting…</p>
  <p id="title"></p>
  <label for="volume">Volume: <span id="level">50</span>%</label>
  <input id="volume" type="range" min="0" max="100" value="50">
  <p id="error" role="alert"></p>
</main>
<script>
  const el = id => document.getElementById(id);
  let paused = false;
  let streams = [];
  let actionError = '';
  async function api(path, body) {
    const response = await fetch('/api/' + path, {
      method: body === undefined ? 'GET' : 'POST',
      headers: body === undefined ? {} : {'Content-Type': 'application/json'},
      body: body === undefined ? undefined : JSON.stringify(body),
      signal: AbortSignal.timeout(10000)
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || 'Request failed');
    return data;
  }
  async function refresh() {
    try {
      const status = await api('status');
      const stream = streams.find(s => s.id === status.stream);
      el('status').textContent = status.state + (stream ? ' — ' + stream.name : '');
      el('title').textContent = status.title || '';
      paused = status.state === 'paused';
      el('pause').textContent = paused ? 'Resume' : 'Pause';
      el('pause').disabled = status.state === 'stopped';
      if (document.activeElement !== el('volume')) {
        el('volume').value = Math.round(status.volume);
        el('level').textContent = Math.round(status.volume);
      }
      el('error').textContent = actionError || status.error || '';
    } catch (error) {
      el('status').textContent = 'Disconnected';
      el('error').textContent = error.message;
    }
  }
  async function command(path, body) {
    try {
      await api(path, body);
      actionError = '';
    } catch (error) {
      actionError = error.message;
    }
    await refresh();
  }
  el('play').onclick = () => command('play', {stream: el('streams').value});
  el('stop').onclick = () => command('stop', {});
  el('pause').onclick = () => command('pause', {paused: !paused});
  el('volume').oninput = () => el('level').textContent = el('volume').value;
  el('volume').onchange = () => command('volume', {volume: Number(el('volume').value)});
  async function initialize() {
    try {
      streams = (await api('streams')).streams;
      for (const stream of streams) {
        const option = document.createElement('option');
        option.value = stream.id;
        option.textContent = stream.name;
        el('streams').append(option);
      }
      el('streams').disabled = false;
      el('play').disabled = false;
    } catch (error) {
      el('error').textContent = error.message;
      setTimeout(initialize, 3000);
      return;
    }
    async function poll() {
      await refresh();
      setTimeout(poll, 3000);
    }
    poll();
  }
  initialize();
</script>
</body>
</html>
"""


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

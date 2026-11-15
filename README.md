# Radio

A tiny browser-controlled music player for Debian. Python serves the HTML UI
and manages one persistent **mpv** process through its private Unix socket.
No Python dependencies, frontend build, database, or external web server.

The music plays on the **Debian machine's audio output**, not in the browser.
Closing the browser does not stop playback. The UI is a mobile-first list of
stations: tap one to start it, and a panel slides up showing the station, its
current track title when the stream provides one, and any playback error, plus
a stop button. Status is refreshed every 3 seconds.

## 1. Install and test audio

Copy this `radio` directory to Debian. From inside that directory:

```sh
sudo apt update
sudo apt install python3 mpv
mpv --no-config --no-video https://ice2.somafm.com/groovesalad-128-mp3
```

Press Ctrl+C after checking that you hear music. Use the **same normal user**
for this test and the service installation below. Do not run the player as root.
Python 3.9+ is required (Debian 11 or newer).

## 2. Configure streams

Install the program and copy the included `streams.json` to your user's
configuration directory:

```sh
sudo install -d -m 755 /opt/radio
sudo install -m 644 radio.py /opt/radio/radio.py
mkdir -p ~/.config/radio
cp -n streams.json ~/.config/radio/streams.json
chmod 600 ~/.config/radio/streams.json
nano ~/.config/radio/streams.json
```

`cp -n` leaves an existing configuration untouched. The file is ordinary JSON:

```json
{
  "jazz": {
    "name": "My Jazz Station",
    "url": "https://example.com/jazz-stream"
  },
  "classical": {
    "name": "My Classical Station",
    "url": "https://example.com/classical-stream"
  }
}
```

- The outer keys (`jazz`, `classical`) are unique stream IDs.
- `name` is the label displayed in the browser.
- `url` is an actual **HTTP/HTTPS audio stream**, not the station's website.
- Replace the URLs above, or keep the real stations from the included file.
  Availability of third-party streams is not guaranteed.
- Add as many entries as needed. JSON does not allow comments or trailing commas.
- The service accepts only configured IDs; it cannot execute arbitrary commands
  or play URLs submitted by a browser.
- Configuration is read at startup. **Restart the service after editing it,
  then reload the browser page to update the dropdown.**

## 3. Quick manual run (optional)

```sh
python3 /opt/radio/radio.py
```

Open <http://127.0.0.1:8080> on Debian, or use the SSH tunnel below from another
computer. Press Ctrl+C to stop both the Python service and its mpv child before
installing/starting the systemd service.

## 4. Install the systemd user service

This is intentionally a **user service**: mpv runs under the user whose console
audio already works, including access to that user's PipeWire/PulseAudio session.
Do not put this unit in `/etc/systemd/system`, and do not use `sudo systemctl`.

From the copied `radio` directory, as that same normal user:

```sh
mkdir -p ~/.config/systemd/user
install -m 644 radio.service ~/.config/systemd/user/radio.service
systemctl --user daemon-reload
systemctl --user enable --now radio.service
```

To start the user service at boot and keep it running after logout:

```sh
sudo loginctl enable-linger "$USER"
```

The service starts with playback **stopped**, ready for you to choose a station.
Systemd restarts Python if it crashes. If mpv exits, the next status/control
request starts a new player; playback is not automatically resumed. Network or
stream errors are shown when mpv reports them; failed streams are not retried
automatically. This avoids an endless retry loop for a broken station.

### Manage it

Run these as the user who installed the service:

```sh
systemctl --user status radio.service
systemctl --user restart radio.service
systemctl --user stop radio.service
systemctl --user start radio.service
systemctl --user disable --now radio.service
journalctl --user -u radio.service -f
```

`restart` is also how you reload the streams configuration. Re-enabling automatic
startup is `systemctl --user enable --now radio.service`. Changes to the unit file
itself require `systemctl --user daemon-reload` followed by a restart.

Each browser polls `/api/status` every 3 seconds; those polls are not written to
the journal, so it only fills with control requests and warnings.

If `systemctl --user` reports that it cannot connect to the bus, log in directly
as the target user (for example over SSH), rather than running these commands
from a root shell or a plain `su` session.

## 5. Open the UI

The service listens on **all IPv4 interfaces** on port 8080 (`0.0.0.0:8080`).
There is **no built-in authentication or TLS**, and the service does not check
the request's hostname. Anyone who can reach port 8080 can control playback,
so the safest access path from another computer is an SSH tunnel. The service
logs a warning at startup because it is not bound to localhost.

On your laptop:

```sh
ssh -N -L 8080:127.0.0.1:8080 your-user@debian-host
```

Keep that SSH connection open and visit <http://localhost:8080> in your laptop's
browser. This gives you encrypted, authenticated remote access without needing
to add a login page.

If port 8080 is already used on your laptop, use a different local port:

```sh
ssh -N -L 8081:127.0.0.1:8080 your-user@debian-host
```

Then visit <http://localhost:8081>.

### Direct access on a trusted LAN or private VPN

Because the service already listens on all interfaces, it is reachable at
Debian's private IP, for example `http://192.168.1.50:8080`. Use a stable IP and
restrict access with your firewall. Plain LAN HTTP is not encrypted. Never
forward this port from your router or expose it to the public internet.

To bind only one address instead of all interfaces, add an override:

```sh
systemctl --user edit radio.service
```

For example, if Debian's private IP is `192.168.1.50`:

```ini
[Service]
ExecStart=
ExecStart=/usr/bin/python3 /opt/radio/radio.py --config %h/.config/radio/streams.json --runtime-dir %t/radio --host 192.168.1.50
```

```sh
systemctl --user daemon-reload
systemctl --user restart radio.service
```

Open `http://192.168.1.50:8080`. You can instead bind to your private VPN IPv4
address. If that address is not available at boot, systemd will retry starting
the service every 3 seconds.

## Audio troubleshooting

First inspect `journalctl --user -u radio.service -f` and confirm manual mpv
playback works under the same user. The service uses `--no-config`, so it does
not inherit your personal mpv configuration. It starts mpv with `--ao=alsa`,
`--volume=100`, and `--audio-device=alsa/plughw:CARD=Headphones,DEV=0`.
The UI has no volume control, so playback always uses that player volume.

To list mpv's audio devices:

```sh
mpv --audio-device=help
```

The default device is a specific ALSA card and will not exist on every machine.
Choose a device from that list and pass it to the service with an override:

```ini
[Service]
ExecStart=
ExecStart=/usr/bin/python3 /opt/radio/radio.py --config %h/.config/radio/streams.json --runtime-dir %t/radio --audio-device alsa/default
```

The `--ao=alsa` output driver is fixed in `radio.py`; using PipeWire or
PulseAudio as the mpv audio output requires editing that argument. Stop the
service before testing a manual player if your ALSA device is exclusive.

On a headless ALSA machine, the service user may need access to `/dev/snd`:

```sh
sudo usermod -aG audio "$USER"
```

Then reboot (or fully restart the user's session/manager) to apply the new group
membership. Desktop PipeWire/PulseAudio setups normally do not need this group.
Enabling linger keeps the user manager alive, but does not itself configure an
audio server: make sure your existing audio setup also works after reboot and
logout. mpv software volume is separate from the system/hardware mixer volume.

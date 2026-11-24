# Radio

A tiny browser-controlled music player for Debian. Python serves the HTML UI
and manages one persistent **mpv** process through its private Unix socket.
No Python dependencies, frontend build, database, or external web server.

The music plays on the **Debian machine's audio output**, not in the browser.
Closing the browser does not stop playback. The UI is a mobile-first list of
stations grouped by network: tap a group to expand it, tap a station to start
it, and a panel slides up showing the station, its current track title when the
stream provides one, and any playback error, plus a stop button. Status is
refreshed every 3 seconds.

## 1. Install and test audio

Copy this `radio` directory to Debian. From inside that directory:

```sh
sudo apt update
sudo apt install python3 mpv
sudo -u kodi mpv --no-config --no-video https://ice2.somafm.com/groovesalad-128-mp3
```

Press Ctrl+C after checking that you hear music. The service below runs as the
unprivileged `kodi` account (change `User=` if a different account owns the
audio output), so test audio as that same account. Do not run the player as
root. Python 3.9+ is required (Debian 11 or newer).

## 2. Install the program and configure streams

The included `deploy.sh` installs the script, the configuration and the systemd
unit in one step (see [Deploying updates from git](#deploying-updates-from-git)).
That is the recommended path. To install the files manually instead:

```sh
sudo install -d -o root -g root -m 755 /opt/homeradio /etc/homeradio
sudo install -o root -g root -m 644 radio.py /opt/homeradio/radio.py
sudo install -o kodi -m 600 streams.json /etc/homeradio/streams.json
sudoedit /etc/homeradio/streams.json
```

Replace the included stream URLs with the desired stations. The file is
ordinary JSON, organized into groups:

```json
{
  "jazz": {
    "name": "Jazz",
    "myjazz": {
      "name": "My Jazz Station",
      "url": "https://example.com/jazz-stream"
    }
  },
  "classical": {
    "name": "Classical",
    "myclassical": {
      "name": "My Classical Station",
      "url": "https://example.com/classical-stream"
    }
  }
}
```

- The outer keys (`jazz`, `classical`) are unique group IDs. Their `name` is
  the heading shown in the browser; tap it to expand or collapse the group.
  Only one group is expanded at a time, and all start collapsed.
- Each group contains one or more stations below its heading. The station keys
  (`myjazz`, `myclassical`) are unique stream IDs and must not be repeated
  across groups.
- A station's `name` is the label displayed in the browser.
- `url` is an actual **HTTP/HTTPS audio stream**, not the station's website.
- Replace the URLs above, or keep the real stations from the included file.
  Availability of third-party streams is not guaranteed.
- Add as many groups and stations as needed. JSON does not allow comments or
  trailing commas.
- The service accepts only configured IDs; it cannot execute arbitrary commands
  or play URLs submitted by a browser.
- Configuration is read at startup. **Restart the service after editing it,
  then reload the browser page to update the station list.**

## 3. Quick manual run (optional)

```sh
sudo -u kodi python3 /opt/homeradio/radio.py \
  --config /etc/homeradio/streams.json \
  --runtime-dir /tmp/homeradio
```

Open <http://127.0.0.1:8080> on Debian, or use the SSH tunnel below from another
computer. Press Ctrl+C to stop both the Python service and its mpv child before
installing/starting the systemd service.

## 4. Install the system service

This is a **system service** running as the unprivileged `kodi` account. It
starts at boot without requiring a login, and `SupplementaryGroups=audio` gives
it access to the ALSA devices.

Create `/etc/systemd/system/homeradio.service`:

```ini
[Unit]
Description=Home Radio
Wants=network-online.target
After=network-online.target

[Service]
Type=simple
User=kodi
SupplementaryGroups=audio
ExecStart=/usr/bin/python3 /opt/homeradio/radio.py --config /etc/homeradio/streams.json --runtime-dir /run/homeradio --host 0.0.0.0 --port 8080
Restart=on-failure
RestartSec=3
RuntimeDirectory=homeradio
RuntimeDirectoryMode=0700
UMask=0077
NoNewPrivileges=true
KillMode=control-group
TimeoutStopSec=15

[Install]
WantedBy=multi-user.target
```

`RuntimeDirectory=homeradio` has systemd create `/run/homeradio` for the
service at startup, and remove it when the service stops. The service starts
with playback **stopped**, ready for you to choose a station. Systemd restarts
Python if it crashes. If mpv exits, the next status/control request starts a
new player; playback is not automatically resumed. Network or stream errors are
shown when mpv reports them; failed streams are not retried automatically. This
avoids an endless retry loop for a broken station.

Enable and start the service:

```sh
sudo systemctl daemon-reload
sudo systemctl enable --now homeradio.service
sudo systemctl status homeradio.service
```

### Manage it

```sh
sudo systemctl status homeradio.service
sudo systemctl restart homeradio.service
sudo systemctl stop homeradio.service
sudo systemctl start homeradio.service
sudo systemctl disable --now homeradio.service
sudo journalctl -u homeradio.service -f
```

`restart` is also how you reload the streams configuration. Re-enabling
automatic startup is `sudo systemctl enable --now homeradio.service`. Changes to
the unit file itself require `sudo systemctl daemon-reload` followed by a
restart.

### Deploying updates from git

After pulling a new version, run the deploy script from the checkout:

```sh
git pull
sudo ./deploy.sh
```

It validates `streams.json`, installs `radio.py` to `/opt/homeradio` as
`root:root` mode `0644`, installs `streams.json` to `/etc/homeradio` owned by
the service account with mode `0600`, and installs the unit file if it changed.
It then enables and restarts `homeradio.service` and reports whether the
service came back up.

Before changing anything, the script runs preflight checks: root access, the
service user and `audio` group, `python3` (3.9+) and `mpv`, unit syntax and its
`User=`/`ExecStart=`, target paths and permissions, configuration validity,
whether the service account can read the configuration, port availability and
audio device access. To run only those checks without deploying:

```sh
sudo ./deploy.sh --check
```

`--check` changes nothing and exits non-zero if a required check fails. It can
be combined with `--keep-config` to validate the configuration that would
actually be kept:

```sh
sudo ./deploy.sh --check --keep-config
```

A replaced `streams.json` is saved first as `/etc/homeradio/streams.json.bak`,
so server-side edits are not lost. If you want to keep those edits in place
instead of deploying the version from the checkout, use:

```sh
sudo ./deploy.sh --keep-config
```

The first run also installs and enables the service, so the manual installation
steps in sections 2 and 4 are optional when using the script.

Each browser polls `/api/status` every 3 seconds; those polls are not written to
the journal, so it only fills with control requests and warnings.

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

To bind only one address instead of all interfaces, add a drop-in:

```sh
sudo systemctl edit homeradio.service
```

For example, if Debian's private IP is `192.168.1.50`:

```ini
[Service]
ExecStart=
ExecStart=/usr/bin/python3 /opt/homeradio/radio.py --config /etc/homeradio/streams.json --runtime-dir /run/homeradio --host 192.168.1.50
```

```sh
sudo systemctl daemon-reload
sudo systemctl restart homeradio.service
```

Open `http://192.168.1.50:8080`. You can instead bind to your private VPN IPv4
address. If that address is not available at boot, systemd will retry starting
the service every 3 seconds.

## Audio troubleshooting

First inspect `sudo journalctl -u homeradio.service -f` and confirm manual mpv
playback works under the `kodi` account. The service uses `--no-config`, so it
does not inherit anyone's personal mpv configuration. It starts mpv with
`--ao=alsa`, `--volume=100`, and
`--audio-device=alsa/plughw:CARD=Headphones,DEV=0`.
The UI has no volume control, so playback always uses that player volume.

To list mpv's audio devices as the service account:

```sh
sudo -u kodi mpv --audio-device=help
```

The default device is a specific ALSA card and will not exist on every machine.
Choose a device from that list and pass it to the service with a drop-in:

```sh
sudo systemctl edit homeradio.service
```

```ini
[Service]
ExecStart=
ExecStart=/usr/bin/python3 /opt/homeradio/radio.py --config /etc/homeradio/streams.json --runtime-dir /run/homeradio --audio-device alsa/default
```

```sh
sudo systemctl daemon-reload
sudo systemctl restart homeradio.service
```

The `--ao=alsa` output driver is fixed in `radio.py`; using PipeWire or
PulseAudio as the mpv audio output requires editing that argument. Stop the
service before testing a manual player if your ALSA device is exclusive.

On a headless ALSA machine, the service account may need access to `/dev/snd`.
`SupplementaryGroups=audio` in the unit already grants this; if you run the
player manually instead, add the account to the group:

```sh
sudo usermod -aG audio kodi
```

Then log out and back in (or reboot) to apply the new group membership.
Desktop PipeWire/PulseAudio setups normally do not need this group. mpv
software volume is separate from the system/hardware mixer volume.

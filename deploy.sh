#!/usr/bin/env bash
# Deploy Home Radio from this checkout to the system locations and restart it.
#
# Usage: sudo ./deploy.sh [--check] [--keep-config]
set -euo pipefail

SERVICE_NAME=homeradio.service
SERVICE_USER=kodi
INSTALL_DIR=/opt/homeradio
CONFIG_DIR=/etc/homeradio
CONFIG_FILE=$CONFIG_DIR/streams.json
UNIT_FILE=/etc/systemd/system/$SERVICE_NAME

KEEP_CONFIG=0
CHECK_ONLY=0
HAVE_USER=0
SERVICE_GROUP=
FAILURES=0
WARNINGS=0

usage() {
    cat <<'EOF'
Usage: sudo ./deploy.sh [--check] [--keep-config]

Verifies the deployment prerequisites, then installs radio.py to
/opt/homeradio, streams.json to /etc/homeradio and the systemd unit to
/etc/systemd/system, and enables and restarts the service.

Options:
  --check        Only run the prerequisite checks; change nothing.
  --keep-config  Keep an existing /etc/homeradio/streams.json instead of
                 replacing it with the one from this checkout.
  -h, --help     Show this help.
EOF
}

die() {
    echo "error: $*" >&2
    exit 1
}

check_ok() {
    printf '[ ok ] %s\n' "$*"
}

check_warn() {
    printf '[warn] %s\n' "$*"
    WARNINGS=$((WARNINGS + 1))
}

check_fail() {
    printf '[fail] %s\n' "$*" >&2
    FAILURES=$((FAILURES + 1))
}

summarize_checks() {
    if (( FAILURES > 0 )); then
        echo "error: $FAILURES check(s) failed; nothing was changed" >&2
        return 1
    fi
    if (( WARNINGS > 0 )); then
        echo "All required checks passed, with $WARNINGS warning(s)."
    else
        echo "All checks passed."
    fi
}

preflight() {
    echo "Checking deployment prerequisites:"

    if [[ $EUID -eq 0 ]]; then
        check_ok "running as root"
    else
        check_fail "not running as root; run this script with sudo"
    fi

    local missing=() cmd
    for cmd in python3 mpv systemctl runuser; do
        if ! command -v "$cmd" >/dev/null 2>&1; then
            missing+=("$cmd")
        fi
    done
    if (( ${#missing[@]} == 0 )); then
        check_ok "required commands found: python3, mpv, systemctl, runuser"
    else
        check_fail "missing required command(s): ${missing[*]}"
    fi

    if command -v python3 >/dev/null 2>&1; then
        if python3 -c 'import sys; raise SystemExit(sys.version_info < (3, 9))'; then
            check_ok "python3 $(python3 -c 'import platform; print(platform.python_version())') (3.9+ required)"
        else
            check_fail "python3 is older than 3.9"
        fi
    fi

    local file
    for file in radio.py streams.json homeradio.service; do
        if [[ -f $SRC_DIR/$file && -r $SRC_DIR/$file ]]; then
            check_ok "source file $file"
        else
            check_fail "missing or unreadable source file $SRC_DIR/$file"
        fi
    done

    if getent passwd "$SERVICE_USER" >/dev/null 2>&1; then
        HAVE_USER=1
        SERVICE_GROUP=$(id -gn "$SERVICE_USER")
        check_ok "service user '$SERVICE_USER' exists (group '$SERVICE_GROUP')"
    else
        check_fail "service user '$SERVICE_USER' does not exist; edit SERVICE_USER in ${BASH_SOURCE[0]}"
    fi

    if getent group audio >/dev/null 2>&1; then
        check_ok "group 'audio' exists"
    else
        check_fail "group 'audio' does not exist; the unit requires SupplementaryGroups=audio"
    fi

    if [[ -d /run/systemd/system ]]; then
        check_ok "systemd is running"
    else
        check_fail "systemd is not running; this script requires a systemd host"
    fi

    local unit_user exec_python
    unit_user=$(sed -n 's/^User=//p' "$SRC_DIR/homeradio.service" 2>/dev/null | head -n1)
    if [[ -z $unit_user ]]; then
        check_fail "homeradio.service has no User= line"
    elif [[ $unit_user == "$SERVICE_USER" ]]; then
        check_ok "unit runs as '$SERVICE_USER'"
    else
        check_fail "unit User=$unit_user does not match SERVICE_USER=$SERVICE_USER"
    fi

    exec_python=$(sed -n 's/^ExecStart=\([^ ]*\).*/\1/p' "$SRC_DIR/homeradio.service" 2>/dev/null | head -n1)
    if [[ -n $exec_python && -x $exec_python ]]; then
        check_ok "ExecStart interpreter $exec_python is executable"
    else
        check_fail "ExecStart interpreter '${exec_python:-<none>}' is missing or not executable"
    fi

    if command -v systemd-analyze >/dev/null 2>&1; then
        local verify_output
        if verify_output=$(systemd-analyze verify "$SRC_DIR/homeradio.service" 2>&1); then
            check_ok "unit file passes systemd-analyze verify"
        else
            check_fail "systemd-analyze verify failed: $verify_output"
        fi
    fi

    local path paths_ok=1
    for path in "$INSTALL_DIR" "$CONFIG_DIR" "$(dirname "$UNIT_FILE")"; do
        if [[ -e $path && ! -d $path ]]; then
            check_fail "$path exists but is not a directory"
            paths_ok=0
        elif [[ -d $path && ! -w $path ]]; then
            check_fail "$path is not writable"
            paths_ok=0
        fi
    done
    if (( paths_ok == 1 )); then
        check_ok "target directories are usable"
    fi

    local target targets_ok=1
    for target in "$INSTALL_DIR/radio.py" "$CONFIG_FILE" "$UNIT_FILE"; do
        if [[ -e $target && ! -f $target ]]; then
            check_fail "$target exists but is not a regular file"
            targets_ok=0
        fi
    done
    if (( targets_ok == 1 )); then
        check_ok "target file locations are usable"
    fi

    local config_to_validate=$SRC_DIR/streams.json
    if [[ $KEEP_CONFIG -eq 1 && -f $CONFIG_FILE ]]; then
        config_to_validate=$CONFIG_FILE
        check_ok "keeping existing $CONFIG_FILE"
    fi
    if [[ -f $config_to_validate ]] && command -v python3 >/dev/null 2>&1; then
        if PYTHONDONTWRITEBYTECODE=1 python3 - "$SRC_DIR" "$config_to_validate" <<'PY'
import sys

sys.path.insert(0, sys.argv[1])
from radio import load_streams

try:
    load_streams(sys.argv[2])
except (OSError, ValueError) as error:
    sys.exit(f"invalid configuration: {error}")
PY
        then
            check_ok "configuration $config_to_validate is valid"
        else
            check_fail "configuration $config_to_validate is invalid"
        fi
    fi

    if [[ -f $CONFIG_FILE ]]; then
        local owner mode
        owner=$(stat -c '%U:%G' "$CONFIG_FILE")
        mode=$(stat -c '%a' "$CONFIG_FILE")
        if [[ $owner == "$SERVICE_USER:$SERVICE_GROUP" && $mode == 600 ]]; then
            check_ok "$CONFIG_FILE is owned by $owner with mode 600"
        elif [[ $KEEP_CONFIG -eq 1 ]]; then
            check_warn "$CONFIG_FILE is $owner mode $mode; --keep-config leaves it unchanged"
        else
            check_warn "$CONFIG_FILE is $owner mode $mode; deploy will reset it to $SERVICE_USER:$SERVICE_GROUP mode 600"
        fi

        if (( HAVE_USER == 1 )) && command -v runuser >/dev/null 2>&1; then
            if runuser -u "$SERVICE_USER" -- test -r "$CONFIG_FILE" 2>/dev/null; then
                check_ok "$SERVICE_USER can read $CONFIG_FILE"
            elif [[ $KEEP_CONFIG -eq 1 ]]; then
                check_fail "$SERVICE_USER cannot read $CONFIG_FILE; --keep-config would break the service"
            else
                check_warn "$SERVICE_USER cannot read $CONFIG_FILE; deploy will fix its permissions"
            fi
        fi
    fi

    local snd_group snd_mode
    if [[ ! -e /dev/snd ]]; then
        check_warn "/dev/snd not found; ALSA playback may fail"
    else
        snd_group=$(stat -c '%G' /dev/snd)
        snd_mode=$(stat -c '%a' /dev/snd)
        if [[ $snd_group == audio ]] && (( (0${snd_mode: -2:1} & 5) == 5 )); then
            check_ok "/dev/snd is accessible to group 'audio'"
        elif (( (0${snd_mode: -1:1} & 5) == 5 )); then
            check_ok "/dev/snd is world-accessible"
        else
            check_warn "/dev/snd ($snd_group mode $snd_mode) may not be accessible to the service; check the ALSA setup"
        fi
    fi

    local port listeners main_pid
    port=$(sed -n 's/.*--port[= ]\([0-9]\{1,\}\).*/\1/p' "$SRC_DIR/homeradio.service" 2>/dev/null | head -n1)
    port=${port:-8080}
    if command -v ss >/dev/null 2>&1 && command -v systemctl >/dev/null 2>&1; then
        listeners=$(ss -H -ltnp "sport = :$port" 2>/dev/null || true)
        if [[ -z $listeners ]]; then
            check_ok "TCP port $port is free"
        else
            main_pid=$(systemctl show -p MainPID --value "$SERVICE_NAME" 2>/dev/null || true)
            main_pid=${main_pid:-0}
            if [[ $main_pid != 0 && $listeners == *"pid=$main_pid,"* ]]; then
                check_ok "TCP port $port is used by the running $SERVICE_NAME"
            else
                check_fail "TCP port $port is already in use: $listeners"
            fi
        fi
    else
        check_warn "cannot verify that TCP port $port is free (ss or systemctl is missing)"
    fi
}

for arg in "$@"; do
    case $arg in
        --check) CHECK_ONLY=1 ;;
        --keep-config) KEEP_CONFIG=1 ;;
        -h|--help) usage; exit 0 ;;
        *) usage >&2; die "unknown option: $arg" ;;
    esac
done

[[ $EUID -eq 0 ]] || die "run this script as root: sudo ${BASH_SOURCE[0]}"

SRC_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)

preflight
summarize_checks || exit 1

if [[ $CHECK_ONLY -eq 1 ]]; then
    exit 0
fi

install -d -o root -g root -m 0755 "$INSTALL_DIR" "$CONFIG_DIR"
install -o root -g root -m 0644 "$SRC_DIR/radio.py" "$INSTALL_DIR/radio.py"
echo "Installed $INSTALL_DIR/radio.py"

if [[ $KEEP_CONFIG -eq 1 && -f $CONFIG_FILE ]]; then
    echo "Kept existing $CONFIG_FILE"
else
    if [[ -f $CONFIG_FILE ]] && ! cmp -s "$SRC_DIR/streams.json" "$CONFIG_FILE"; then
        install -o "$SERVICE_USER" -g "$SERVICE_GROUP" -m 0600 \
            "$CONFIG_FILE" "$CONFIG_FILE.bak"
        echo "Saved previous configuration to $CONFIG_FILE.bak"
    fi
    install -o "$SERVICE_USER" -g "$SERVICE_GROUP" -m 0600 \
        "$SRC_DIR/streams.json" "$CONFIG_FILE"
    echo "Installed $CONFIG_FILE"
fi

if [[ ! -f $UNIT_FILE ]] || ! cmp -s "$SRC_DIR/homeradio.service" "$UNIT_FILE"; then
    install -d -o root -g root -m 0755 "$(dirname "$UNIT_FILE")"
    install -o root -g root -m 0644 "$SRC_DIR/homeradio.service" "$UNIT_FILE"
    systemctl daemon-reload
    echo "Installed $UNIT_FILE"
fi

systemctl enable "$SERVICE_NAME" >/dev/null
systemctl restart "$SERVICE_NAME"

sleep 1
if ! systemctl is-active --quiet "$SERVICE_NAME"; then
    echo "error: $SERVICE_NAME failed to start; recent log:" >&2
    journalctl -u "$SERVICE_NAME" -n 20 --no-pager >&2
    exit 1
fi

systemctl status --no-pager --lines=0 "$SERVICE_NAME" || true
echo "Home Radio is running at http://localhost:8080"

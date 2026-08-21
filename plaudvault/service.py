"""Install plaudvault as background services.

Two units, deliberately separate:

  console — always up while you're logged in, restarted if it dies. Cheap: it's an
            idle web server until you open a tab.
  sync    — runs the pipeline on a schedule (default 4x/day). Kept apart from the
            console so a long transcription run can never take the UI down with it,
            and so you can change one cadence without touching the other.

Neither ever prunes. Deleting from Plaud's cloud stays a thing you do on purpose.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

LABEL_WEB = "com.plaudvault.console"
LABEL_SYNC = "com.plaudvault.sync"

DEFAULT_HOURS = [7, 12, 18, 22]


def _executable() -> str:
    """Absolute path to the plaudctl entry point in the running environment."""
    exe = Path(sys.argv[0]).resolve()
    if exe.name == "plaudctl" and exe.exists():
        return str(exe)
    candidate = Path(sys.prefix) / "bin" / "plaudctl"
    return str(candidate) if candidate.exists() else f"{sys.executable} -m plaudvault.cli"


def _log_dir() -> Path:
    if sys.platform == "darwin":
        return Path.home() / "Library/Logs"
    return Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "plaudvault"


# ------------------------------------------------------------------------ macOS


def _launch_agents() -> Path:
    return Path.home() / "Library/LaunchAgents"


def _plist_web(exe: str, logs: Path) -> str:
    args = "".join(f"        <string>{a}</string>\n" for a in [*exe.split(), "web"])
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>{LABEL_WEB}</string>
    <key>ProgramArguments</key>
    <array>
{args}    </array>
    <key>RunAtLoad</key><true/>
    <key>KeepAlive</key><true/>
    <key>StandardOutPath</key><string>{logs}/plaudvault-console.log</string>
    <key>StandardErrorPath</key><string>{logs}/plaudvault-console.log</string>
    <key>ProcessType</key><string>Background</string>
</dict>
</plist>
"""


def _plist_sync(exe: str, logs: Path, hours: list[int]) -> str:
    args = "".join(f"        <string>{a}</string>\n" for a in [*exe.split(), "run"])
    intervals = "".join(
        f"        <dict><key>Hour</key><integer>{h}</integer>"
        f"<key>Minute</key><integer>0</integer></dict>\n"
        for h in hours
    )
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>{LABEL_SYNC}</string>
    <key>ProgramArguments</key>
    <array>
{args}    </array>
    <key>StartCalendarInterval</key>
    <array>
{intervals}    </array>
    <key>RunAtLoad</key><false/>
    <key>StandardOutPath</key><string>{logs}/plaudvault-sync.log</string>
    <key>StandardErrorPath</key><string>{logs}/plaudvault-sync.log</string>
    <key>ProcessType</key><string>Background</string>
    <key>LowPriorityIO</key><true/>
</dict>
</plist>
"""


def _launchctl(*args: str) -> None:
    subprocess.run(["launchctl", *args], capture_output=True)


def _install_macos(hours: list[int]) -> int:
    exe, logs = _executable(), _log_dir()
    logs.mkdir(parents=True, exist_ok=True)
    agents = _launch_agents()
    agents.mkdir(parents=True, exist_ok=True)

    for label, body in (
        (LABEL_WEB, _plist_web(exe, logs)),
        (LABEL_SYNC, _plist_sync(exe, logs, hours)),
    ):
        path = agents / f"{label}.plist"
        _launchctl("bootout", f"gui/{os.getuid()}/{label}")
        path.write_text(body)
        _launchctl("bootstrap", f"gui/{os.getuid()}", str(path))
        print(f"  installed {path}")

    print(f"\n  console: always on, restarted if it exits")
    print(f"  sync:    {', '.join(f'{h:02d}:00' for h in hours)} daily")
    print(f"  logs:    {logs}/plaudvault-*.log")
    return 0


def _uninstall_macos() -> int:
    for label in (LABEL_WEB, LABEL_SYNC):
        _launchctl("bootout", f"gui/{os.getuid()}/{label}")
        path = _launch_agents() / f"{label}.plist"
        if path.exists():
            path.unlink()
            print(f"  removed {path}")
    return 0


# ------------------------------------------------------------------------ Linux


def _systemd_dir() -> Path:
    return Path(
        os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")
    ) / "systemd/user"


def _install_linux(hours: list[int]) -> int:
    exe = _executable()
    unit_dir = _systemd_dir()
    unit_dir.mkdir(parents=True, exist_ok=True)

    (unit_dir / "plaudvault-console.service").write_text(f"""[Unit]
Description=plaudvault console
After=network-online.target

[Service]
Type=simple
ExecStart={exe} web
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
""")

    (unit_dir / "plaudvault-sync.service").write_text(f"""[Unit]
Description=plaudvault sync and process
After=network-online.target

[Service]
Type=oneshot
ExecStart={exe} run
Nice=10
""")

    calendar = "\n".join(f"OnCalendar=*-*-* {h:02d}:00:00" for h in hours)
    (unit_dir / "plaudvault-sync.timer").write_text(f"""[Unit]
Description=plaudvault scheduled sync

[Timer]
{calendar}
Persistent=true

[Install]
WantedBy=timers.target
""")

    subprocess.run(["systemctl", "--user", "daemon-reload"], check=False)
    for unit in ("plaudvault-console.service", "plaudvault-sync.timer"):
        subprocess.run(["systemctl", "--user", "enable", "--now", unit], check=False)
        print(f"  enabled {unit}")

    print(f"\n  console: always on, restarted if it exits")
    print(f"  sync:    {', '.join(f'{h:02d}:00' for h in hours)} daily")
    print("  logs:    journalctl --user -u plaudvault-console -f")
    print("\n  For the console to survive logout: sudo loginctl enable-linger $USER")
    return 0


def _uninstall_linux() -> int:
    for unit in ("plaudvault-console.service", "plaudvault-sync.timer"):
        subprocess.run(["systemctl", "--user", "disable", "--now", unit], check=False)
    for name in (
        "plaudvault-console.service",
        "plaudvault-sync.service",
        "plaudvault-sync.timer",
    ):
        path = _systemd_dir() / name
        if path.exists():
            path.unlink()
            print(f"  removed {path}")
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=False)
    return 0


# ------------------------------------------------------------------------ api


def install(hours: list[int] | None = None) -> int:
    hours = sorted(set(hours or DEFAULT_HOURS))
    if sys.platform == "darwin":
        return _install_macos(hours)
    if sys.platform.startswith("linux"):
        return _install_linux(hours)
    print(f"  automatic service installation isn't supported on {sys.platform}.")
    print("  Run `plaudctl web` and schedule `plaudctl run` with your OS scheduler.")
    return 2


def uninstall() -> int:
    if sys.platform == "darwin":
        return _uninstall_macos()
    if sys.platform.startswith("linux"):
        return _uninstall_linux()
    print(f"  nothing to uninstall on {sys.platform}")
    return 2


def status() -> int:
    if sys.platform == "darwin":
        for label in (LABEL_WEB, LABEL_SYNC):
            out = subprocess.run(
                ["launchctl", "print", f"gui/{os.getuid()}/{label}"],
                capture_output=True, text=True,
            )
            state = "not installed"
            if out.returncode == 0:
                for line in out.stdout.splitlines():
                    if "state = " in line:
                        state = line.split("state = ")[1].strip()
                        break
                else:
                    state = "loaded"
            print(f"  {label:28} {state}")
    elif sys.platform.startswith("linux"):
        for unit in ("plaudvault-console.service", "plaudvault-sync.timer"):
            out = subprocess.run(
                ["systemctl", "--user", "is-active", unit], capture_output=True, text=True
            )
            print(f"  {unit:32} {out.stdout.strip() or 'unknown'}")
    else:
        print(f"  unsupported platform {sys.platform}")
    print(f"\n  logs: {_log_dir()}")
    return 0

"""User-scoped macOS launch agent for the local automation server."""

import os
import plistlib
import subprocess
import sys
import tempfile
from pathlib import Path

LABEL = "com.alexandrpoge.linkdln-auto"


def install_service(database_url: str, config: Path, *, port: int = 8765,
                    interval_minutes: int = 360) -> Path:
    if sys.platform != "darwin":
        raise ValueError("background service installation currently supports macOS")
    if not 1 <= port <= 65535 or not 5 <= interval_minutes <= 1440:
        raise ValueError("invalid service port or interval")
    config = config.resolve(strict=True)
    backend = Path(__file__).resolve().parents[2]
    data = backend.parent / "data"
    data.mkdir(parents=True, exist_ok=True)
    destination = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"
    if destination.exists() or destination.is_symlink():
        raise ValueError("service definition already exists; stop it and inspect it before replacing")
    definition = {
        "Label": LABEL, "ProgramArguments": [sys.executable, "-m", "app", "serve", "--port", str(port),
            "--config", str(config), "--interval-minutes", str(interval_minutes)],
        "WorkingDirectory": str(backend), "EnvironmentVariables": {"DATABASE_URL": database_url},
        "RunAtLoad": True, "KeepAlive": True, "ThrottleInterval": 30,
        "StandardOutPath": str(data / "service.log"), "StandardErrorPath": str(data / "service-error.log"),
    }
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".linkdln-auto-", dir=destination.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            plistlib.dump(definition, stream)
        os.replace(temporary, destination)
        result = subprocess.run(["launchctl", "bootstrap", f"gui/{os.getuid()}", str(destination)],
                                capture_output=True, timeout=20)
        if result.returncode:
            destination.unlink()
            raise ValueError("launchctl could not start the service; check macOS permissions")
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return destination

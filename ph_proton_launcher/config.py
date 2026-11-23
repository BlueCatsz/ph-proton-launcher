"""Configuration persistence for Process Hacker Proton Launcher."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path

def get_default_config_path() -> Path:
    """Return the persistent config file path respecting ``XDG_CONFIG_HOME``."""
    xdg_config = os.environ.get("XDG_CONFIG_HOME", "").strip()
    base = Path(xdg_config).expanduser() if xdg_config else (Path.home() / ".config")
    return base / "ph-proton-launcher" / "config.json"


DEFAULT_CONFIG_PATH = get_default_config_path()
DEFAULT_PROCESS_HACKER_EXE = str(
    Path.home() / "ProcessHacker" / "x64" / "ProcessHacker.exe"
)
DEFAULT_EXE_CANDIDATES = (
    Path.home() / "ProcessHacker" / "x64" / "ProcessHacker.exe",
    Path.home() / "SystemInformer" / "amd64" / "SystemInformer.exe",
    Path.home() / "SystemInformer" / "x64" / "SystemInformer.exe",
)


def detect_default_exe() -> str:
    """Return the first existing default Process Hacker / System Informer executable."""
    for candidate in DEFAULT_EXE_CANDIDATES:
        if candidate.is_file():
            return str(candidate)
    return DEFAULT_PROCESS_HACKER_EXE


@dataclass
class AppConfig:
    """Persistent application configuration."""

    process_hacker_exe: str = DEFAULT_PROCESS_HACKER_EXE

    def resolved_exe_path(self) -> Path:
        """Return the configured executable path with ``~`` expanded."""
        return Path(self.process_hacker_exe).expanduser()


def load_config(config_path: Path | None = None) -> AppConfig:
    """Load configuration from disk, falling back to defaults on missing or invalid data."""
    target = config_path if config_path is not None else get_default_config_path()
    try:
        raw = target.read_text(encoding="utf-8")
    except OSError:
        return AppConfig(process_hacker_exe=detect_default_exe())

    if not raw.strip():
        return AppConfig(process_hacker_exe=detect_default_exe())

    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError, ValueError):
        return AppConfig(process_hacker_exe=detect_default_exe())

    if not isinstance(data, dict):
        return AppConfig(process_hacker_exe=detect_default_exe())

    exe_path = data.get("process_hacker_exe")
    if isinstance(exe_path, str) and exe_path.strip():
        return AppConfig(process_hacker_exe=exe_path.strip())

    return AppConfig(process_hacker_exe=detect_default_exe())


def save_config(config: AppConfig, config_path: Path | None = None) -> Path:
    """Atomically persist configuration to disk and return the written file path."""
    target = config_path if config_path is not None else get_default_config_path()
    target.parent.mkdir(parents=True, exist_ok=True)

    payload = json.dumps(asdict(config), indent=2) + "\n"
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{target.name}.",
        suffix=".tmp",
        dir=str(target.parent),
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, target)
    except Exception:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise

    return target

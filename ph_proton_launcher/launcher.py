"""Command construction, validation, desktop notifications, and auto-launch."""

from __future__ import annotations

import shutil
import subprocess
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from . import APP_NAME
from .config import AppConfig
from .steam import SteamGame

ErrorPresenter = Callable[[str, str, str], None]

FLATPAK_PROTONTRICKS_APP_ID = "com.github.Matoking.protontricks"
AUTO_LONG_RUN_THRESHOLD_SECONDS = 30.0
_PROTONTRICKS_ERROR_MARKERS = ("Traceback", "protontricks (ERROR)")


def resolve_protontricks_command() -> list[str] | None:
    """Return the command prefix for ``protontricks-launch`` (native or Flatpak)."""
    native_bin = shutil.which("protontricks-launch")
    if native_bin is not None:
        return ["protontricks-launch"]

    flatpak_bin = shutil.which("flatpak")
    if flatpak_bin is not None:
        try:
            probe = subprocess.run(
                [flatpak_bin, "info", FLATPAK_PROTONTRICKS_APP_ID],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5,
                check=False,
            )
            if probe.returncode == 0:
                return [
                    "flatpak",
                    "run",
                    "--command=protontricks-launch",
                    FLATPAK_PROTONTRICKS_APP_ID,
                ]
        except (OSError, subprocess.SubprocessError):
            pass

    return None


def build_launch_command(
    appid: str,
    exe_path: str | Path,
    *,
    verbose: bool = False,
    launcher_prefix: Sequence[str] | None = None,
) -> list[str]:
    """Build the ``protontricks-launch`` command list."""
    clean_appid = str(appid).strip()
    if not clean_appid.isdigit():
        raise ValueError(f"AppID must be numeric, got {appid!r}")

    prefix = list(launcher_prefix) if launcher_prefix else ["protontricks-launch"]
    if verbose:
        prefix.append("-v")

    resolved_exe = str(Path(exe_path).expanduser())
    return [
        *prefix,
        "--no-bwrap",
        "--appid",
        clean_appid,
        resolved_exe,
    ]


def validate_launch_inputs(
    appid: str,
    exe_path: str | Path,
    *,
    verbose: bool = False,
) -> tuple[str | None, list[str] | None]:
    """Validate AppID, ``protontricks-launch`` availability, and executable file path."""
    clean_appid = str(appid).strip()
    if not clean_appid or not clean_appid.isdigit():
        return ("Select a running Steam game or enter a valid numeric AppID.", None)

    exe_str = str(exe_path).strip()
    if not exe_str:
        return ("Specify the path to ProcessHacker.exe.", None)

    resolved_exe = Path(exe_str).expanduser()
    if not resolved_exe.is_file():
        return (
            f"Process Hacker executable was not found:\n{resolved_exe}",
            None,
        )

    prefix = resolve_protontricks_command()
    if prefix is None:
        return (
            "`protontricks-launch` was not found in PATH and Flatpak "
            f"`{FLATPAK_PROTONTRICKS_APP_ID}` is not installed.",
            None,
        )

    return (
        None,
        build_launch_command(
            clean_appid,
            resolved_exe,
            verbose=verbose,
            launcher_prefix=prefix,
        ),
    )


def is_expected_teardown_exit(
    returncode: int,
    elapsed_seconds: float,
    output: str = "",
    *,
    threshold_seconds: float = AUTO_LONG_RUN_THRESHOLD_SECONDS,
) -> bool:
    """Return True when a non-zero exit looks like a normal long-running game shutdown."""
    if returncode == 0 or elapsed_seconds < threshold_seconds:
        return False
    return not any(marker in output for marker in _PROTONTRICKS_ERROR_MARKERS)


def send_notification(title: str, body: str, urgency: str = "normal") -> bool:
    """Send a best-effort desktop notification via ``notify-send``."""
    notify_bin = shutil.which("notify-send")
    if notify_bin is None:
        return False

    try:
        subprocess.run(
            [
                notify_bin,
                f"--app-name={APP_NAME}",
                "--icon=utilities-system-monitor",
                f"--urgency={urgency}",
                title,
                body,
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
            check=False,
        )
        return True
    except (OSError, subprocess.SubprocessError):
        return False


def _combine_process_output(stdout: str | None, stderr: str | None) -> str:
    """Combine captured stdout and stderr blocks into readable error details."""
    parts: list[str] = []
    if stderr and stderr.strip():
        parts.append(stderr.strip())
    if stdout and stdout.strip():
        parts.append(stdout.strip())
    return "\n\n".join(parts)


def run_auto_launch(
    game: SteamGame,
    config: AppConfig,
    *,
    runner: Callable[..., Any] = subprocess.run,
    notifier: Callable[[str, str], Any] = send_notification,
    error_presenter: ErrorPresenter | None = None,
    clock: Callable[[], float] = time.monotonic,
    long_run_threshold: float = AUTO_LONG_RUN_THRESHOLD_SECONDS,
) -> int:
    """Silently launch Process Hacker for ``game`` in ``--auto`` mode."""
    error_msg, cmd = validate_launch_inputs(game.appid, config.process_hacker_exe)
    if error_msg or cmd is None:
        if error_presenter is not None:
            error_presenter(
                "Process Hacker Launch Failed",
                error_msg or "Invalid launch configuration.",
                "",
            )
        return 1

    notifier(
        "Launching Process Hacker",
        f"Attaching to {game.display_label}",
    )

    started_at = clock()
    try:
        result = runner(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except OSError as exc:
        if error_presenter is not None:
            error_presenter(
                "Process Hacker Launch Failed",
                f"Failed to execute protontricks-launch for {game.display_label}.",
                str(exc),
            )
        return 1

    elapsed = clock() - started_at
    if result.returncode == 0:
        return 0

    details = _combine_process_output(result.stdout, result.stderr)
    if is_expected_teardown_exit(
        result.returncode,
        elapsed,
        details,
        threshold_seconds=long_run_threshold,
    ):
        return 0

    if error_presenter is not None:
        error_presenter(
            "Process Hacker Launch Failed",
            (
                f"protontricks-launch exited with code {result.returncode} "
                f"for {game.display_label}."
            ),
            details,
        )
    return int(result.returncode) or 1

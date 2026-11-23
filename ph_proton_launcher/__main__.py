"""CLI entrypoint and mode routing for Process Hacker Proton Launcher."""

from __future__ import annotations

import argparse
import signal
import sys
from collections.abc import Sequence

from . import __version__
from .config import AppConfig, load_config, save_config
from .downloader import (
    TOOL_PROCESS_HACKER,
    TOOL_SYSTEM_INFORMER,
    download_and_install_tool,
)
from .launcher import run_auto_launch
from .steam import SteamGame, detect_running_games


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """Parse CLI flags."""
    parser = argparse.ArgumentParser(
        prog="ph-proton-launcher",
        description=(
            "Launch Process Hacker or System Informer inside a running Steam Proton "
            "prefix via protontricks-launch."
        ),
    )
    parser.add_argument(
        "--auto",
        action="store_true",
        help=(
            "Silently launch if exactly 1 running Steam game is detected; "
            "otherwise open the GUI window."
        ),
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Open the GUI window with the Verbose Log drawer expanded.",
    )
    parser.add_argument(
        "--download",
        choices=(TOOL_PROCESS_HACKER, TOOL_SYSTEM_INFORMER),
        metavar="TOOL",
        help=(
            "Download and verify a portable copy of 'process-hacker' (2.39) or "
            "'system-informer' (latest stable), save its path to config, and exit."
        ),
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    return parser.parse_args(list(argv) if argv is not None else None)


def _lazy_show_error_dialog(title: str, message: str, details: str = "") -> None:
    """Import PyQt6 UI lazily only if an error dialog must be displayed."""
    from .ui import show_error_dialog

    show_error_dialog(title, message, details)


def _run_download_cli(tool_key: str, config: AppConfig) -> int:
    """Download a portable tool in CLI mode and save its path to config."""
    try:
        result = download_and_install_tool(tool_key)
    except Exception as exc:
        print(f"Download failed: {exc}", file=sys.stderr)
        return 1

    config.process_hacker_exe = str(result.exe_path)
    save_path = save_config(config)
    print(f"Installed {result.display_name} ({result.version}) to {result.exe_path}")
    print(f"Saved config to {save_path}")
    return 0


def _run_gui(
    config: AppConfig,
    games: list[SteamGame],
    *,
    start_verbose: bool,
) -> int:
    """Initialize Qt and start the interactive launcher window."""
    from .ui import MainWindow, create_qapplication

    signal.signal(signal.SIGINT, signal.SIG_DFL)
    app = create_qapplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    window = MainWindow(
        config=config,
        start_verbose=start_verbose,
        initial_games=games,
    )
    window.show()
    return app.exec()


def main(argv: Sequence[str] | None = None) -> int:
    """Application entrypoint."""
    args = parse_args(argv)
    config = load_config()

    if args.download:
        return _run_download_cli(args.download, config)

    games = detect_running_games()

    if args.auto and len(games) == 1:
        return run_auto_launch(
            games[0],
            config,
            error_presenter=_lazy_show_error_dialog,
        )

    return _run_gui(config, games, start_verbose=args.verbose)


if __name__ == "__main__":
    raise SystemExit(main())

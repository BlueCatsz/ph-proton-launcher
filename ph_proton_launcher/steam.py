"""Steam library discovery, ACF/VDF parsing, and running Proton game detection."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DEFAULT_STEAM_VDF_PATHS = (
    Path.home() / ".local" / "share" / "Steam" / "steamapps" / "libraryfolders.vdf",
    Path.home() / ".steam" / "steam" / "steamapps" / "libraryfolders.vdf",
    Path.home()
    / ".var"
    / "app"
    / "com.valvesoftware.Steam"
    / ".local"
    / "share"
    / "Steam"
    / "steamapps"
    / "libraryfolders.vdf",
    Path.home()
    / "snap"
    / "steam"
    / "common"
    / ".local"
    / "share"
    / "Steam"
    / "steamapps"
    / "libraryfolders.vdf",
)

_IGNORED_SCAN_EXECUTABLES = frozenset({"pgrep", "grep", "rg", "ag"})
_APPID_TOKEN_RE = re.compile(r"AppId=(\d+)")
_SHELL_STEAMLAUNCH_RE = re.compile(r"\bSteamLaunch\s+AppId=(\d+)\b")


@dataclass(frozen=True)
class SteamGame:
    """Represents a running Steam game identified by AppID and display name."""

    appid: str
    name: str

    @property
    def display_label(self) -> str:
        """Return the UI display string ``Game Name (AppID)``."""
        return f"{self.name} ({self.appid})"


def _tokenize_vdf(text: str) -> list[str]:
    """Tokenize Valve KeyValues (VDF/ACF) text into braces and strings."""
    tokens: list[str] = []
    length = len(text)
    idx = 0

    while idx < length:
        ch = text[idx]
        if ch.isspace():
            idx += 1
            continue

        if ch == "/" and idx + 1 < length and text[idx + 1] == "/":
            newline = text.find("\n", idx + 2)
            idx = length if newline == -1 else newline + 1
            continue

        if ch in ("{", "}"):
            tokens.append(ch)
            idx += 1
            continue

        if ch == '"':
            idx += 1
            chars: list[str] = []
            while idx < length:
                cur = text[idx]
                if cur == "\\" and idx + 1 < length:
                    nxt = text[idx + 1]
                    if nxt == "n":
                        chars.append("\n")
                    elif nxt == "t":
                        chars.append("\t")
                    elif nxt == "r":
                        chars.append("\r")
                    else:
                        chars.append(nxt)
                    idx += 2
                    continue
                if cur == '"':
                    idx += 1
                    break
                chars.append(cur)
                idx += 1
            tokens.append("".join(chars))
            continue

        start = idx
        while idx < length and not text[idx].isspace() and text[idx] not in ('{', '}', '"'):
            if text[idx] == "/" and idx + 1 < length and text[idx + 1] == "/":
                break
            idx += 1
        tokens.append(text[start:idx])

    return tokens


def _parse_vdf_block(tokens: list[str], index: int) -> tuple[dict[str, Any], int]:
    """Parse a VDF key/value block starting at ``index`` until ``}`` or EOF."""
    result: dict[str, Any] = {}
    total = len(tokens)

    while index < total:
        token = tokens[index]
        if token == "}":
            return result, index + 1
        if token == "{":
            index += 1
            continue

        key = token
        index += 1
        if index >= total:
            break

        next_token = tokens[index]
        if next_token == "{":
            nested, index = _parse_vdf_block(tokens, index + 1)
            existing = result.get(key)
            if isinstance(existing, dict):
                existing.update(nested)
            else:
                result[key] = nested
        elif next_token == "}":
            result[key] = ""
            return result, index + 1
        else:
            result[key] = next_token
            index += 1

    return result, index


def parse_vdf(text: str) -> dict[str, Any]:
    """Parse Valve KeyValues (VDF/ACF) text into nested Python dictionaries."""
    tokens = _tokenize_vdf(text)
    parsed, _ = _parse_vdf_block(tokens, 0)
    return parsed


def _ci_get(mapping: dict[str, Any], target_key: str, default: Any = None) -> Any:
    """Perform a case-insensitive dictionary lookup."""
    if target_key in mapping:
        return mapping[target_key]
    lowered = target_key.casefold()
    for key, value in mapping.items():
        if isinstance(key, str) and key.casefold() == lowered:
            return value
    return default


def _normalize_path_no_fs(path_str: str | Path) -> Path:
    """Normalize and expand ``~`` in a path without touching the filesystem."""
    expanded = Path(path_str).expanduser()
    return Path(os.path.normpath(str(expanded)))


def _resolve_vdf_paths(vdf_path: Path | None) -> list[Path]:
    """Return active ``libraryfolders.vdf`` paths to inspect in priority order."""
    if vdf_path is not None:
        return [vdf_path]
    existing = [candidate for candidate in DEFAULT_STEAM_VDF_PATHS if candidate.is_file()]
    return existing if existing else [DEFAULT_STEAM_VDF_PATHS[0]]


def get_steam_libraries(vdf_path: Path | None = None) -> list[tuple[Path, set[str]]]:
    """Return ordered, deduplicated ``(library_path, known_appids)`` pairs."""
    libraries: dict[Path, set[str]] = {}

    def add_library(raw_path: str | Path, appids: set[str] | None = None) -> None:
        norm_path = _normalize_path_no_fs(raw_path)
        bucket = libraries.setdefault(norm_path, set())
        if appids:
            bucket.update(appids)

    for target_vdf in _resolve_vdf_paths(vdf_path):
        try:
            vdf_text = target_vdf.read_text(encoding="utf-8", errors="replace")
        except OSError:
            vdf_text = ""

        if vdf_text:
            parsed = parse_vdf(vdf_text)
            folders = _ci_get(parsed, "libraryfolders")
            if not isinstance(folders, dict):
                folders = parsed

            for key, entry in folders.items():
                if isinstance(entry, dict):
                    raw_path = _ci_get(entry, "path")
                    if not isinstance(raw_path, str) or not raw_path.strip():
                        continue
                    apps_obj = _ci_get(entry, "apps")
                    appids: set[str] = set()
                    if isinstance(apps_obj, dict):
                        appids = {
                            str(app_key).strip()
                            for app_key in apps_obj
                            if str(app_key).strip().isdigit()
                        }
                    add_library(raw_path.strip(), appids)
                elif isinstance(entry, str) and str(key).strip().isdigit() and entry.strip():
                    add_library(entry.strip())

        # Always include the Steam root owning libraryfolders.vdf as a fallback library.
        vdf_root = target_vdf.parent.parent
        if str(vdf_root) not in ("", "."):
            add_library(vdf_root)

    return list(libraries.items())


def resolve_game_name(
    appid: str,
    vdf_path: Path | None = None,
    libraries: list[tuple[Path, set[str]]] | None = None,
) -> str:
    """Resolve an AppID to its human-readable game name from ``appmanifest_<appid>.acf``."""
    clean_appid = str(appid).strip()
    if not clean_appid.isdigit():
        return "Unknown Game"

    libs = libraries if libraries is not None else get_steam_libraries(vdf_path)
    prioritized = sorted(
        libs,
        key=lambda item: 0 if clean_appid in item[1] else 1,
    )

    for lib_path, _ in prioritized:
        for steamapps_dir in ("steamapps", "SteamApps"):
            manifest_path = lib_path / steamapps_dir / f"appmanifest_{clean_appid}.acf"
            try:
                content = manifest_path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue

            parsed = parse_vdf(content)
            app_state = _ci_get(parsed, "AppState")
            if not isinstance(app_state, dict):
                app_state = parsed
            name = _ci_get(app_state, "name")
            if isinstance(name, str) and name.strip():
                return name.strip()

    return "Unknown Game"


def extract_appid_from_cmdline(argv: list[str]) -> str | None:
    """Extract the Steam AppID from a process argv list matching ``SteamLaunch AppId=<ID>``."""
    if not argv:
        return None

    exe_name = Path(argv[0]).name.casefold()
    if exe_name in _IGNORED_SCAN_EXECUTABLES:
        return None

    for idx in range(len(argv) - 1):
        if argv[idx] == "SteamLaunch":
            match = _APPID_TOKEN_RE.fullmatch(argv[idx + 1])
            if match:
                return match.group(1)

    joined = " ".join(argv)
    match = _SHELL_STEAMLAUNCH_RE.search(joined)
    if match:
        return match.group(1)

    return None


def detect_running_games(
    proc_root: Path = Path("/proc"),
    vdf_path: Path | None = None,
    self_pid: int | None = None,
) -> list[SteamGame]:
    """Scan ``proc_root`` for running ``SteamLaunch AppId=<ID>`` games."""
    current_pid = os.getpid() if self_pid is None else self_pid
    seen_appids: set[str] = set()

    try:
        entries = list(proc_root.iterdir())
    except OSError:
        return []

    for entry in entries:
        if not entry.name.isdigit():
            continue
        if int(entry.name) == current_pid:
            continue

        try:
            raw_cmdline = (entry / "cmdline").read_bytes()
        except OSError:
            continue

        if not raw_cmdline or b"SteamLaunch" not in raw_cmdline:
            continue

        argv = [
            part.decode("utf-8", errors="replace")
            for part in raw_cmdline.split(b"\x00")
            if part
        ]
        appid = extract_appid_from_cmdline(argv)
        if appid:
            seen_appids.add(appid)

    if not seen_appids:
        return []

    libraries = get_steam_libraries(vdf_path)
    games = [
        SteamGame(
            appid=appid,
            name=resolve_game_name(appid, vdf_path=vdf_path, libraries=libraries),
        )
        for appid in seen_appids
    ]
    games.sort(key=lambda game: (game.name.casefold(), int(game.appid)))
    return games

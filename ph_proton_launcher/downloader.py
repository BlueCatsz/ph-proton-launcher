"""Secure downloader and portable installer for Process Hacker and System Informer."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import urllib.parse
import urllib.request
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

TOOL_PROCESS_HACKER = "process-hacker"
TOOL_SYSTEM_INFORMER = "system-informer"

PROCESS_HACKER_239_URL = (
    "https://sourceforge.net/projects/processhacker/files/processhacker2/"
    "processhacker-2.39-bin.zip/download"
)
PROCESS_HACKER_239_SHA256 = (
    "2afb5303e191dde688c5626c3ee545e32e52f09da3b35b20f5e0d29a418432f5"
)

SYSTEM_INFORMER_RELEASE_API = (
    "https://api.github.com/repos/winsiderss/systeminformer/releases/latest"
)
SYSTEM_INFORMER_FALLBACK_API = (
    "https://api.github.com/repos/winsiderss/si-builds/releases/latest"
)

MAX_DOWNLOAD_BYTES = 100 * 1024 * 1024  # 100 MiB
MAX_EXTRACTED_BYTES = 250 * 1024 * 1024  # 250 MiB
_CHUNK_SIZE = 65536
_USER_AGENT = "ph-proton-launcher/1.0"
_ALLOWED_HOSTS = frozenset(
    {
        "github.com",
        "api.github.com",
        "objects.githubusercontent.com",
        "release-assets.githubusercontent.com",
        "sourceforge.net",
        "downloads.sourceforge.net",
    }
)


class _TrustedRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Reject HTTP redirects to insecure schemes or untrusted hosts before connecting."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        resolved = urllib.parse.urljoin(req.full_url, newurl)
        _validate_download_url(resolved)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_default_opener = urllib.request.build_opener(_TrustedRedirectHandler)


class DownloadCancelledError(RuntimeError):
    """Raised when a user cancels an active download."""


@dataclass(frozen=True)
class ReleaseAsset:
    """Resolved downloadable portable archive metadata."""

    tool_key: str
    display_name: str
    version: str
    url: str
    exe_name: str
    expected_sha256: str | None = None
    user_agent: str = _USER_AGENT


@dataclass(frozen=True)
class DownloadResult:
    """Installed portable tool result."""

    tool_key: str
    display_name: str
    version: str
    exe_path: Path
    install_dir: Path
    sha256: str


def get_tools_dir() -> Path:
    """Return the managed directory for auto-downloaded tools."""
    xdg_data = os.environ.get("XDG_DATA_HOME", "").strip()
    base = Path(xdg_data).expanduser() if xdg_data else (Path.home() / ".local" / "share")
    return base / "ph-proton-launcher" / "tools"


def _validate_download_url(url: str) -> None:
    """Ensure download and API URLs use HTTPS on trusted release hosts."""
    parsed = urllib.parse.urlsplit(url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme.lower() != "https" or not host:
        raise ValueError(f"Untrusted download URL: {url}")
    if host in _ALLOWED_HOSTS or host.endswith(".dl.sourceforge.net"):
        return
    raise ValueError(f"Untrusted download host: {host}")


def _extract_sha256_digest(asset: dict[str, Any]) -> str | None:
    """Extract lowercase SHA-256 hex digest from a GitHub release asset object."""
    digest = asset.get("digest")
    if isinstance(digest, str) and digest.lower().startswith("sha256:"):
        hex_value = digest.split(":", 1)[1].strip().lower()
        if len(hex_value) == 64 and all(ch in "0123456789abcdef" for ch in hex_value):
            return hex_value
    return None


def select_system_informer_asset(release_json: dict[str, Any]) -> ReleaseAsset:
    """Select the 64-bit portable ZIP asset from a GitHub release payload."""
    assets = release_json.get("assets")
    if not isinstance(assets, list):
        raise ValueError("GitHub release payload does not contain an assets list.")

    candidates: list[tuple[int, dict[str, Any]]] = []
    for asset in assets:
        if not isinstance(asset, dict):
            continue
        name = str(asset.get("name") or "")
        lower = name.casefold()
        if not lower.endswith(".zip") or "bin" not in lower:
            continue
        if any(skip in lower for skip in ("-pdb", "arm64", "win32", "x86", "setup", "src")):
            continue
        priority = 0 if ("win64" in lower or "amd64" in lower) else 1
        candidates.append((priority, asset))

    if not candidates:
        raise ValueError("No portable 64-bit ZIP asset found in System Informer release.")

    candidates.sort(key=lambda item: item[0])
    chosen = candidates[0][1]
    url = str(chosen.get("browser_download_url") or "")
    _validate_download_url(url)

    return ReleaseAsset(
        tool_key=TOOL_SYSTEM_INFORMER,
        display_name="System Informer",
        version=str(release_json.get("tag_name") or "latest"),
        url=url,
        exe_name="SystemInformer.exe",
        expected_sha256=_extract_sha256_digest(chosen),
        user_agent=_USER_AGENT,
    )


def _fetch_json(url: str, *, opener: Callable[..., Any]) -> dict[str, Any]:
    """Fetch and parse a JSON payload from an allowlisted HTTPS endpoint."""
    _validate_download_url(url)
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": _USER_AGENT,
            "Accept": "application/vnd.github+json",
        },
    )
    with opener(req, timeout=20) as resp:
        final_url = resp.geturl() if hasattr(resp, "geturl") else url
        if final_url:
            _validate_download_url(final_url)
        raw = resp.read(2 * 1024 * 1024)
    data = json.loads(raw.decode("utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"Expected JSON object from {url}")
    return data


def resolve_release_asset(
    tool_key: str,
    *,
    opener: Callable[..., Any] = _default_opener.open,
) -> ReleaseAsset:
    """Resolve download URL, version, and checksum metadata for ``tool_key``."""
    if tool_key == TOOL_PROCESS_HACKER:
        return ReleaseAsset(
            tool_key=TOOL_PROCESS_HACKER,
            display_name="Process Hacker",
            version="2.39",
            url=PROCESS_HACKER_239_URL,
            exe_name="ProcessHacker.exe",
            expected_sha256=PROCESS_HACKER_239_SHA256,
            user_agent="curl/8.0",
        )

    if tool_key == TOOL_SYSTEM_INFORMER:
        try:
            primary_json = _fetch_json(SYSTEM_INFORMER_RELEASE_API, opener=opener)
            return select_system_informer_asset(primary_json)
        except Exception as primary_exc:
            try:
                fallback_json = _fetch_json(SYSTEM_INFORMER_FALLBACK_API, opener=opener)
                return select_system_informer_asset(fallback_json)
            except Exception as fallback_exc:
                raise RuntimeError(
                    f"Failed to resolve System Informer release ({primary_exc}; {fallback_exc})"
                ) from fallback_exc

    raise ValueError(f"Unsupported tool key: {tool_key!r}")


def _safe_extract_zip(
    zip_path: Path,
    dest_dir: Path,
    *,
    max_extracted_bytes: int = MAX_EXTRACTED_BYTES,
) -> None:
    """Safely extract regular files and directories from ``zip_path`` into ``dest_dir``."""
    dest_root = dest_dir.resolve()
    extracted_total = 0
    with zipfile.ZipFile(zip_path, "r") as zf:
        for info in zf.infolist():
            member_name = info.filename.replace("\\", "/")
            if not member_name:
                continue

            posix_path = PurePosixPath(member_name)
            if (
                posix_path.is_absolute()
                or member_name.startswith("/")
                or ".." in posix_path.parts
                or (posix_path.parts and ":" in posix_path.parts[0])
            ):
                raise ValueError(f"Unsafe path in archive: {info.filename!r}")

            mode = (info.external_attr >> 16) & 0o170000
            if mode == 0o120000:
                raise ValueError(f"Refusing to extract symlink from archive: {info.filename!r}")

            target = (dest_root / posix_path).resolve()
            if target != dest_root and dest_root not in target.parents:
                raise ValueError(f"Archive member escapes target directory: {info.filename!r}")

            if info.is_dir() or member_name.endswith("/") or target == dest_root:
                target.mkdir(parents=True, exist_ok=True)
                continue

            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info, "r") as src, target.open("wb") as dst:
                while True:
                    chunk = src.read(_CHUNK_SIZE)
                    if not chunk:
                        break
                    extracted_total += len(chunk)
                    if extracted_total > max_extracted_bytes:
                        raise ValueError(
                            f"Extracted archive contents exceed {max_extracted_bytes} byte limit."
                        )
                    dst.write(chunk)


def find_executable_in_dir(root_dir: Path, exe_name: str) -> Path:
    """Locate the preferred 64-bit ``exe_name`` inside an extracted archive."""
    target_lower = exe_name.casefold()
    matches = [
        path
        for path in root_dir.rglob("*")
        if path.is_file() and path.name.casefold() == target_lower
    ]
    if not matches:
        raise FileNotFoundError(f"Could not find {exe_name} inside extracted archive.")

    def _rank(path: Path) -> tuple[int, str]:
        rel_parts = [part.casefold() for part in path.relative_to(root_dir).parts[:-1]]
        if "amd64" in rel_parts:
            tier = 0
        elif "x64" in rel_parts:
            tier = 1
        elif not rel_parts:
            tier = 2
        elif not any(arch in rel_parts for arch in ("x86", "win32", "i386", "arm64")):
            tier = 3
        else:
            tier = 4
        return (tier, str(path))

    return min(matches, key=_rank)


def download_and_install_tool(
    tool_key: str,
    *,
    tools_dir: Path | None = None,
    progress_callback: Callable[[int, int], None] | None = None,
    cancel_check: Callable[[], bool] | None = None,
    opener: Callable[..., Any] = _default_opener.open,
) -> DownloadResult:
    """Download, SHA-256 verify, safely extract, and atomically install a portable tool."""
    asset = resolve_release_asset(tool_key, opener=opener)
    _validate_download_url(asset.url)

    base_tools_dir = tools_dir if tools_dir is not None else get_tools_dir()
    base_tools_dir.mkdir(parents=True, exist_ok=True)

    fd, tmp_zip_name = tempfile.mkstemp(
        prefix=f".{tool_key}-",
        suffix=".zip",
        dir=str(base_tools_dir),
    )
    os.close(fd)
    tmp_zip = Path(tmp_zip_name)
    staging_dir = Path(
        tempfile.mkdtemp(prefix=f".{tool_key}-stage-", dir=str(base_tools_dir))
    )
    target_dir = base_tools_dir / tool_key
    backup_dir = base_tools_dir / f".{tool_key}-backup-{os.getpid()}"

    try:
        req = urllib.request.Request(
            asset.url,
            headers={"User-Agent": asset.user_agent, "Accept": "*/*"},
        )
        hasher = hashlib.sha256()
        downloaded = 0

        with opener(req, timeout=30) as resp:
            final_url = resp.geturl() if hasattr(resp, "geturl") else asset.url
            if final_url:
                _validate_download_url(final_url)

            raw_length = resp.headers.get("Content-Length") if hasattr(resp, "headers") else None
            raw_str = str(raw_length).strip() if raw_length is not None else ""
            total_bytes = int(raw_str) if raw_str.isdigit() else -1
            if total_bytes > MAX_DOWNLOAD_BYTES:
                raise ValueError(
                    f"Remote archive size ({total_bytes} bytes) exceeds {MAX_DOWNLOAD_BYTES} byte limit."
                )

            with tmp_zip.open("wb") as out_file:
                while True:
                    if cancel_check is not None and cancel_check():
                        raise DownloadCancelledError("Download cancelled by user.")
                    chunk = resp.read(_CHUNK_SIZE)
                    if not chunk:
                        break
                    downloaded += len(chunk)
                    if downloaded > MAX_DOWNLOAD_BYTES:
                        raise ValueError(
                            f"Download exceeded maximum allowed size ({MAX_DOWNLOAD_BYTES} bytes)."
                        )
                    hasher.update(chunk)
                    out_file.write(chunk)
                    if progress_callback is not None:
                        progress_callback(downloaded, total_bytes)

        actual_sha256 = hasher.hexdigest().lower()
        if asset.expected_sha256 and actual_sha256 != asset.expected_sha256.lower():
            raise ValueError(
                f"SHA-256 verification failed for {asset.display_name}: "
                f"expected {asset.expected_sha256}, got {actual_sha256}."
            )

        _safe_extract_zip(tmp_zip, staging_dir)
        staged_exe = find_executable_in_dir(staging_dir, asset.exe_name)
        rel_exe = staged_exe.relative_to(staging_dir)

        portable_marker = staged_exe.parent / f"{staged_exe.name}.settings.xml"
        existing_settings = target_dir / rel_exe.parent / portable_marker.name
        if (
            existing_settings.is_file()
            and not existing_settings.is_symlink()
            and existing_settings.stat().st_size > 0
        ):
            shutil.copy2(existing_settings, portable_marker)
        else:
            portable_marker.touch(exist_ok=True)

        shutil.rmtree(backup_dir, ignore_errors=True)
        moved_existing = False
        if target_dir.exists():
            target_dir.replace(backup_dir)
            moved_existing = True
        try:
            staging_dir.replace(target_dir)
        except Exception:
            if moved_existing and backup_dir.exists() and not target_dir.exists():
                backup_dir.replace(target_dir)
            raise
        if moved_existing:
            shutil.rmtree(backup_dir, ignore_errors=True)

        return DownloadResult(
            tool_key=asset.tool_key,
            display_name=asset.display_name,
            version=asset.version,
            exe_path=target_dir / rel_exe,
            install_dir=target_dir,
            sha256=actual_sha256,
        )
    finally:
        tmp_zip.unlink(missing_ok=True)
        shutil.rmtree(staging_dir, ignore_errors=True)

"""Unit and headless PyQt6 tests for Process Hacker Proton Launcher."""

from __future__ import annotations

import hashlib
import io
import json
import os
import signal
import subprocess
import sys
import tempfile
import unittest
import urllib.request
import zipfile
from pathlib import Path
from unittest import mock

from PyQt6.QtCore import QEventLoop, QTimer
from PyQt6.QtGui import QCloseEvent
from PyQt6.QtWidgets import QApplication

from ph_proton_launcher import __main__ as cli_main
from ph_proton_launcher.config import (
    DEFAULT_PROCESS_HACKER_EXE,
    AppConfig,
    detect_default_exe,
    get_default_config_path,
    load_config,
    save_config,
)
from ph_proton_launcher.downloader import (
    PROCESS_HACKER_239_SHA256,
    TOOL_PROCESS_HACKER,
    TOOL_SYSTEM_INFORMER,
    DownloadResult,
    _safe_extract_zip,
    _TrustedRedirectHandler,
    _validate_download_url,
    download_and_install_tool,
    find_executable_in_dir,
    select_system_informer_asset,
)
from ph_proton_launcher.launcher import (
    FLATPAK_PROTONTRICKS_APP_ID,
    build_launch_command,
    list_descendant_pids,
    resolve_protontricks_command,
    run_auto_launch,
    signal_process_tree,
    tool_display_name,
    validate_launch_inputs,
)
from ph_proton_launcher.steam import (
    DEFAULT_STEAM_VDF_PATHS,
    SteamGame,
    detect_running_games,
    extract_appid_from_cmdline,
    get_steam_libraries,
    parse_vdf,
    resolve_game_name,
)
from ph_proton_launcher.ui import MainWindow, create_qapplication


class _FakeHttpResponse:
    """In-memory urllib response stub for downloader tests."""

    def __init__(
        self,
        data: bytes,
        url: str,
        headers: dict[str, str] | None = None,
    ) -> None:
        self._stream = io.BytesIO(data)
        self._url = url
        self.headers = headers or {"Content-Length": str(len(data))}

    def read(self, amt: int = -1) -> bytes:
        return self._stream.read(amt)

    def geturl(self) -> str:
        return self._url

    def __enter__(self) -> _FakeHttpResponse:
        return self

    def __exit__(self, *_args: object) -> None:
        return None


def _make_zip_bytes(entries: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, content in entries.items():
            zf.writestr(name, content)
    return buf.getvalue()


class VdfAndSteamTests(unittest.TestCase):
    def test_default_steam_vdf_paths_includes_flatpak_and_snap(self) -> None:
        joined = "\n".join(str(path) for path in DEFAULT_STEAM_VDF_PATHS)
        self.assertIn("com.valvesoftware.Steam", joined)
        self.assertIn("/snap/steam/", joined)

    def test_parse_vdf_handles_comments_escapes_and_nested_blocks(self) -> None:
        raw = """
        // Steam library folders
        "libraryfolders"
        {
            "0"
            {
                "path"      "/home/test/Steam"
                "apps"
                {
                    "230410"    "12345"
                }
            }
            "1"     "/mnt/legacy/SteamLibrary"
        }
        """
        parsed = parse_vdf(raw)
        self.assertEqual(parsed["libraryfolders"]["0"]["path"], "/home/test/Steam")
        self.assertIn("230410", parsed["libraryfolders"]["0"]["apps"])
        self.assertEqual(parsed["libraryfolders"]["1"], "/mnt/legacy/SteamLibrary")

    def test_multi_library_resolution_and_missing_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            steam_root = root / "Steam"
            lib2 = root / "MissingOrUnmounted"
            lib3 = root / "ExtraLib"

            (steam_root / "steamapps").mkdir(parents=True)
            (lib3 / "steamapps").mkdir(parents=True)

            vdf = steam_root / "steamapps" / "libraryfolders.vdf"
            vdf.write_text(
                f'''"libraryfolders"
{{
    "0"
    {{
        "path" "{steam_root}"
        "apps" {{ "100" "1" }}
    }}
    "1"
    {{
        "path" "{lib2}"
        "apps" {{ "200" "1" }}
    }}
    "2" "{lib3}"
}}
''',
                encoding="utf-8",
            )

            (steam_root / "steamapps" / "appmanifest_100.acf").write_text(
                '"AppState"\n{\n\t"appid"\t\t"100"\n\t"name"\t\t"Portal 2"\n}\n',
                encoding="utf-8",
            )
            (lib3 / "steamapps" / "appmanifest_300.acf").write_text(
                '"AppState"\n{\n\t"appid"\t\t"300"\n\t"name"\t\t"Warframe \\"Test\\""\n}\n',
                encoding="utf-8",
            )

            libs = get_steam_libraries(vdf)
            self.assertEqual(len(libs), 3)
            self.assertEqual(resolve_game_name("100", vdf_path=vdf), "Portal 2")
            self.assertEqual(resolve_game_name("300", vdf_path=vdf), 'Warframe "Test"')
            self.assertEqual(resolve_game_name("200", vdf_path=vdf), "Unknown Game")

    def test_get_steam_libraries_aggregates_multiple_existing_vdf_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            native_steam = root / "native_steam"
            flatpak_steam = root / "flatpak_steam"
            extra_lib = root / "extra_lib"
            (native_steam / "steamapps").mkdir(parents=True)
            (flatpak_steam / "steamapps").mkdir(parents=True)
            (extra_lib / "steamapps").mkdir(parents=True)

            vdf1 = native_steam / "steamapps" / "libraryfolders.vdf"
            vdf1.write_text(
                f'"libraryfolders" {{ "0" {{ "path" "{native_steam}" "apps" {{ "100" "1" }} }} }}',
                encoding="utf-8",
            )
            vdf2 = flatpak_steam / "steamapps" / "libraryfolders.vdf"
            vdf2.write_text(
                f'"libraryfolders" {{ "0" {{ "path" "{extra_lib}" "apps" {{ "200" "1" }} }} }}',
                encoding="utf-8",
            )
            (extra_lib / "steamapps" / "appmanifest_200.acf").write_text(
                '"AppState" { "appid" "200" "name" "Flatpak Library Game" }',
                encoding="utf-8",
            )

            with mock.patch("ph_proton_launcher.steam.DEFAULT_STEAM_VDF_PATHS", (vdf1, vdf2)):
                libs = get_steam_libraries()
                lib_paths = [path for path, _ in libs]
                self.assertIn(native_steam, lib_paths)
                self.assertIn(extra_lib, lib_paths)
                self.assertIn(flatpak_steam, lib_paths)
                self.assertEqual(resolve_game_name("200"), "Flatpak Library Game")

    def test_detect_running_games_scans_proc_and_deduplicates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            proc = root / "proc"
            steam_root = root / "Steam"
            (steam_root / "steamapps").mkdir(parents=True)
            vdf = steam_root / "steamapps" / "libraryfolders.vdf"
            vdf.write_text(
                f'"libraryfolders"\n{{\n\t"0"\n\t{{\n\t\t"path" "{steam_root}"\n\t\t"apps" {{ "230410" "1" "620" "1" }}\n\t}}\n}}\n',
                encoding="utf-8",
            )
            (steam_root / "steamapps" / "appmanifest_230410.acf").write_text(
                '"AppState" { "appid" "230410" "name" "Warframe" }',
                encoding="utf-8",
            )
            (steam_root / "steamapps" / "appmanifest_620.acf").write_text(
                '"AppState" { "appid" "620" "name" "Portal 2" }',
                encoding="utf-8",
            )

            def add_proc(pid: str, argv: list[str]) -> None:
                pdir = proc / pid
                pdir.mkdir(parents=True)
                (pdir / "cmdline").write_bytes(b"\x00".join(a.encode() for a in argv) + b"\x00")

            add_proc("101", ["/home/user/.steam/ubuntu12_32/reaper", "SteamLaunch", "AppId=230410", "--", "game.exe"])
            add_proc("102", ["/bin/sh", "-c", "SteamLaunch AppId=230410 -- other"])
            add_proc("103", ["reaper", "SteamLaunch", "AppId=620"])
            add_proc("104", ["pgrep", "-af", "SteamLaunch AppId=999"])
            add_proc("105", ["reaper", "SteamLaunch", "AppId=777"])
            (proc / "self").mkdir()

            games = detect_running_games(proc_root=proc, vdf_path=vdf, self_pid=105)
            self.assertEqual(
                games,
                [
                    SteamGame("620", "Portal 2"),
                    SteamGame("230410", "Warframe"),
                ],
            )

    def test_extract_appid_ignores_grep_tools(self) -> None:
        self.assertIsNone(extract_appid_from_cmdline(["/usr/bin/pgrep", "-af", "SteamLaunch AppId=123"]))
        self.assertIsNone(extract_appid_from_cmdline(["grep", "SteamLaunch AppId=123"]))
        self.assertIsNone(extract_appid_from_cmdline(["ag", "SteamLaunch AppId=123"]))
        self.assertEqual(
            extract_appid_from_cmdline(["reaper", "SteamLaunch", "AppId=1091500", "--"]),
            "1091500",
        )


class DownloaderTests(unittest.TestCase):
    def test_validate_download_url_enforces_https_and_allowlist(self) -> None:
        _validate_download_url("https://github.com/winsiderss/systeminformer/releases/download/v4/a.zip")
        _validate_download_url("https://release-assets.githubusercontent.com/123")
        _validate_download_url("https://cfhcable.dl.sourceforge.net/project/processhacker/a.zip")

        with self.assertRaises(ValueError):
            _validate_download_url("http://github.com/file.zip")
        with self.assertRaises(ValueError):
            _validate_download_url("https://evil.example.com/file.zip")

    def test_trusted_redirect_handler_blocks_untrusted_hops(self) -> None:
        handler = _TrustedRedirectHandler()
        req = urllib.request.Request("https://sourceforge.net/projects/processhacker/download")
        with self.assertRaises(ValueError):
            handler.redirect_request(req, None, 302, "Found", {}, "http://github.com/evil.zip")
        with self.assertRaises(ValueError):
            handler.redirect_request(req, None, 302, "Found", {}, "https://evil.example.com/evil.zip")
        redirected = handler.redirect_request(
            req,
            None,
            302,
            "Found",
            {},
            "https://cfhcable.dl.sourceforge.net/project/processhacker/processhacker-2.39-bin.zip",
        )
        self.assertIsNotNone(redirected)

    def test_safe_extract_zip_blocks_zip_slip_zip_bomb_and_extracts_valid_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            evil_zip = root / "evil.zip"
            evil_zip.write_bytes(_make_zip_bytes({"../escape.txt": b"bad"}))
            with self.assertRaises(ValueError):
                _safe_extract_zip(evil_zip, root / "out")

            abs_zip = root / "abs.zip"
            abs_zip.write_bytes(_make_zip_bytes({"/tmp/abs.txt": b"bad"}))
            with self.assertRaises(ValueError):
                _safe_extract_zip(abs_zip, root / "out2")

            bomb_zip = root / "bomb.zip"
            bomb_zip.write_bytes(_make_zip_bytes({"big.bin": b"A" * 2048}))
            with self.assertRaises(ValueError):
                _safe_extract_zip(bomb_zip, root / "bomb_out", max_extracted_bytes=1024)

            good_zip = root / "good.zip"
            good_zip.write_bytes(
                _make_zip_bytes({"x64/ProcessHacker.exe": b"MZ", "x86/ProcessHacker.exe": b"MZ32"})
            )
            out_dir = root / "good_out"
            _safe_extract_zip(good_zip, out_dir)
            exe = find_executable_in_dir(out_dir, "ProcessHacker.exe")
            self.assertEqual(exe, out_dir / "x64" / "ProcessHacker.exe")

            win_dir_zip = root / "win_dir.zip"
            win_dir_zip.write_bytes(
                _make_zip_bytes({
                    ".": b"",
                    "x64\\": b"",
                    "x64\\ProcessHacker.exe": b"MZ",
                })
            )
            win_out = root / "win_out"
            _safe_extract_zip(win_dir_zip, win_out)
            self.assertTrue((win_out / "x64").is_dir())
            self.assertEqual(
                find_executable_in_dir(win_out, "ProcessHacker.exe"),
                win_out / "x64" / "ProcessHacker.exe",
            )

    def test_select_system_informer_asset_prefers_64bit_and_extracts_digest(self) -> None:
        payload = {
            "tag_name": "v4.0.26241.138",
            "assets": [
                {
                    "name": "systeminformer-4.0.26241.138-release-setup.exe",
                    "browser_download_url": "https://github.com/winsiderss/systeminformer/releases/download/v4/setup.exe",
                },
                {
                    "name": "systeminformer-4.0.26241.138-bin.zip",
                    "browser_download_url": "https://github.com/winsiderss/systeminformer/releases/download/v4/bin.zip",
                    "digest": "sha256:" + ("a" * 64),
                },
            ],
        }
        asset = select_system_informer_asset(payload)
        self.assertEqual(asset.version, "v4.0.26241.138")
        self.assertEqual(asset.exe_name, "SystemInformer.exe")
        self.assertEqual(asset.expected_sha256, "a" * 64)

        bad_hex_payload = {
            "tag_name": "v4.0.26241.138",
            "assets": [
                {
                    "name": "systeminformer-4.0.26241.138-bin.zip",
                    "browser_download_url": "https://github.com/winsiderss/systeminformer/releases/download/v4/bin.zip",
                    "digest": "sha256:" + ("z" * 64),
                },
            ],
        }
        self.assertIsNone(select_system_informer_asset(bad_hex_payload).expected_sha256)

    def test_download_and_install_tool_verifies_sha256_and_sets_portable_mode(self) -> None:
        si_zip = _make_zip_bytes(
            {
                "amd64/SystemInformer.exe": b"MZ-64",
                "x86/SystemInformer.exe": b"MZ-32",
            }
        )
        si_sha = hashlib.sha256(si_zip).hexdigest()
        release_json = json.dumps(
            {
                "tag_name": "v4.0.999",
                "assets": [
                    {
                        "name": "systeminformer-bin.zip",
                        "browser_download_url": "https://github.com/winsiderss/systeminformer/releases/download/v4.0.999/systeminformer-bin.zip",
                        "digest": f"sha256:{si_sha}",
                    }
                ],
            }
        ).encode("utf-8")

        def fake_opener(req, timeout=20):  # noqa: ARG001
            url = req.full_url
            if "api.github.com" in url:
                return _FakeHttpResponse(release_json, url)
            return _FakeHttpResponse(si_zip, url)

        with tempfile.TemporaryDirectory() as tmp:
            tools_dir = Path(tmp) / "tools"
            progress_events: list[tuple[int, int]] = []
            result = download_and_install_tool(
                TOOL_SYSTEM_INFORMER,
                tools_dir=tools_dir,
                progress_callback=lambda done, total: progress_events.append((done, total)),
                opener=fake_opener,
            )
            self.assertEqual(result.exe_path, tools_dir / TOOL_SYSTEM_INFORMER / "amd64" / "SystemInformer.exe")
            self.assertTrue(result.exe_path.is_file())
            self.assertTrue((result.exe_path.parent / "SystemInformer.exe.settings.xml").is_file())
            self.assertEqual(result.sha256, si_sha)
            self.assertTrue(progress_events)

            # Re-installing preserves existing non-empty portable settings XML
            settings_file = result.exe_path.parent / "SystemInformer.exe.settings.xml"
            settings_file.write_bytes(b"<settings><dark>1</dark></settings>")
            reinstalled = download_and_install_tool(
                TOOL_SYSTEM_INFORMER,
                tools_dir=tools_dir,
                opener=fake_opener,
            )
            self.assertEqual(
                (reinstalled.exe_path.parent / "SystemInformer.exe.settings.xml").read_bytes(),
                b"<settings><dark>1</dark></settings>",
            )

            # Corrupt SHA-256 fails for Process Hacker 2.39
            bad_ph_zip = _make_zip_bytes({"x64/ProcessHacker.exe": b"BAD"})
            self.assertNotEqual(hashlib.sha256(bad_ph_zip).hexdigest(), PROCESS_HACKER_239_SHA256)
            with self.assertRaises(ValueError):
                download_and_install_tool(
                    TOOL_PROCESS_HACKER,
                    tools_dir=tools_dir,
                    opener=lambda req, timeout=20: _FakeHttpResponse(
                        bad_ph_zip,
                        "https://cfhcable.dl.sourceforge.net/project/processhacker/processhacker-2.39-bin.zip",
                    ),
                )


class ConfigAndLauncherTests(unittest.TestCase):
    def test_config_round_trip_and_corrupt_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg_path = Path(tmp) / "sub" / "config.json"
            self.assertEqual(load_config(cfg_path).process_hacker_exe, detect_default_exe())

            save_config(AppConfig(process_hacker_exe="/tmp/CustomPH.exe"), cfg_path)
            self.assertEqual(load_config(cfg_path).process_hacker_exe, "/tmp/CustomPH.exe")

            cfg_path.write_text("{corrupt json", encoding="utf-8")
            self.assertEqual(load_config(cfg_path).process_hacker_exe, detect_default_exe())

    def test_get_default_config_path_respects_xdg_config_home(self) -> None:
        with mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": "/tmp/custom-xdg-cfg"}, clear=False):
            self.assertEqual(
                get_default_config_path(),
                Path("/tmp/custom-xdg-cfg/ph-proton-launcher/config.json"),
            )
        with mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": ""}, clear=False):
            self.assertEqual(
                get_default_config_path(),
                Path.home() / ".config" / "ph-proton-launcher" / "config.json",
            )

    def test_detect_default_exe_prefers_existing_candidate_including_system_informer(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            missing = root / "ProcessHacker.exe"
            si_exe = root / "SystemInformer.exe"
            si_exe.write_bytes(b"MZ")
            with mock.patch("ph_proton_launcher.config.DEFAULT_EXE_CANDIDATES", (missing, si_exe)):
                self.assertEqual(detect_default_exe(), str(si_exe))
            with mock.patch("ph_proton_launcher.config.DEFAULT_EXE_CANDIDATES", (missing,)):
                self.assertEqual(detect_default_exe(), DEFAULT_PROCESS_HACKER_EXE)

    def test_detect_default_exe_honors_runtime_xdg_data_home(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            xdg_data = root / "xdg_data"
            fake_home = root / "home"
            fake_home.mkdir()
            managed_exe = (
                xdg_data / "ph-proton-launcher" / "tools" / "system-informer" / "amd64" / "SystemInformer.exe"
            )
            managed_exe.parent.mkdir(parents=True)
            managed_exe.write_bytes(b"MZ")

            with (
                mock.patch("ph_proton_launcher.config.Path.home", return_value=fake_home),
                mock.patch("ph_proton_launcher.downloader.Path.home", return_value=fake_home),
                mock.patch.dict(os.environ, {"XDG_DATA_HOME": str(xdg_data)}, clear=False),
            ):
                self.assertEqual(detect_default_exe(), str(managed_exe))

    def test_resolve_protontricks_command_native_and_flatpak(self) -> None:
        with mock.patch(
            "ph_proton_launcher.launcher.shutil.which",
            side_effect=lambda name: "/usr/bin/protontricks-launch" if name == "protontricks-launch" else None,
        ):
            self.assertEqual(resolve_protontricks_command(), ["protontricks-launch"])

        with (
            mock.patch(
                "ph_proton_launcher.launcher.shutil.which",
                side_effect=lambda name: "/usr/bin/flatpak" if name == "flatpak" else None,
            ),
            mock.patch(
                "ph_proton_launcher.launcher.subprocess.run",
                return_value=subprocess.CompletedProcess([], 0),
            ),
        ):
            self.assertEqual(
                resolve_protontricks_command(),
                ["flatpak", "run", "--command=protontricks-launch", FLATPAK_PROTONTRICKS_APP_ID],
            )

        with mock.patch("ph_proton_launcher.launcher.shutil.which", return_value=None):
            self.assertIsNone(resolve_protontricks_command())

    def test_tool_display_name_and_flatpak_filesystem_injection(self) -> None:
        self.assertEqual(tool_display_name("/tmp/ProcessHacker.exe"), "Process Hacker")
        self.assertEqual(tool_display_name("/tmp/SystemInformer.exe"), "System Informer")
        self.assertEqual(tool_display_name("/tmp/MyTool.exe"), "MyTool")

        cmd = build_launch_command(
            "230410",
            "/tmp/tools/SystemInformer.exe",
            verbose=True,
            launcher_prefix=[
                "flatpak",
                "run",
                "--command=protontricks-launch",
                FLATPAK_PROTONTRICKS_APP_ID,
            ],
        )
        self.assertEqual(
            cmd,
            [
                "flatpak",
                "run",
                "--filesystem=/tmp/tools",
                "--command=protontricks-launch",
                FLATPAK_PROTONTRICKS_APP_ID,
                "-v",
                "--no-bwrap",
                "--appid",
                "230410",
                "/tmp/tools/SystemInformer.exe",
            ],
        )

        with tempfile.TemporaryDirectory() as tmp:
            real_dir = Path(tmp) / "real"
            real_dir.mkdir()
            real_exe = real_dir / "SystemInformer.exe"
            real_exe.write_bytes(b"MZ")
            link_exe = Path(tmp) / "link.exe"
            link_exe.symlink_to(real_exe)
            symlink_cmd = build_launch_command(
                "230410",
                link_exe,
                launcher_prefix=[
                    "flatpak",
                    "run",
                    "--command=protontricks-launch",
                    FLATPAK_PROTONTRICKS_APP_ID,
                ],
            )
            self.assertIn(f"--filesystem={real_dir.resolve()}", symlink_cmd)
            self.assertEqual(symlink_cmd[-1], str(real_exe.resolve()))

    def test_build_command_verbose_and_validation(self) -> None:
        cmd = build_launch_command("230410", "~/ProcessHacker/x64/ProcessHacker.exe")
        self.assertEqual(
            cmd,
            [
                "protontricks-launch",
                "--no-bwrap",
                "--appid",
                "230410",
                str(Path.home() / "ProcessHacker" / "x64" / "ProcessHacker.exe"),
            ],
        )

        verbose_cmd = build_launch_command(
            "230410",
            "~/ProcessHacker/x64/ProcessHacker.exe",
            verbose=True,
        )
        self.assertEqual(
            verbose_cmd,
            [
                "protontricks-launch",
                "-v",
                "--no-bwrap",
                "--appid",
                "230410",
                str(Path.home() / "ProcessHacker" / "x64" / "ProcessHacker.exe"),
            ],
        )

        with tempfile.TemporaryDirectory() as tmp:
            fake_exe = Path(tmp) / "ProcessHacker.exe"
            fake_exe.write_bytes(b"MZ")
            with mock.patch("ph_proton_launcher.launcher.shutil.which", return_value="/usr/bin/protontricks-launch"):
                err, valid_cmd = validate_launch_inputs("230410", fake_exe, verbose=True)
                self.assertIsNone(err)
                self.assertEqual(
                    valid_cmd,
                    ["protontricks-launch", "-v", "--no-bwrap", "--appid", "230410", str(fake_exe)],
                )

                err_bad_id, none_cmd = validate_launch_inputs("abc", fake_exe)
                self.assertIsNotNone(err_bad_id)
                self.assertIsNone(none_cmd)

                err_missing_exe, none_cmd2 = validate_launch_inputs("230410", Path(tmp) / "nope.exe")
                self.assertIsNotNone(err_missing_exe)
                self.assertIsNone(none_cmd2)

    def test_auto_launch_success_failure_and_long_run_teardown(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fake_exe = Path(tmp) / "ProcessHacker.exe"
            fake_exe.write_bytes(b"MZ")
            cfg = AppConfig(process_hacker_exe=str(fake_exe))
            game = SteamGame("230410", "Warframe")

            notifications: list[tuple[str, str]] = []
            errors: list[tuple[str, str, str]] = []

            def fake_notify(title: str, body: str) -> bool:
                notifications.append((title, body))
                return True

            def fake_err(title: str, msg: str, details: str) -> None:
                errors.append((title, msg, details))

            with mock.patch("ph_proton_launcher.launcher.shutil.which", return_value="/usr/bin/protontricks-launch"):
                ok_runner = mock.Mock(
                    return_value=subprocess.CompletedProcess([], 0, stdout="", stderr="")
                )
                rc = run_auto_launch(
                    game,
                    cfg,
                    runner=ok_runner,
                    notifier=fake_notify,
                    error_presenter=fake_err,
                )
                self.assertEqual(rc, 0)
                self.assertEqual(len(notifications), 1)
                self.assertEqual(errors, [])

                fast_clock = mock.Mock(side_effect=[100.0, 101.5])
                fail_runner = mock.Mock(
                    return_value=subprocess.CompletedProcess(
                        [], 2, stdout="out", stderr="wine failed"
                    )
                )
                rc_fail = run_auto_launch(
                    game,
                    cfg,
                    runner=fail_runner,
                    notifier=fake_notify,
                    error_presenter=fake_err,
                    clock=fast_clock,
                )
                self.assertEqual(rc_fail, 2)
                self.assertEqual(len(errors), 1)
                self.assertIn("wine failed", errors[0][2])

                errors.clear()
                long_clock = mock.Mock(side_effect=[100.0, 145.0])
                teardown_runner = mock.Mock(
                    return_value=subprocess.CompletedProcess([], 15, stdout="", stderr="SIGTERM")
                )
                rc_teardown = run_auto_launch(
                    game,
                    cfg,
                    runner=teardown_runner,
                    notifier=fake_notify,
                    error_presenter=fake_err,
                    clock=long_clock,
                )
                self.assertEqual(rc_teardown, 0)
                self.assertEqual(errors, [])

                tb_clock = mock.Mock(side_effect=[100.0, 145.0])
                tb_runner = mock.Mock(
                    return_value=subprocess.CompletedProcess(
                        [], 1, stdout="", stderr="Traceback (most recent call last):"
                    )
                )
                rc_tb = run_auto_launch(
                    game,
                    cfg,
                    runner=tb_runner,
                    notifier=fake_notify,
                    error_presenter=fake_err,
                    clock=tb_clock,
                )
                self.assertEqual(rc_tb, 1)
                self.assertEqual(len(errors), 1)

    def test_cli_auto_routes_to_gui_when_zero_or_multiple_games(self) -> None:
        with (
            mock.patch("ph_proton_launcher.__main__.load_config", return_value=AppConfig()),
            mock.patch("ph_proton_launcher.__main__.detect_running_games", return_value=[]),
            mock.patch("ph_proton_launcher.__main__._run_gui", return_value=0) as mock_gui,
        ):
            rc = cli_main.main(["--auto"])
            self.assertEqual(rc, 0)
            mock_gui.assert_called_once()

        two_games = [SteamGame("620", "Portal 2"), SteamGame("230410", "Warframe")]
        with (
            mock.patch("ph_proton_launcher.__main__.load_config", return_value=AppConfig()),
            mock.patch("ph_proton_launcher.__main__.detect_running_games", return_value=two_games),
            mock.patch("ph_proton_launcher.__main__._run_gui", return_value=0) as mock_gui,
        ):
            rc = cli_main.main(["--auto"])
            self.assertEqual(rc, 0)
            mock_gui.assert_called_once()

    def test_cli_auto_runs_silently_without_gui_for_single_game(self) -> None:
        one_game = [SteamGame("230410", "Warframe")]
        with (
            mock.patch("ph_proton_launcher.__main__.load_config", return_value=AppConfig()),
            mock.patch("ph_proton_launcher.__main__.detect_running_games", return_value=one_game),
            mock.patch("ph_proton_launcher.__main__.run_auto_launch", return_value=0) as mock_auto,
            mock.patch("ph_proton_launcher.__main__._run_gui") as mock_gui,
        ):
            rc = cli_main.main(["--auto"])
            self.assertEqual(rc, 0)
            mock_auto.assert_called_once()
            mock_gui.assert_not_called()

    def test_cli_download_saves_installed_exe_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fake_exe = Path(tmp) / "ProcessHacker.exe"
            fake_result = DownloadResult(
                tool_key=TOOL_PROCESS_HACKER,
                display_name="Process Hacker",
                version="2.39",
                exe_path=fake_exe,
                install_dir=Path(tmp),
                sha256="0" * 64,
            )
            cfg = AppConfig()
            with (
                mock.patch("ph_proton_launcher.__main__.load_config", return_value=cfg),
                mock.patch(
                    "ph_proton_launcher.__main__.download_and_install_tool",
                    return_value=fake_result,
                ) as mock_dl,
                mock.patch(
                    "ph_proton_launcher.__main__.save_config",
                    return_value=Path(tmp) / "config.json",
                ) as mock_save,
            ):
                rc = cli_main.main(["--download", TOOL_PROCESS_HACKER])
                self.assertEqual(rc, 0)
                mock_dl.assert_called_once_with(TOOL_PROCESS_HACKER)
                mock_save.assert_called_once()
                self.assertEqual(cfg.process_hacker_exe, str(fake_exe))

    def test_process_tree_discovery_and_targeted_signaling(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            proc = Path(tmp) / "proc"

            def add_stat(pid: int, comm: str, ppid: int) -> None:
                pdir = proc / str(pid)
                pdir.mkdir(parents=True)
                (pdir / "stat").write_text(
                    f"{pid} ({comm}) S {ppid} 100 100 0 -1 0 0 0 0 0 0 0 0 20 0 1 0 0 0 0",
                    encoding="utf-8",
                )

            # Tree: 100 (protontricks-launch) -> 101 (sh) -> 102 (wine (ProcessHacker))
            # Unrelated game/wineserver PID 500 must never be touched.
            add_stat(100, "protontricks-la", 1)
            add_stat(101, "sh", 100)
            add_stat(102, "wine (ProcessHacker)", 101)
            add_stat(500, "wineserver", 1)

            descendants, leaves = list_descendant_pids(100, proc_root=proc)
            self.assertEqual(descendants, [102, 101])
            self.assertEqual(leaves, [102])

            term_calls: list[tuple[int, int]] = []
            signaled_term = signal_process_tree(
                100,
                signal.SIGTERM,
                leaves_only=True,
                proc_root=proc,
                killer=lambda p, s: term_calls.append((p, s)),
            )
            self.assertEqual(signaled_term, [102])
            self.assertEqual(term_calls, [(102, signal.SIGTERM)])

            kill_calls: list[tuple[int, int]] = []
            signaled_kill = signal_process_tree(
                100,
                signal.SIGKILL,
                leaves_only=False,
                proc_root=proc,
                killer=lambda p, s: kill_calls.append((p, s)),
            )
            self.assertEqual(signaled_kill, [102, 101, 100])
            self.assertEqual(
                kill_calls,
                [(102, signal.SIGKILL), (101, signal.SIGKILL), (100, signal.SIGKILL)],
            )


class HeadlessGuiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        cls.app = create_qapplication([])

    def test_verbose_drawer_and_selection_preservation(self) -> None:
        games = [SteamGame("620", "Portal 2"), SteamGame("230410", "Warframe")]
        with tempfile.TemporaryDirectory() as tmp:
            cfg_path = Path(tmp) / "config.json"
            win = MainWindow(
                config=AppConfig(),
                config_path=cfg_path,
                start_verbose=False,
                initial_games=games,
            )
            self.assertTrue(win.log_container.isHidden())
            self.assertTrue(win.stop_button.isHidden())
            self.assertEqual(win.selected_appid(), "620")

            # Selecting second game and refreshing keeps AppID 230410 selected
            win.games_list.setCurrentRow(1)
            self.assertEqual(win.selected_appid(), "230410")
            win.refresh_games(games=[SteamGame("570", "Dota 2"), SteamGame("230410", "Warframe")])
            self.assertEqual(win.selected_appid(), "230410")

            win.manual_appid_input.setText("999999")
            win._on_manual_appid_edited("999999")
            self.assertEqual(win.selected_appid(), "999999")

            win.verbose_checkbox.setChecked(True)
            QApplication.processEvents()
            self.assertFalse(win.log_container.isHidden())

            win_verbose = MainWindow(
                config=AppConfig(),
                config_path=cfg_path,
                start_verbose=True,
                initial_games=[],
            )
            self.assertFalse(win_verbose.log_container.isHidden())
            self.assertEqual(win_verbose.selected_appid(), "")
            win.deleteLater()
            win_verbose.deleteLater()

    def test_launch_selected_passes_verbose_flag_to_command(self) -> None:
        games = [SteamGame("230410", "Warframe")]
        with tempfile.TemporaryDirectory() as tmp:
            fake_exe = Path(tmp) / "ProcessHacker.exe"
            fake_exe.write_bytes(b"MZ")
            cfg_path = Path(tmp) / "config.json"
            win = MainWindow(
                config=AppConfig(process_hacker_exe=str(fake_exe)),
                config_path=cfg_path,
                start_verbose=False,
                initial_games=games,
            )
            with (
                mock.patch("ph_proton_launcher.launcher.shutil.which", return_value="/usr/bin/protontricks-launch"),
                mock.patch.object(win, "start_command") as mock_start,
            ):
                win.launch_selected()
                mock_start.assert_called_once()
                program, args = mock_start.call_args[0]
                self.assertEqual(program, "protontricks-launch")
                self.assertNotIn("-v", args)

                mock_start.reset_mock()
                win.verbose_checkbox.setChecked(True)
                win.launch_selected()
                program_v, args_v = mock_start.call_args[0]
                self.assertEqual(program_v, "protontricks-launch")
                self.assertEqual(args_v[0], "-v")

            win.deleteLater()

    def test_qprocess_streams_stdout_and_stderr_live_and_toggles_stop_button(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg_path = Path(tmp) / "config.json"
            win = MainWindow(
                config=AppConfig(),
                config_path=cfg_path,
                start_verbose=True,
                initial_games=[],
            )
            self.assertTrue(win.stop_button.isHidden())

            loop = QEventLoop()
            win.process.finished.connect(lambda _code, _status: loop.quit())
            QTimer.singleShot(5000, loop.quit)

            win.start_command(
                sys.executable,
                [
                    "-c",
                    "import sys; print('stdout line'); print('stderr line', file=sys.stderr)",
                ],
                target_label="Test Stream",
            )
            self.assertFalse(win.stop_button.isHidden())
            loop.exec()
            QApplication.processEvents()

            self.assertTrue(win.stop_button.isHidden())
            log_text = win.log_view.toPlainText()
            self.assertIn("stdout line", log_text)
            self.assertIn("stderr line", log_text)
            self.assertIn("[finished] exit code 0", log_text)
            win.deleteLater()

    def test_stop_button_suppresses_error_dialog_and_close_progress_dialog_blocks_cancel(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg_path = Path(tmp) / "config.json"
            win = MainWindow(
                config=AppConfig(),
                config_path=cfg_path,
                start_verbose=True,
                initial_games=[],
            )

            started_loop = QEventLoop()
            finished_loop = QEventLoop()
            win.process.started.connect(started_loop.quit)
            win.process.finished.connect(lambda _code, _status: finished_loop.quit())
            QTimer.singleShot(5000, started_loop.quit)
            QTimer.singleShot(5000, finished_loop.quit)

            with mock.patch("ph_proton_launcher.ui.show_error_dialog") as mock_err:
                win.start_command(
                    sys.executable,
                    ["-c", "import time; time.sleep(30)"],
                    target_label="Long Process",
                )
                started_loop.exec()
                self.assertEqual(win.stop_button.text(), "Stop")
                win.stop_button.click()
                self.assertEqual(win.stop_button.text(), "Kill")
                finished_loop.exec()
                QApplication.processEvents()

                mock_err.assert_not_called()
                self.assertTrue(win.stop_button.isHidden())
                self.assertEqual(win.stop_button.text(), "Stop")
                self.assertIn("Stopped ", win.status_label.text())
                self.assertIn("[stop] Sent SIGTERM", win.log_view.toPlainText())

            # Closing progress dialog on completion must not emit canceled
            from PyQt6.QtWidgets import QProgressDialog

            dlg = QProgressDialog("Downloading…", "Cancel", 0, 100, win)
            cancel_calls: list[bool] = []
            dlg.canceled.connect(lambda: cancel_calls.append(True))
            win._progress_dialog = dlg
            win._close_progress_dialog()
            QApplication.processEvents()
            self.assertEqual(cancel_calls, [])

            win.deleteLater()

    def test_resize_never_overlaps_widgets_and_stop_terminates_child_tree(self) -> None:
        games = [SteamGame("311210", "Call of Duty: Black Ops III")]
        with tempfile.TemporaryDirectory() as tmp:
            cfg_path = Path(tmp) / "config.json"
            win = MainWindow(
                config=AppConfig(),
                config_path=cfg_path,
                start_verbose=False,
                initial_games=games,
            )
            win.status_label.setText(
                "Running Process Hacker for Call of Duty: Black Ops III (311210) (PID 20651)…"
            )
            win.show()

            for expanded in (False, True):
                win.verbose_checkbox.setChecked(expanded)
                QApplication.processEvents()
                win._sync_window_size()
                QApplication.processEvents()

                for target_h in (win.minimumHeight(), 340 if not expanded else 500, 620):
                    win.resize(520, max(win.minimumHeight(), target_h))
                    QApplication.processEvents()
                    self.assertLess(
                        win.games_list.geometry().bottom(),
                        win.manual_appid_input.geometry().top(),
                    )
                    self.assertLess(
                        win.manual_appid_input.geometry().bottom(),
                        win.exe_path_input.geometry().top(),
                    )

            # Real 2-level child process: sh -> python sleep; Stop must terminate child leaf.
            pid_file = Path(tmp) / "child.pid"
            child_script = (
                f"import pathlib, os, time; "
                f"pathlib.Path({str(pid_file)!r}).write_text(str(os.getpid())); "
                f"time.sleep(30)"
            )
            started_loop = QEventLoop()
            finished_loop = QEventLoop()
            win.process.started.connect(started_loop.quit)
            win.process.finished.connect(lambda _code, _status: finished_loop.quit())
            QTimer.singleShot(5000, started_loop.quit)
            QTimer.singleShot(5000, finished_loop.quit)

            with mock.patch("ph_proton_launcher.ui.show_error_dialog") as mock_err:
                win.start_command(
                    "/bin/sh",
                    ["-c", f"{sys.executable} -c {child_script!r}; true"],
                    target_label="Child Tree",
                )
                started_loop.exec()

                # Wait briefly for grandchild python process to write pid_file
                for _ in range(50):
                    if pid_file.is_file() and pid_file.read_text().strip():
                        break
                    QApplication.processEvents()
                    QTimer.singleShot(20, lambda: None)
                    import time as _time

                    _time.sleep(0.02)

                self.assertTrue(pid_file.is_file())
                child_pid = int(pid_file.read_text().strip())

                win.stop_button.click()
                finished_loop.exec()
                QApplication.processEvents()

                mock_err.assert_not_called()
                with self.assertRaises(ProcessLookupError):
                    os.kill(child_pid, 0)

            win.deleteLater()

    def test_keyboard_selection_clears_manual_appid_and_close_hides_during_download(self) -> None:
        games = [SteamGame("620", "Portal 2"), SteamGame("230410", "Warframe")]
        with tempfile.TemporaryDirectory() as tmp:
            cfg_path = Path(tmp) / "config.json"
            win = MainWindow(
                config=AppConfig(),
                config_path=cfg_path,
                start_verbose=False,
                initial_games=games,
            )
            win.manual_appid_input.setText("999999")
            win._on_manual_appid_edited("999999")
            self.assertEqual(win.selected_appid(), "999999")

            # Changing current row (e.g. keyboard navigation) clears manual AppID
            win.games_list.setCurrentRow(0)
            self.assertEqual(win.manual_appid_input.text(), "")
            self.assertEqual(win.selected_appid(), "620")

            # Closing while a download thread is still running must hide and defer quit
            fake_thread = mock.Mock()
            fake_thread.isRunning.return_value = True
            fake_thread.wait.return_value = False
            fake_worker = mock.Mock()
            win._download_thread = fake_thread
            win._download_worker = fake_worker

            win.show()
            event = QCloseEvent()
            with mock.patch("ph_proton_launcher.ui.QApplication.quit") as mock_quit:
                win.closeEvent(event)

            self.assertFalse(event.isAccepted())
            self.assertTrue(win.isHidden())
            self.assertTrue(win._quit_on_finish)
            fake_worker.cancel.assert_called_once()
            fake_thread.quit.assert_called_once()
            fake_thread.wait.assert_called_once_with(2000)
            mock_quit.assert_not_called()

            win._download_thread = None
            win._download_worker = None
            win.deleteLater()


if __name__ == "__main__":
    unittest.main()

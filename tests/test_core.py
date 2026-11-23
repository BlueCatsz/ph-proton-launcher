"""Unit and headless PyQt6 tests for Process Hacker Proton Launcher."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from PyQt6.QtCore import QEventLoop, QTimer
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
from ph_proton_launcher.launcher import (
    FLATPAK_PROTONTRICKS_APP_ID,
    build_launch_command,
    resolve_protontricks_command,
    run_auto_launch,
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


class VdfAndSteamTests(unittest.TestCase):
    def test_default_steam_vdf_paths_includes_flatpak(self) -> None:
        self.assertTrue(
            any("com.valvesoftware.Steam" in str(path) for path in DEFAULT_STEAM_VDF_PATHS)
        )

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
        self.assertEqual(
            extract_appid_from_cmdline(["reaper", "SteamLaunch", "AppId=1091500", "--"]),
            "1091500",
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

                # Long-running session terminated when game exits: suppress modal error
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

                # Long-running session with actual Protontricks traceback still alerts
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


class HeadlessGuiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        cls.app = create_qapplication([])

    def test_verbose_drawer_and_selection_behavior(self) -> None:
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
            self.assertEqual(win.selected_appid(), "620")

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

    def test_qprocess_streams_stdout_and_stderr_live(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg_path = Path(tmp) / "config.json"
            win = MainWindow(
                config=AppConfig(),
                config_path=cfg_path,
                start_verbose=True,
                initial_games=[],
            )
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
            loop.exec()
            QApplication.processEvents()

            log_text = win.log_view.toPlainText()
            self.assertIn("stdout line", log_text)
            self.assertIn("stderr line", log_text)
            self.assertIn("[finished] exit code 0", log_text)
            win.deleteLater()


if __name__ == "__main__":
    unittest.main()

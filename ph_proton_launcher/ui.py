"""PyQt6 GUI and Wayland/KDE Plasma native integration."""

from __future__ import annotations

import shlex
import time
from pathlib import Path

from PyQt6.QtCore import QProcess, QRegularExpression, Qt, QTimer
from PyQt6.QtGui import QFontDatabase, QIcon, QKeySequence, QRegularExpressionValidator, QTextCursor
from PyQt6.QtWidgets import (
    QApplication,
    QCheckBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from . import APP_ID, APP_NAME
from .config import DEFAULT_PROCESS_HACKER_EXE, AppConfig, save_config
from .launcher import is_expected_teardown_exit, validate_launch_inputs
from .steam import SteamGame, detect_running_games

_ICON_NAME = "utilities-system-monitor"
_MAX_DETAIL_CHARS = 32768


def create_qapplication(argv: list[str] | None = None) -> QApplication:
    """Create or reuse a ``QApplication`` configured for KDE Plasma Wayland."""
    existing = QApplication.instance()
    if isinstance(existing, QApplication):
        return existing

    app = QApplication(argv if argv is not None else [])
    app.setApplicationName(APP_NAME)
    app.setApplicationDisplayName(APP_NAME)
    app.setDesktopFileName(APP_ID)
    icon = QIcon.fromTheme(_ICON_NAME)
    if not icon.isNull():
        app.setWindowIcon(icon)
    return app


def show_error_dialog(
    title: str,
    message: str,
    details: str = "",
    parent: QWidget | None = None,
) -> None:
    """Display a native critical error dialog, lazily initializing Qt if needed."""
    create_qapplication([])
    box = QMessageBox(
        QMessageBox.Icon.Critical,
        title,
        message,
        QMessageBox.StandardButton.Ok,
        parent,
    )
    if details and details.strip():
        box.setDetailedText(details.strip())
    box.exec()


class MainWindow(QMainWindow):
    """Compact main window for selecting a Steam game and launching Process Hacker."""

    def __init__(
        self,
        config: AppConfig,
        config_path: Path | None = None,
        start_verbose: bool = False,
        initial_games: list[SteamGame] | None = None,
    ) -> None:
        super().__init__()
        self._config = config
        self._config_path = config_path
        self._output_chunks: list[str] = []
        self._output_size = 0
        self._last_error_handled = False
        self._quit_on_finish = False
        self._active_target_label = ""
        self._process_started_at: float | None = None

        self.process = QProcess(self)
        self.process.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
        self.process.started.connect(self._on_process_started)
        self.process.readyReadStandardOutput.connect(self._on_process_output)
        self.process.errorOccurred.connect(self._on_process_error)
        self.process.finished.connect(self._on_process_finished)

        self.setWindowTitle("Process Hacker Proton Launcher")
        icon = QIcon.fromTheme(_ICON_NAME)
        if not icon.isNull():
            self.setWindowIcon(icon)
        self.setMinimumWidth(520)

        self._build_ui(start_verbose=start_verbose)
        self.refresh_games(games=initial_games)
        self._sync_window_size()

    def _build_ui(self, *, start_verbose: bool) -> None:
        central = QWidget(self)
        self.setCentralWidget(central)

        root = QVBoxLayout(central)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(8)

        # 1. Running Steam Games header
        header_row = QHBoxLayout()
        header_row.setSpacing(8)
        games_heading = QLabel("Running Steam Games")
        heading_font = games_heading.font()
        heading_font.setBold(True)
        games_heading.setFont(heading_font)
        header_row.addWidget(games_heading)
        header_row.addStretch(1)

        self.refresh_button = QPushButton("Refresh")
        refresh_icon = QIcon.fromTheme("view-refresh")
        if not refresh_icon.isNull():
            self.refresh_button.setIcon(refresh_icon)
        self.refresh_button.setShortcut(QKeySequence("F5"))
        self.refresh_button.setToolTip("Rescan running Steam Proton games (F5)")
        self.refresh_button.clicked.connect(lambda: self.refresh_games())
        header_row.addWidget(self.refresh_button)
        root.addLayout(header_row)

        # 2. Games list
        self.games_list = QListWidget()
        self.games_list.setMinimumHeight(96)
        self.games_list.setMaximumHeight(140)
        self.games_list.setAlternatingRowColors(True)
        self.games_list.itemClicked.connect(self._on_game_clicked)
        self.games_list.itemDoubleClicked.connect(self._on_game_double_clicked)
        root.addWidget(self.games_list)

        # 3. Manual AppID fallback row
        appid_row = QHBoxLayout()
        appid_row.setSpacing(8)
        appid_label = QLabel("Manual AppID:")
        self.manual_appid_input = QLineEdit()
        self.manual_appid_input.setPlaceholderText("Fallback AppID (e.g. 230410)")
        self.manual_appid_input.setValidator(
            QRegularExpressionValidator(QRegularExpression(r"^\d*$"), self)
        )
        self.manual_appid_input.setClearButtonEnabled(True)
        self.manual_appid_input.textEdited.connect(self._on_manual_appid_edited)
        self.manual_appid_input.returnPressed.connect(self.launch_selected)
        appid_row.addWidget(appid_label)
        appid_row.addWidget(self.manual_appid_input, stretch=1)
        root.addLayout(appid_row)

        # 4. Process Hacker executable path row
        exe_row = QHBoxLayout()
        exe_row.setSpacing(8)
        exe_label = QLabel("Process Hacker:")
        self.exe_path_input = QLineEdit(
            self._config.process_hacker_exe or DEFAULT_PROCESS_HACKER_EXE
        )
        self.exe_path_input.setPlaceholderText(DEFAULT_PROCESS_HACKER_EXE)
        self.exe_path_input.editingFinished.connect(self._persist_exe_path)

        self.browse_button = QPushButton("Browse…")
        browse_icon = QIcon.fromTheme("document-open")
        if not browse_icon.isNull():
            self.browse_button.setIcon(browse_icon)
        self.browse_button.clicked.connect(self._on_browse_exe)

        exe_row.addWidget(exe_label)
        exe_row.addWidget(self.exe_path_input, stretch=1)
        exe_row.addWidget(self.browse_button)
        root.addLayout(exe_row)

        # 5. Controls row
        controls_row = QHBoxLayout()
        controls_row.setSpacing(8)
        self.verbose_checkbox = QCheckBox("Verbose Log")
        self.verbose_checkbox.setChecked(start_verbose)
        self.verbose_checkbox.setToolTip(
            "Show live stdout/stderr inside the window and pass -v to protontricks-launch"
        )
        self.verbose_checkbox.toggled.connect(self._on_verbose_toggled)
        controls_row.addWidget(self.verbose_checkbox)
        controls_row.addStretch(1)

        self.launch_button = QPushButton("Launch")
        launch_icon = QIcon.fromTheme("system-run")
        if not launch_icon.isNull():
            self.launch_button.setIcon(launch_icon)
        self.launch_button.setDefault(True)
        self.launch_button.clicked.connect(self.launch_selected)
        controls_row.addWidget(self.launch_button)
        root.addLayout(controls_row)

        # 6. Collapsible verbose output drawer
        self.log_container = QWidget()
        log_layout = QVBoxLayout(self.log_container)
        log_layout.setContentsMargins(0, 4, 0, 0)
        log_layout.setSpacing(4)

        log_header = QHBoxLayout()
        log_header.setSpacing(8)
        log_title = QLabel("Verbose Output")
        log_header.addWidget(log_title)
        log_header.addStretch(1)

        self.clear_log_button = QPushButton("Clear")
        self.clear_log_button.clicked.connect(self._clear_log)
        log_header.addWidget(self.clear_log_button)
        log_layout.addLayout(log_header)

        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(5000)
        self.log_view.setFont(
            QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont)
        )
        self.log_view.setMinimumHeight(180)
        log_layout.addWidget(self.log_view)

        self.log_container.setVisible(start_verbose)
        root.addWidget(self.log_container, stretch=1)

        # 7. Status indicator
        self.status_label = QLabel("Ready.")
        self.status_label.setWordWrap(True)
        root.addWidget(self.status_label)

    def _sync_window_size(self) -> None:
        if self.centralWidget() and self.centralWidget().layout():
            self.centralWidget().layout().activate()
        hint = self.sizeHint()
        target_width = max(self.width(), 540)
        if self.verbose_checkbox.isChecked():
            target_height = max(hint.height(), 470)
        else:
            target_height = hint.height()
        self.resize(target_width, target_height)

    def _on_verbose_toggled(self, checked: bool) -> None:
        self.log_container.setVisible(checked)
        QTimer.singleShot(0, self._sync_window_size)

    def _clear_log(self) -> None:
        self.log_view.clear()
        self._output_chunks.clear()
        self._output_size = 0

    def _append_log_text(self, text: str) -> None:
        if not text:
            return
        cursor = self.log_view.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        cursor.insertText(text)
        self.log_view.setTextCursor(cursor)
        self.log_view.ensureCursorVisible()

        self._output_chunks.append(text)
        self._output_size += len(text)
        while self._output_size > _MAX_DETAIL_CHARS and len(self._output_chunks) > 1:
            removed = self._output_chunks.pop(0)
            self._output_size -= len(removed)

    def _append_log_line(self, line: str) -> None:
        self._append_log_text(line.rstrip("\n") + "\n")

    def _collected_output(self) -> str:
        return "".join(self._output_chunks).strip()

    def _set_running_state(self, running: bool) -> None:
        self.launch_button.setEnabled(not running)
        self.launch_button.setText("Running…" if running else "Launch")

    def _persist_exe_path(self) -> None:
        text = self.exe_path_input.text().strip()
        if not text or text == self._config.process_hacker_exe:
            return
        self._config.process_hacker_exe = text
        try:
            save_config(self._config, self._config_path)
        except OSError as exc:
            self.status_label.setText(f"Warning: could not save config ({exc}).")

    def _on_browse_exe(self) -> None:
        current = Path(self.exe_path_input.text().strip() or DEFAULT_PROCESS_HACKER_EXE).expanduser()
        start_dir = str(current.parent if current.parent.exists() else Path.home())
        selected, _ = QFileDialog.getOpenFileName(
            self,
            "Select Process Hacker Executable",
            start_dir,
            "Windows Executables (*.exe);;All Files (*)",
        )
        if selected:
            self.exe_path_input.setText(selected)
            self._persist_exe_path()

    def _on_game_clicked(self, item: QListWidgetItem) -> None:
        if item.data(Qt.ItemDataRole.UserRole):
            self.manual_appid_input.clear()

    def _on_game_double_clicked(self, item: QListWidgetItem) -> None:
        appid = item.data(Qt.ItemDataRole.UserRole)
        if not appid:
            return
        self.manual_appid_input.clear()
        self.games_list.setCurrentItem(item)
        self.launch_selected()

    def _on_manual_appid_edited(self, text: str) -> None:
        if text.strip():
            self.games_list.clearSelection()
            self.games_list.setCurrentItem(None)

    def refresh_games(self, games: list[SteamGame] | None = None) -> None:
        """Reload the running Steam Proton games list."""
        detected = detect_running_games() if games is None else games
        self.games_list.clear()

        if not detected:
            placeholder = QListWidgetItem("No running Steam games detected")
            placeholder.setFlags(Qt.ItemFlag.NoItemFlags)
            self.games_list.addItem(placeholder)
            if self.process.state() == QProcess.ProcessState.NotRunning:
                self.status_label.setText(
                    "No running Steam games detected. Enter an AppID manually or click Refresh."
                )
            return

        for game in detected:
            item = QListWidgetItem(game.display_label)
            item.setData(Qt.ItemDataRole.UserRole, game.appid)
            self.games_list.addItem(item)

        if not self.manual_appid_input.text().strip():
            self.games_list.setCurrentRow(0)

        if self.process.state() == QProcess.ProcessState.NotRunning:
            noun = "game" if len(detected) == 1 else "games"
            self.status_label.setText(f"Detected {len(detected)} running Steam {noun}.")

    def selected_appid(self) -> str:
        """Return the target AppID from manual input or selected game row."""
        manual = self.manual_appid_input.text().strip()
        if manual:
            return manual

        item = self.games_list.currentItem()
        if item is not None and item.isSelected():
            data = item.data(Qt.ItemDataRole.UserRole)
            if isinstance(data, str) and data.strip():
                return data.strip()
        return ""

    def _selected_target_description(self, appid: str) -> str:
        manual = self.manual_appid_input.text().strip()
        if manual:
            return f"AppID {appid}"
        item = self.games_list.currentItem()
        if item is not None and item.data(Qt.ItemDataRole.UserRole) == appid:
            return item.text()
        return f"AppID {appid}"

    def start_command(
        self, program: str, arguments: list[str], target_label: str = ""
    ) -> None:
        """Start an external command asynchronously using ``QProcess``."""
        if self.process.state() != QProcess.ProcessState.NotRunning:
            self.status_label.setText("A launch is already in progress.")
            return

        self._output_chunks.clear()
        self._output_size = 0
        self._last_error_handled = False
        self._active_target_label = target_label or program
        self._process_started_at = time.monotonic()

        self._append_log_line(f"$ {shlex.join([program, *arguments])}")
        self._set_running_state(True)
        self.status_label.setText(f"Launching Process Hacker for {self._active_target_label}…")
        self.process.start(program, arguments)

    def launch_selected(self) -> None:
        """Validate inputs and launch Process Hacker for the selected game or manual AppID."""
        if self.process.state() != QProcess.ProcessState.NotRunning:
            self.status_label.setText("A launch is already in progress.")
            return

        self._persist_exe_path()
        appid = self.selected_appid()
        exe_path = self.exe_path_input.text().strip()

        error_msg, cmd = validate_launch_inputs(
            appid,
            exe_path,
            verbose=self.verbose_checkbox.isChecked(),
        )
        if error_msg or cmd is None:
            self.status_label.setText(error_msg or "Invalid launch configuration.")
            show_error_dialog(
                "Cannot Launch Process Hacker",
                error_msg or "Invalid launch configuration.",
                parent=self,
            )
            return

        target_label = self._selected_target_description(appid)
        self.start_command(cmd[0], cmd[1:], target_label=target_label)

    def _on_process_started(self) -> None:
        pid = self.process.processId()
        self._append_log_line(f"[started] PID {pid}")
        self.status_label.setText(
            f"Running Process Hacker for {self._active_target_label} (PID {pid})…"
        )

    def _on_process_output(self) -> None:
        raw = bytes(self.process.readAllStandardOutput())
        if raw:
            self._append_log_text(raw.decode("utf-8", errors="replace"))

    def _on_process_error(self, error: QProcess.ProcessError) -> None:
        if error == QProcess.ProcessError.FailedToStart:
            err_text = self.process.errorString()
            self._append_log_line(f"[error] Failed to start: {err_text}")
            self._set_running_state(False)
            self._last_error_handled = True
            self.status_label.setText(f"Failed to start: {err_text}")
            show_error_dialog(
                "Process Hacker Launch Failed",
                f"Failed to start protontricks-launch for {self._active_target_label}.",
                self._collected_output() or err_text,
                parent=None if self.isHidden() else self,
            )
            if self._quit_on_finish:
                QApplication.quit()

    def _on_process_finished(
        self, exit_code: int, exit_status: QProcess.ExitStatus
    ) -> None:
        self._on_process_output()
        self._set_running_state(False)

        elapsed = (
            time.monotonic() - self._process_started_at
            if self._process_started_at is not None
            else 0.0
        )
        self._process_started_at = None

        crashed = exit_status == QProcess.ExitStatus.CrashExit
        status_name = "crashed" if crashed else "normal"
        self._append_log_line(f"[finished] exit code {exit_code} ({status_name})")

        details = self._collected_output()
        if exit_code == 0 and not crashed:
            self.status_label.setText("Process Hacker exited cleanly (code 0).")
        elif is_expected_teardown_exit(
            exit_code if exit_code != 0 else 1,
            elapsed,
            details,
        ):
            self.status_label.setText(
                f"Process Hacker closed (code {exit_code} — game may have exited)."
            )
        else:
            self.status_label.setText(f"Launch failed (exit code {exit_code}).")
            if not self._last_error_handled:
                show_error_dialog(
                    "Process Hacker Launch Failed",
                    (
                        f"protontricks-launch exited with code {exit_code} "
                        f"for {self._active_target_label}."
                    ),
                    details,
                    parent=None if self.isHidden() else self,
                )

        if self._quit_on_finish:
            QApplication.quit()

    def closeEvent(self, event) -> None:
        """Hide instead of terminating ``QProcess`` if a launch is still active."""
        self._persist_exe_path()
        if self.process.state() != QProcess.ProcessState.NotRunning:
            self._quit_on_finish = True
            event.ignore()
            self.hide()
            return

        event.accept()
        QApplication.quit()

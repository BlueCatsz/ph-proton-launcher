"""PyQt6 GUI and Wayland/KDE Plasma native integration."""

from __future__ import annotations

import shlex
import threading
import time
from pathlib import Path

from PyQt6.QtCore import (
    QObject,
    QProcess,
    QRegularExpression,
    Qt,
    QThread,
    QTimer,
    pyqtSignal,
)
from PyQt6.QtGui import (
    QFontDatabase,
    QIcon,
    QKeySequence,
    QRegularExpressionValidator,
    QTextCursor,
)
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
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QProgressDialog,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from . import APP_ID, APP_NAME
from .config import DEFAULT_PROCESS_HACKER_EXE, AppConfig, save_config
from .downloader import (
    TOOL_PROCESS_HACKER,
    TOOL_SYSTEM_INFORMER,
    DownloadCancelledError,
    DownloadResult,
    download_and_install_tool,
)
from .launcher import (
    is_expected_teardown_exit,
    tool_display_name,
    validate_launch_inputs,
)
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


class _DownloadWorker(QObject):
    """Background worker for downloading and extracting Process Hacker / System Informer."""

    progress = pyqtSignal(int, int)
    finished = pyqtSignal(object)
    cancelled = pyqtSignal()
    failed = pyqtSignal(str)

    def __init__(self, tool_key: str) -> None:
        super().__init__()
        self._tool_key = tool_key
        self._cancel_event = threading.Event()

    def cancel(self) -> None:
        """Request cancellation of the active download."""
        self._cancel_event.set()

    def run(self) -> None:
        """Run the blocking download/extraction on the worker thread."""
        try:
            result = download_and_install_tool(
                self._tool_key,
                progress_callback=lambda done, total: self.progress.emit(done, total),
                cancel_check=self._cancel_event.is_set,
            )
        except DownloadCancelledError:
            self.cancelled.emit()
        except Exception as exc:
            self.failed.emit(str(exc))
        else:
            self.finished.emit(result)


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
        self._stop_requested = False
        self._terminate_sent = False
        self._active_target_label = ""
        self._active_tool_name = tool_display_name(config.process_hacker_exe)
        self._process_started_at: float | None = None

        self._download_thread: QThread | None = None
        self._download_worker: _DownloadWorker | None = None
        self._progress_dialog: QProgressDialog | None = None
        self._download_label = ""

        self.process = QProcess(self)
        self.process.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
        self.process.started.connect(self._on_process_started)
        self.process.readyReadStandardOutput.connect(self._on_process_output)
        self.process.errorOccurred.connect(self._on_process_error)
        self.process.finished.connect(self._on_process_finished)

        self.setWindowTitle(APP_NAME)
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

        # 4. Executable path row (Browse + Auto-Download)
        exe_row = QHBoxLayout()
        exe_row.setSpacing(8)
        exe_label = QLabel("Executable:")
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

        self.download_button = QPushButton("Download…")
        download_icon = QIcon.fromTheme("download")
        if not download_icon.isNull():
            self.download_button.setIcon(download_icon)
        self.download_button.setToolTip(
            "Automatically download portable Process Hacker 2.39 or System Informer"
        )
        download_menu = QMenu(self.download_button)
        act_ph = download_menu.addAction(
            "Process Hacker 2.39 (Classic — recommended for Proton)"
        )
        act_ph.triggered.connect(lambda _checked=False: self.start_download(TOOL_PROCESS_HACKER))
        act_si = download_menu.addAction("System Informer (Latest Stable)")
        act_si.triggered.connect(lambda _checked=False: self.start_download(TOOL_SYSTEM_INFORMER))
        self.download_button.setMenu(download_menu)

        exe_row.addWidget(exe_label)
        exe_row.addWidget(self.exe_path_input, stretch=1)
        exe_row.addWidget(self.browse_button)
        exe_row.addWidget(self.download_button)
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

        self.stop_button = QPushButton("Stop")
        stop_icon = QIcon.fromTheme("process-stop")
        if not stop_icon.isNull():
            self.stop_button.setIcon(stop_icon)
        self.stop_button.setToolTip("Terminate the active launcher process")
        self.stop_button.setVisible(False)
        self.stop_button.clicked.connect(self._on_stop_clicked)
        controls_row.addWidget(self.stop_button)

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
        if self.centralWidget():
            self.centralWidget().updateGeometry()
            if self.centralWidget().layout():
                self.centralWidget().layout().activate()
        target_width = max(self.width() if self.isVisible() else 580, 520)
        if self.verbose_checkbox.isChecked():
            target_height = max(self.sizeHint().height(), 470)
        else:
            target_height = self.minimumSizeHint().height()
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
        self.stop_button.setText("Stop")
        self.stop_button.setToolTip("Terminate the active launcher process")
        self.stop_button.setVisible(running)

    def _persist_exe_path(self, *, force: bool = False) -> None:
        text = self.exe_path_input.text().strip()
        if not text or (not force and text == self._config.process_hacker_exe):
            return
        self._config.process_hacker_exe = text
        try:
            save_config(self._config, self._config_path)
        except OSError as exc:
            self.status_label.setText(f"Warning: could not save config ({exc}).")

    def _on_browse_exe(self) -> None:
        current = Path(
            self.exe_path_input.text().strip() or DEFAULT_PROCESS_HACKER_EXE
        ).expanduser()
        start_dir = str(current.parent if current.parent.exists() else Path.home())
        selected, _ = QFileDialog.getOpenFileName(
            self,
            "Select Process Hacker or System Informer Executable",
            start_dir,
            "Windows Executables (*.exe);;All Files (*)",
        )
        if selected:
            self.exe_path_input.setText(selected)
            self._persist_exe_path(force=True)

    def start_download(self, tool_key: str) -> None:
        """Download and install a portable Process Hacker or System Informer build."""
        if self._download_thread is not None and self._download_thread.isRunning():
            self.status_label.setText("A download is already in progress.")
            return

        self._download_label = (
            "Process Hacker 2.39"
            if tool_key == TOOL_PROCESS_HACKER
            else "System Informer"
        )
        self.download_button.setEnabled(False)
        self.status_label.setText(f"Downloading {self._download_label}…")

        dlg = QProgressDialog(
            f"Downloading {self._download_label}…",
            "Cancel",
            0,
            0,
            self,
        )
        dlg.setWindowTitle("Downloading Tool")
        dlg.setWindowModality(Qt.WindowModality.WindowModal)
        dlg.setMinimumDuration(0)
        dlg.setAutoClose(False)
        dlg.setAutoReset(False)
        dlg.show()
        self._progress_dialog = dlg

        thread = QThread(self)
        worker = _DownloadWorker(tool_key)
        worker.moveToThread(thread)

        thread.started.connect(worker.run)
        worker.progress.connect(self._on_download_progress)
        worker.finished.connect(self._on_download_finished)
        worker.cancelled.connect(self._on_download_cancelled)
        worker.failed.connect(self._on_download_failed)
        dlg.canceled.connect(self._on_cancel_download_clicked)

        worker.finished.connect(thread.quit)
        worker.cancelled.connect(thread.quit)
        worker.failed.connect(thread.quit)
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(self._on_download_thread_finished)

        self._download_thread = thread
        self._download_worker = worker
        thread.start()

    def _on_cancel_download_clicked(self) -> None:
        if self._download_worker is not None:
            self._download_worker.cancel()
        if self._progress_dialog is not None:
            self._progress_dialog.setLabelText("Cancelling download…")

    def _close_progress_dialog(self) -> None:
        if self._progress_dialog is not None:
            self._progress_dialog.blockSignals(True)
            self._progress_dialog.close()
            self._progress_dialog.deleteLater()
            self._progress_dialog = None
        self.download_button.setEnabled(True)

    def _on_download_progress(self, downloaded: int, total: int) -> None:
        dlg = self._progress_dialog
        if dlg is None:
            return
        mb_done = downloaded / 1048576
        if total > 0:
            if dlg.maximum() != 100:
                dlg.setRange(0, 100)
            dlg.setValue(min(100, int(downloaded * 100 / total)))
            mb_total = total / 1048576
            dlg.setLabelText(
                f"Downloading {self._download_label}… ({mb_done:.1f} / {mb_total:.1f} MiB)"
            )
        else:
            if dlg.maximum() != 0:
                dlg.setRange(0, 0)
            dlg.setLabelText(f"Downloading {self._download_label}… ({mb_done:.1f} MiB)")

    def _on_download_finished(self, result: DownloadResult) -> None:
        self._close_progress_dialog()
        self.exe_path_input.setText(str(result.exe_path))
        self._persist_exe_path(force=True)
        self.status_label.setText(
            f"Installed {result.display_name} ({result.version}) to {result.exe_path}."
        )
        self._append_log_line(
            f"[download] Installed {result.display_name} {result.version} "
            f"(sha256: {result.sha256}) -> {result.exe_path}"
        )

    def _on_download_cancelled(self) -> None:
        self._close_progress_dialog()
        self.status_label.setText("Download cancelled.")

    def _on_download_failed(self, error_text: str) -> None:
        self._close_progress_dialog()
        self.status_label.setText(f"Download failed: {error_text}")
        show_error_dialog(
            "Download Failed",
            f"Could not download {self._download_label}.",
            error_text,
            parent=self,
        )

    def _on_download_thread_finished(self) -> None:
        if self._download_thread is not None:
            self._download_thread.deleteLater()
        self._download_thread = None
        self._download_worker = None

    def _prompt_download_missing_tool(self, exe_path: str) -> bool:
        """Offer automatic download when the configured executable path does not exist."""
        resolved = Path(exe_path).expanduser() if exe_path else None
        msg = (
            f"The configured executable was not found:\n{resolved}"
            if resolved
            else "No Process Hacker or System Informer executable is configured."
        )
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Question)
        box.setWindowTitle("Executable Not Found")
        box.setText(msg)
        box.setInformativeText(
            "Would you like to download a verified portable copy automatically?"
        )
        btn_ph = box.addButton(
            "Download Process Hacker 2.39",
            QMessageBox.ButtonRole.AcceptRole,
        )
        btn_si = box.addButton(
            "Download System Informer",
            QMessageBox.ButtonRole.ActionRole,
        )
        box.addButton(QMessageBox.StandardButton.Cancel)
        box.exec()

        clicked = box.clickedButton()
        if clicked == btn_ph:
            self.start_download(TOOL_PROCESS_HACKER)
        elif clicked == btn_si:
            self.start_download(TOOL_SYSTEM_INFORMER)
        else:
            self.status_label.setText("Executable not found.")
        return True

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
        """Reload the running Steam Proton games list while preserving selection."""
        current_item = self.games_list.currentItem()
        prev_appid = (
            current_item.data(Qt.ItemDataRole.UserRole)
            if current_item is not None and current_item.isSelected()
            else None
        )

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

        selected_row = 0
        for idx, game in enumerate(detected):
            item = QListWidgetItem(game.display_label)
            item.setData(Qt.ItemDataRole.UserRole, game.appid)
            self.games_list.addItem(item)
            if prev_appid and game.appid == prev_appid:
                selected_row = idx

        if not self.manual_appid_input.text().strip():
            self.games_list.setCurrentRow(selected_row)

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
        self,
        program: str,
        arguments: list[str],
        target_label: str = "",
        tool_name: str = "",
    ) -> None:
        """Start an external command asynchronously using ``QProcess``."""
        if self.process.state() != QProcess.ProcessState.NotRunning:
            self.status_label.setText("A launch is already in progress.")
            return

        self._output_chunks.clear()
        self._output_size = 0
        self._last_error_handled = False
        self._stop_requested = False
        self._terminate_sent = False
        self._active_target_label = target_label or program
        self._active_tool_name = tool_name or tool_display_name(
            self.exe_path_input.text().strip()
        )
        self._process_started_at = time.monotonic()

        self._append_log_line(f"$ {shlex.join([program, *arguments])}")
        self._set_running_state(True)
        self.status_label.setText(
            f"Launching {self._active_tool_name} for {self._active_target_label}…"
        )
        self.process.start(program, arguments)

    def launch_selected(self) -> None:
        """Validate inputs and launch the configured tool for the selected game."""
        if self.process.state() != QProcess.ProcessState.NotRunning:
            self.status_label.setText("A launch is already in progress.")
            return

        self._persist_exe_path()
        appid = self.selected_appid()
        exe_path = self.exe_path_input.text().strip()

        if appid.isdigit() and (not exe_path or not Path(exe_path).expanduser().is_file()):
            self._prompt_download_missing_tool(exe_path)
            return

        verbose = self.verbose_checkbox.isChecked()
        tool_name = tool_display_name(exe_path)
        error_msg, cmd = validate_launch_inputs(appid, exe_path, verbose=verbose)
        if error_msg or cmd is None:
            self.status_label.setText(error_msg or "Invalid launch configuration.")
            show_error_dialog(
                f"Cannot Launch {tool_name}",
                error_msg or "Invalid launch configuration.",
                parent=self,
            )
            return

        target_label = self._selected_target_description(appid)
        self.start_command(
            cmd[0],
            cmd[1:],
            target_label=target_label,
            tool_name=tool_name,
        )

    def _on_stop_clicked(self) -> None:
        if self.process.state() == QProcess.ProcessState.NotRunning:
            return
        self._stop_requested = True
        if not self._terminate_sent:
            self._terminate_sent = True
            self.stop_button.setText("Kill")
            self.stop_button.setToolTip("Force-kill the active launcher process")
            self._append_log_line("[stop] Sent SIGTERM; click Kill to force-terminate.")
            self.status_label.setText(f"Stopping {self._active_tool_name}…")
            self.process.terminate()
        else:
            self._append_log_line("[stop] Sent SIGKILL.")
            self.process.kill()

    def _on_process_started(self) -> None:
        pid = self.process.processId()
        self._append_log_line(f"[started] PID {pid}")
        self.status_label.setText(
            f"Running {self._active_tool_name} for {self._active_target_label} (PID {pid})…"
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
                f"{self._active_tool_name} Launch Failed",
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
        was_stopped = self._stop_requested
        self._stop_requested = False
        self._terminate_sent = False
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
        if was_stopped:
            self.status_label.setText(f"Stopped {self._active_tool_name}.")
        elif exit_code == 0 and not crashed:
            self.status_label.setText(
                f"{self._active_tool_name} exited cleanly (code 0)."
            )
        elif is_expected_teardown_exit(
            exit_code if exit_code != 0 else 1,
            elapsed,
            details,
        ):
            self.status_label.setText(
                f"{self._active_tool_name} closed (code {exit_code} — game may have exited)."
            )
        else:
            self.status_label.setText(f"Launch failed (exit code {exit_code}).")
            if not self._last_error_handled:
                show_error_dialog(
                    f"{self._active_tool_name} Launch Failed",
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
        if self._download_thread is not None and self._download_thread.isRunning():
            if self._download_worker is not None:
                self._download_worker.cancel()
            self._close_progress_dialog()
            self._download_thread.quit()
            self._download_thread.wait(2000)

        if self.process.state() != QProcess.ProcessState.NotRunning:
            self._quit_on_finish = True
            event.ignore()
            self.hide()
            return

        event.accept()
        QApplication.quit()

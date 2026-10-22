"""Update check / download / install UI (see flyroll/updater.py)."""
from __future__ import annotations

import threading
from typing import Callable, Optional

from PySide6.QtCore import QObject, QSettings, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QAction, QDesktopServices
from PySide6.QtWidgets import (QDialog, QHBoxLayout, QLabel, QMenu, QMessageBox, QProgressDialog, QPushButton,
                               QTextBrowser, QToolButton, QVBoxLayout)

from .. import APP_NAME, __version__, updater
from ..updater import Release, UpdateError


class UpdateController(QObject):
    _found = Signal(object, bool)
    _none = Signal(bool)
    _failed = Signal(str, bool)
    _progress = Signal(int, int)
    _downloaded = Signal(object)
    _download_failed = Signal(str)

    def __init__(self, window, is_busy: Callable[[], bool], log: Callable[[str, str], None]) -> None:
        super().__init__(window)
        self.window = window
        self.is_busy = is_busy
        self.log = log
        self.settings = QSettings(APP_NAME, APP_NAME)
        self.release: Optional[Release] = None
        self._checking = False
        self._cancel = threading.Event()
        self._progress_dlg: Optional[QProgressDialog] = None

        # "Update available" pill shown in the header
        self.button = QPushButton()
        self.button.setObjectName("update")
        self.button.setVisible(False)
        self.button.clicked.connect(self.open_dialog)

        # "⋯" menu
        self.menu_button = QToolButton()
        self.menu_button.setText("⋯")
        self.menu_button.setPopupMode(QToolButton.InstantPopup)
        menu = QMenu(self.menu_button)
        a = QAction("Check for updates now", menu)
        a.triggered.connect(lambda: self.check(manual=True))
        menu.addAction(a)
        self.auto_action = QAction("Check for updates on startup", menu, checkable=True)
        self.auto_action.setChecked(self.settings.value("update/auto_check", True, type=bool))
        self.auto_action.toggled.connect(lambda on: self.settings.setValue("update/auto_check", on))
        menu.addAction(self.auto_action)
        menu.addSeparator()
        a = QAction("Release notes && downloads…", menu)
        a.triggered.connect(lambda: QDesktopServices.openUrl(QUrl(updater.RELEASES_PAGE)))
        menu.addAction(a)
        a = QAction(f"About {APP_NAME} {__version__}", menu)
        a.triggered.connect(self._about)
        menu.addAction(a)
        self.menu_button.setMenu(menu)

        self._found.connect(self._on_found)
        self._none.connect(self._on_none)
        self._failed.connect(self._on_failed)
        self._progress.connect(self._on_progress)
        self._downloaded.connect(self._on_downloaded)
        self._download_failed.connect(self._on_download_failed)

    # ------------------------------------------------------------------ check
    def start(self) -> None:
        if self.auto_action.isChecked():
            QTimer.singleShot(2500, lambda: self.check(manual=False))

    def check(self, manual: bool) -> None:
        if self._checking:
            return
        self._checking = True

        def work():
            try:
                rel = updater.check_latest()
                if rel:
                    self._found.emit(rel, manual)
                else:
                    self._none.emit(manual)
            except UpdateError as exc:
                self._failed.emit(str(exc), manual)

        threading.Thread(target=work, name="FlyRollUpdateCheck", daemon=True).start()

    def _on_found(self, rel: Release, manual: bool) -> None:
        self._checking = False
        if not manual and self.settings.value("update/skip", "") == rel.tag:
            return
        self.release = rel
        self.button.setText(f"⬆  Update to {rel.tag}")
        self.button.setVisible(True)
        self.log(f"{APP_NAME} {rel.tag} is available (you have {__version__})", "info")
        if manual:
            self.open_dialog()

    def _on_none(self, manual: bool) -> None:
        self._checking = False
        if manual:
            QMessageBox.information(self.window, APP_NAME, f"You have the latest version ({__version__}).")

    def _on_failed(self, msg: str, manual: bool) -> None:
        self._checking = False
        self.log(msg, "debug")
        if manual:
            QMessageBox.warning(self.window, APP_NAME, msg)

    # ------------------------------------------------------------------ dialog
    def open_dialog(self) -> None:
        rel = self.release
        if rel is None:
            return
        dlg = QDialog(self.window)
        dlg.setWindowTitle(f"{APP_NAME} update")
        dlg.resize(560, 440)
        lay = QVBoxLayout(dlg)
        head = QLabel(f"<h3>{rel.name}</h3>You have version {__version__}.")
        lay.addWidget(head)
        notes = QTextBrowser()
        notes.setOpenExternalLinks(True)
        notes.setMarkdown(rel.notes or "No release notes.")
        lay.addWidget(notes, 1)
        row = QHBoxLayout()
        skip = QPushButton("Skip this version")
        later = QPushButton("Later")
        ok, reason = updater.can_self_update()
        go = QPushButton("Update now" if ok and rel.asset_url else "Open download page")
        go.setObjectName("primary")
        go.setDefault(True)
        if not ok:
            lay.addWidget(QLabel(f"<i>{reason}</i>"))
        row.addWidget(skip)
        row.addStretch(1)
        row.addWidget(later)
        row.addWidget(go)
        lay.addLayout(row)

        def do_skip():
            self.settings.setValue("update/skip", rel.tag)
            self.button.setVisible(False)
            dlg.reject()

        def do_go():
            dlg.accept()
            if ok and rel.asset_url:
                self._download()
            else:
                QDesktopServices.openUrl(QUrl(rel.page_url))

        skip.clicked.connect(do_skip)
        later.clicked.connect(dlg.reject)
        go.clicked.connect(do_go)
        dlg.exec()
        dlg.deleteLater()

    # ------------------------------------------------------------------ download & install
    def _download(self) -> None:
        if self.is_busy():
            QMessageBox.information(self.window, APP_NAME, "Stop the recording first, then update.")
            return
        rel = self.release
        self._cancel.clear()
        dlg = QProgressDialog(f"Downloading {rel.asset_name}…", "Cancel", 0, 100, self.window)
        dlg.setWindowTitle(f"{APP_NAME} update")
        dlg.setWindowModality(Qt.WindowModal)
        dlg.setMinimumDuration(0)
        dlg.canceled.connect(self._cancel.set)
        dlg.setValue(0)
        self._progress_dlg = dlg

        def work():
            try:
                path = updater.download(rel, lambda done, total: (self._progress.emit(done, total),
                                                                   not self._cancel.is_set())[1])
                self._downloaded.emit(path)
            except UpdateError as exc:
                self._download_failed.emit(str(exc))

        threading.Thread(target=work, name="FlyRollUpdateDownload", daemon=True).start()

    def _on_progress(self, done: int, total: int) -> None:
        if self._progress_dlg and total:
            self._progress_dlg.setValue(min(99, int(done * 100 / total)))
            self._progress_dlg.setLabelText(f"Downloading {self.release.asset_name}…  "
                                            f"{done / 1048576:.1f} / {total / 1048576:.1f} MB")

    def _close_progress(self) -> None:
        if self._progress_dlg:
            self._progress_dlg.canceled.disconnect()
            self._progress_dlg.close()
            self._progress_dlg = None

    def _on_download_failed(self, msg: str) -> None:
        self._close_progress()
        if "cancelled" not in msg:
            QMessageBox.warning(self.window, APP_NAME, msg)

    def _on_downloaded(self, path) -> None:
        self._close_progress()
        if self.is_busy():
            QMessageBox.information(self.window, APP_NAME, "Stop the recording first, then update.")
            return
        if QMessageBox.question(self.window, APP_NAME,
                                f"{self.release.tag} is ready. {APP_NAME} will close, install the update "
                                f"and start again.\n\nYour logbook and profiles are kept.") != QMessageBox.Yes:
            return
        try:
            updater.install(path)
        except UpdateError as exc:
            QMessageBox.warning(self.window, APP_NAME, str(exc))
            return
        self.window.close()

    def _about(self) -> None:
        QMessageBox.about(self.window, APP_NAME,
                          f"<b>{APP_NAME} {__version__}</b><br>Flight recorder & replay for MSFS 2020 / 2024."
                          f"<br><a href='https://github.com/{updater.REPO}'>github.com/{updater.REPO}</a>"
                          "<br><br>Based on Sky Dolly by Oliver Knoll (MIT).")

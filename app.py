#!/usr/bin/env python3
"""
Serum 2 → Ableton Preset Converter
PySide6 GUI
"""

import subprocess, sys
from pathlib import Path

from PySide6.QtCore import Qt, QThread, Signal, QMimeData
from PySide6.QtGui import QFont, QIcon, QDragEnterEvent, QDropEvent
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QPushButton, QLineEdit, QFileDialog, QProgressBar,
    QTextEdit, QRadioButton, QButtonGroup, QGroupBox, QCheckBox, QFrame,
)

import converter

VERSION = "1.0.1"

# ── worker thread ──────────────────────────────────────────────────────────────

class ConvertWorker(QThread):
    progress = Signal(int, int)      # (done, total)
    log      = Signal(str, bool)     # (message, is_error)
    finished = Signal(int, int)      # (ok, fail)

    def __init__(self, presets, input_root, output_root, flat):
        super().__init__()
        self.presets     = presets
        self.input_root  = input_root
        self.output_root = output_root
        self.flat        = flat
        self._stop       = False

    def stop(self):
        self._stop = True

    def run(self):
        ok = fail = 0
        total = len(self.presets)
        for i, sp in enumerate(self.presets):
            if self._stop:
                self.log.emit("Cancelled.", False)
                break
            out = converter.output_path_for(sp, self.input_root, self.output_root, self.flat)
            try:
                converter.convert_one(sp, out)
                self.log.emit(f"✓  {sp.stem}", False)
                ok += 1
            except Exception as e:
                self.log.emit(f"✗  {sp.stem}  —  {e}", True)
                fail += 1
            self.progress.emit(i + 1, total)
        self.finished.emit(ok, fail)


# ── main window ────────────────────────────────────────────────────────────────

class MainWindow(QMainWindow):
    DEFAULT_INPUT  = "/Library/Audio/Presets/Xfer Records/Serum 2 Presets/Presets"
    DEFAULT_OUTPUT = str(Path.home() / "Desktop" / "Serum Vstpresets")

    def __init__(self):
        super().__init__()
        self.setWindowTitle("Serum to Live")
        self.setMinimumWidth(680)
        self.setAcceptDrops(True)
        self._selected_files = []
        self.worker = None
        self._build_ui()

    # ── UI construction ───────────────────────────────────────────────────────

    def _build_ui(self):
        root = QWidget()
        self.setCentralWidget(root)
        layout = QVBoxLayout(root)
        layout.setSpacing(16)
        layout.setContentsMargins(20, 20, 20, 20)

        # ── Input group ──
        input_group = QGroupBox("Input")
        ig = QVBoxLayout(input_group)
        ig.setSpacing(10)
        ig.setContentsMargins(12, 14, 12, 14)

        mode_row = QHBoxLayout()
        self.radio_folder = QRadioButton("Folder")
        self.radio_files  = QRadioButton("Individual files")
        self.radio_folder.setChecked(True)
        bg = QButtonGroup(self)
        bg.addButton(self.radio_folder)
        bg.addButton(self.radio_files)
        self.radio_folder.toggled.connect(self._on_mode_change)
        mode_row.addWidget(self.radio_folder)
        mode_row.addWidget(self.radio_files)
        mode_row.addStretch()
        ig.addLayout(mode_row)

        input_row = QHBoxLayout()
        self.input_edit = QLineEdit(self.DEFAULT_INPUT)
        self.input_edit.setPlaceholderText("Drop a folder or files here, or Browse…")
        self.btn_browse_input = QPushButton("Browse…")
        self.btn_browse_input.clicked.connect(self._browse_input)
        input_row.addWidget(self.input_edit)
        input_row.addWidget(self.btn_browse_input)
        ig.addLayout(input_row)

        self.file_count_label = QLabel("")
        self.file_count_label.setStyleSheet("font-size: 11px;")
        ig.addWidget(self.file_count_label)

        layout.addWidget(input_group)

        # ── Output group ──
        output_group = QGroupBox("Output")
        og = QVBoxLayout(output_group)
        og.setSpacing(10)
        og.setContentsMargins(12, 14, 12, 14)

        output_row = QHBoxLayout()
        self.output_edit = QLineEdit(self.DEFAULT_OUTPUT)
        self.output_edit.setPlaceholderText("Output folder…")
        btn_browse_out = QPushButton("Browse…")
        btn_browse_out.clicked.connect(self._browse_output)
        output_row.addWidget(self.output_edit)
        output_row.addWidget(btn_browse_out)
        og.addLayout(output_row)

        self.flat_check = QCheckBox("Flat output (no subfolders)")
        og.addWidget(self.flat_check)

        layout.addWidget(output_group)

        # ── Action buttons ──
        btn_row = QHBoxLayout()
        btn_row.setSpacing(8)
        self.btn_all = QPushButton("Convert All")
        self.btn_all.setFixedHeight(36)
        self.btn_all.clicked.connect(lambda: self._start(new_only=False))

        self.btn_new = QPushButton("Convert New Only")
        self.btn_new.setFixedHeight(36)
        self.btn_new.setToolTip("Skip presets that already have a matching .vstpreset in the output folder")
        self.btn_new.clicked.connect(lambda: self._start(new_only=True))

        self.btn_cancel = QPushButton("Cancel")
        self.btn_cancel.setFixedHeight(36)
        self.btn_cancel.setEnabled(False)
        self.btn_cancel.clicked.connect(self._cancel)

        btn_row.addWidget(self.btn_all)
        btn_row.addWidget(self.btn_new)
        btn_row.addStretch()
        btn_row.addWidget(self.btn_cancel)
        layout.addLayout(btn_row)

        # ── Progress ──
        self.progress_bar = QProgressBar()
        self.progress_bar.setTextVisible(True)
        self.progress_bar.hide()
        layout.addWidget(self.progress_bar)

        # ── Log ──
        self.log_box = QTextEdit()
        self.log_box.setReadOnly(True)
        self.log_box.setFont(QFont("Menlo", 11))
        self.log_box.setMinimumHeight(180)
        layout.addWidget(self.log_box)

        # ── Summary banner (hidden until conversion finishes) ──
        self.banner = QFrame()
        self.banner.setFrameShape(QFrame.NoFrame)
        self.banner.hide()
        banner_layout = QHBoxLayout(self.banner)
        banner_layout.setContentsMargins(14, 10, 14, 10)

        self.banner_label = QLabel()
        self.banner_label.setFont(QFont(self.font().family(), 13))

        self.btn_reveal = QPushButton("Reveal in Finder")
        self.btn_reveal.setFixedHeight(26)
        self.btn_reveal.setCursor(Qt.PointingHandCursor)
        self.btn_reveal.clicked.connect(self._reveal_in_finder)

        self.btn_dismiss = QPushButton("✕")
        self.btn_dismiss.setFixedSize(22, 22)
        self.btn_dismiss.setCursor(Qt.PointingHandCursor)
        self.btn_dismiss.clicked.connect(self.banner.hide)

        banner_layout.addWidget(self.banner_label)
        banner_layout.addStretch()
        banner_layout.addWidget(self.btn_reveal)
        banner_layout.addSpacing(8)
        banner_layout.addWidget(self.btn_dismiss)
        layout.addWidget(self.banner)

        # ── Footer ──
        footer = QLabel(f"Created by Omri Behr  ·  v{VERSION}  ·  2026")
        footer.setAlignment(Qt.AlignCenter)
        footer.setStyleSheet("font-size: 10px; color: palette(mid);")
        layout.addWidget(footer)

    # ── dark mode ─────────────────────────────────────────────────────────────

    def _is_dark(self) -> bool:
        from PySide6.QtCore import Qt as _Qt
        try:
            scheme = QApplication.styleHints().colorScheme()
            return scheme == _Qt.ColorScheme.Dark
        except AttributeError:
            return False

    def on_scheme_changed(self):
        # Re-apply banner styles if visible
        if not self.banner.isHidden():
            self._on_finished(*self._last_result)

    # ── drag and drop ─────────────────────────────────────────────────────────

    def dragEnterEvent(self, event: QDragEnterEvent):
        if event.mimeData().hasUrls():
            for url in event.mimeData().urls():
                p = Path(url.toLocalFile())
                if p.is_dir() or p.suffix == ".SerumPreset":
                    event.acceptProposedAction()
                    return
        event.ignore()

    def dropEvent(self, event: QDropEvent):
        urls  = event.mimeData().urls()
        paths = [Path(u.toLocalFile()) for u in urls]

        folders = [p for p in paths if p.is_dir()]
        files   = [p for p in paths if p.suffix == ".SerumPreset"]

        if folders:
            self.radio_folder.setChecked(True)
            self.input_edit.setText(str(folders[0]))
            self._refresh_count()
        elif files:
            self.radio_files.setChecked(True)
            self._selected_files = files
            self.input_edit.setText(f"{len(files)} file(s) selected")
            self.file_count_label.setText(f"{len(files)} preset(s) selected")

        event.acceptProposedAction()

    # ── slots ─────────────────────────────────────────────────────────────────

    def _on_mode_change(self):
        is_folder = self.radio_folder.isChecked()
        self.flat_check.setEnabled(is_folder)
        self.btn_new.setEnabled(is_folder)
        self.file_count_label.setText("")

    def _browse_input(self):
        if self.radio_folder.isChecked():
            path = QFileDialog.getExistingDirectory(self, "Select preset folder",
                                                    self.input_edit.text())
            if path:
                self.input_edit.setText(path)
                self._refresh_count()
        else:
            paths, _ = QFileDialog.getOpenFileNames(
                self, "Select presets", "",
                "Serum Presets (*.SerumPreset)")
            if paths:
                self._selected_files = [Path(p) for p in paths]
                self.input_edit.setText(f"{len(paths)} file(s) selected")
                self.file_count_label.setText(f"{len(paths)} preset(s) selected")

    def _browse_output(self):
        path = QFileDialog.getExistingDirectory(self, "Select output folder",
                                                self.output_edit.text())
        if path:
            self.output_edit.setText(path)
            if self.radio_folder.isChecked():
                self._refresh_count()

    def _refresh_count(self):
        folder = Path(self.input_edit.text())
        if folder.is_dir():
            n = len(list(folder.rglob("*.SerumPreset")))
            self.file_count_label.setText(f"{n} preset(s) found")

    def _start(self, new_only: bool):
        self.banner.hide()
        output_root = Path(self.output_edit.text())
        flat        = self.flat_check.isChecked()

        if self.radio_folder.isChecked():
            input_root = Path(self.input_edit.text())
            if not input_root.is_dir():
                self._log("Input folder not found.", error=True)
                return
            presets = (converter.find_new_presets(input_root, output_root, flat)
                       if new_only else converter.find_presets(input_root))
        else:
            presets    = self._selected_files
            input_root = presets[0].parent if presets else Path(".")

        if not presets:
            self._log("No presets to convert — everything is already up to date." if new_only
                      else "No presets found.")
            return

        self.log_box.clear()
        self._log(f"Starting: {len(presets)} preset(s) → {output_root}")
        self.progress_bar.setMaximum(len(presets))
        self.progress_bar.setValue(0)
        self.progress_bar.show()
        self._set_busy(True)

        self.worker = ConvertWorker(presets, input_root, output_root, flat)
        self.worker.progress.connect(self._on_progress)
        self.worker.log.connect(self._on_worker_log)
        self.worker.finished.connect(self._on_finished)
        self.worker.start()

    def _cancel(self):
        if self.worker:
            self.worker.stop()

    def _on_progress(self, done, total):
        self.progress_bar.setValue(done)
        self.progress_bar.setFormat(f"{done} / {total}")

    def _on_worker_log(self, msg, is_error):
        self._log(msg, error=is_error)

    def _on_finished(self, ok, fail):
        self._last_result = (ok, fail)
        self._set_busy(False)
        self.progress_bar.hide()

        dark = self._is_dark()
        dismiss_style = (
            "QPushButton { color: %s; background: transparent; border: none;"
            " font-size: 14px; padding: 0; }"
            "QPushButton:hover { color: %s; }"
        )

        if fail == 0:
            if dark:
                frame_style = ("QFrame { background: rgba(52,199,89,0.15); border: 1px solid rgba(52,199,89,0.40); border-radius: 8px; }"
                               "QLabel { color: #4ade80; background: transparent; border: none; }")
                reveal_style = ("QPushButton { color: #4ade80; background: transparent; border: none; font-size: 12px; }"
                                "QPushButton:hover { color: #86efac; }")
                dismiss_s = dismiss_style % ("rgba(74,222,128,0.5)", "#4ade80")
            else:
                frame_style = ("QFrame { background: rgba(52,199,89,0.10); border: 1px solid rgba(52,199,89,0.35); border-radius: 8px; }"
                               "QLabel { color: #1a5c2a; background: transparent; border: none; }")
                reveal_style = ("QPushButton { color: #1a7a35; background: transparent; border: none; font-size: 12px; }"
                                "QPushButton:hover { color: #0d4d20; }")
                dismiss_s = dismiss_style % ("rgba(26,92,42,0.5)", "#1a5c2a")
            self.banner_label.setText(f"✓  {ok} preset{'s' if ok != 1 else ''} converted")
        else:
            if dark:
                frame_style = ("QFrame { background: rgba(255,59,48,0.12); border: 1px solid rgba(255,59,48,0.40); border-radius: 8px; }"
                               "QLabel { color: #ff7b73; background: transparent; border: none; }")
                reveal_style = ("QPushButton { color: #ff7b73; background: transparent; border: none; font-size: 12px; }"
                                "QPushButton:hover { color: #fca5a5; }")
                dismiss_s = dismiss_style % ("rgba(255,123,115,0.5)", "#ff7b73")
            else:
                frame_style = ("QFrame { background: rgba(255,59,48,0.08); border: 1px solid rgba(255,59,48,0.30); border-radius: 8px; }"
                               "QLabel { color: #8a1f1a; background: transparent; border: none; }")
                reveal_style = ("QPushButton { color: #8a1f1a; background: transparent; border: none; font-size: 12px; }"
                                "QPushButton:hover { color: #5a0f0a; }")
                dismiss_s = dismiss_style % ("rgba(138,31,26,0.5)", "#8a1f1a")
            self.banner_label.setText(f"✓  {ok} converted  ·  ✗  {fail} failed — see log")

        self.banner.setStyleSheet(frame_style)
        self.btn_reveal.setStyleSheet(reveal_style)
        self.btn_dismiss.setStyleSheet(dismiss_s)
        self.banner.show()

    def _reveal_in_finder(self):
        subprocess.run(["open", self.output_edit.text()])

    # ── helpers ───────────────────────────────────────────────────────────────

    def _set_busy(self, busy: bool):
        self.btn_all.setEnabled(not busy)
        self.btn_new.setEnabled(not busy and self.radio_folder.isChecked())
        self.btn_cancel.setEnabled(busy)

    def _log(self, msg: str, error: bool = False):
        if error:
            self.log_box.append(f'<span style="color:#ff6b6b;">{msg}</span>')
        else:
            self.log_box.append(msg)
        self.log_box.verticalScrollBar().setValue(
            self.log_box.verticalScrollBar().maximum())


# ── entry point ────────────────────────────────────────────────────────────────

def main():
    app = QApplication(sys.argv)
    app.setApplicationName("Serum to Live")

    app.setStyleSheet("""
        QPushButton {
            border-radius: 6px;
            padding: 2px 10px;
        }
    """)

    _here = Path(getattr(sys, "_MEIPASS", Path(__file__).parent))
    icon_path = _here / "AppIcon.icns"
    if icon_path.exists():
        app.setWindowIcon(QIcon(str(icon_path)))

    win = MainWindow()

    try:
        app.styleHints().colorSchemeChanged.connect(win.on_scheme_changed)
    except AttributeError:
        pass

    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()

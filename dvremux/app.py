import time
import re
import shutil
import sys
import tempfile
from pathlib import Path

from PySide6.QtCore import QTimer
from PySide6.QtCore import QProcess, Qt
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
    QFrame,
    QLabel,
    QMainWindow,
    QPlainTextEdit,
    QPushButton,
    QProgressBar,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from .analyzer import analyze_ffprobe_json, build_summary
from .planner import build_plan
from .output import default_output_path, path_is_safe
from .job import RemuxJob
from .planner import build_plan
from .output import default_output_path, path_is_safe


ROOT = Path(__file__).resolve().parent
ICON_PATH = ROOT / "icon.svg"


def parse_dovi_summary(text):
    result = {
        "profile": None,
        "type": None,
        "dm_version": None,
        "frames": None,
        "raw": text.strip(),
    }

    match = re.search(r"Frames:\s*(\d+)", text)
    if match:
        result["frames"] = int(match.group(1))

    match = re.search(
        r"Profile:\s*(\d+)(?:\s*\(([^)]+)\))?",
        text,
        re.IGNORECASE,
    )
    if match:
        result["profile"] = int(match.group(1))
        if match.group(2):
            result["type"] = match.group(2).strip()

    match = re.search(
        r"DM version:\s*(\d+)(?:\s*\(([^)]+)\))?",
        text,
        re.IGNORECASE,
    )
    if match:
        result["dm_version"] = match.group(0).split(":", 1)[1].strip()

    return result


class DropArea(QFrame):
    def __init__(self, file_selected_callback):
        super().__init__()

        self.file_selected_callback = file_selected_callback

        self.setAcceptDrops(True)
        self.setObjectName("dropArea")
        self.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Preferred,
        )

        self.label = QLabel("Drop a movie here")
        self.label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.label.setWordWrap(True)
        self.label.setObjectName("dropTitle")
        self.label.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Preferred,
        )

        self.or_label = QLabel("or")
        self.or_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.or_label.setObjectName("muted")

        self.browse = QPushButton("Browse Files")
        self.browse.clicked.connect(self.browse_file)
        self.browse.setMaximumWidth(320)
        self.browse.setMinimumWidth(220)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(32, 28, 32, 28)
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.setSpacing(12)

        layout.addWidget(self.label)
        layout.addWidget(self.or_label)
        layout.addWidget(
            self.browse,
            alignment=Qt.AlignmentFlag.AlignHCenter,
        )

        self.setMinimumHeight(175)
        self.setMaximumHeight(245)

    def browse_file(self):
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Select Movie",
            "",
            (
                "Video Files "
                "(*.mkv *.mp4 *.m2ts *.mts *.ts *.mov);;"
                "All Files (*)"
            ),
        )

        if path:
            self.select_file(path)

    def select_file(self, path):
        path = Path(path)

        if not path.is_file():
            return

        self.label.setText(path.name)
        self.label.setToolTip(str(path))

        self.file_selected_callback(path)

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event):
        urls = event.mimeData().urls()

        if not urls:
            event.ignore()
            return

        path = urls[0].toLocalFile()

        if path:
            self.select_file(path)
            event.acceptProposedAction()
        else:
            event.ignore()


class DVRemuxWindow(QMainWindow):
    def __init__(self):
        super().__init__()

        self.current_source = None
        self.ffprobe_process = None
        self.dovi_extract_process = None
        self.dovi_info_process = None

        self.analysis_result = None
        self.deep_dovi = None
        self.output_path = None
        self.plan_ready = False
        self.job = None
        self.temp_dir = None
        self.rpu_path = None

        self.setWindowTitle("DV Remux")
        self.setWindowIcon(QIcon(str(ICON_PATH)))

        screen = QApplication.primaryScreen().availableGeometry()

        target_width = min(1000, int(screen.width() * 0.78))
        target_height = min(760, int(screen.height() * 0.82))

        self.resize(
            max(720, target_width),
            max(540, target_height),
        )

        self.setMinimumSize(650, 500)

        self.build_ui()

    def build_ui(self):
        title = QLabel("DV")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title.setObjectName("logo")

        remux = QLabel("R E M U X")
        remux.setAlignment(Qt.AlignmentFlag.AlignCenter)
        remux.setObjectName("remux")

        subtitle = QLabel(
            "DOLBY VISION   •   MP4   •   PROFILE 8.1   •   PREMIUM DOLBY AUDIO"
        )
        subtitle.setAlignment(Qt.AlignmentFlag.AlignCenter)
        subtitle.setWordWrap(True)
        subtitle.setObjectName("subtitle")

        self.drop_area = DropArea(self.analyze_source)

        self.analysis_title = QLabel("SOURCE ANALYSIS")
        self.analysis_title.setObjectName("sectionTitle")
        self.analysis_title.hide()

        self.status = QLabel("")
        self.status.setObjectName("status")
        self.status.setWordWrap(True)
        self.status.hide()

        self.analysis = QPlainTextEdit()
        self.analysis.setReadOnly(True)
        self.analysis.setObjectName("analysis")

        self.analysis.setLineWrapMode(
            QPlainTextEdit.LineWrapMode.WidgetWidth
        )

        self.analysis.setMinimumHeight(250)
        self.analysis.setMaximumHeight(360)

        self.analysis.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Preferred,
        )

        self.analysis.hide()

        self.plan_title = QLabel("WHAT DV REMUX WILL DO")
        self.plan_title.setObjectName("sectionTitle")
        self.plan_title.hide()

        self.plan = QPlainTextEdit()
        self.plan.setReadOnly(True)
        self.plan.setObjectName("plan")
        self.plan.setMinimumHeight(260)
        self.plan.setMaximumHeight(340)
        self.plan.hide()

        self.output_label = QLabel("OUTPUT")
        self.output_label.setObjectName("sectionTitle")
        self.output_label.hide()

        self.output_path_label = QLabel("")
        self.output_path_label.setObjectName("outputPath")
        self.output_path_label.setWordWrap(True)
        self.output_path_label.hide()

        self.choose_output = QPushButton("Choose Destination")
        self.choose_output.clicked.connect(self.choose_destination)
        self.choose_output.setMaximumWidth(280)
        self.choose_output.hide()

        self.export_button = QPushButton("MAKE DV 8.1 MP4")
        self.export_button.setObjectName("exportButton")
        self.export_button.setEnabled(False)
        self.export_button.setMinimumHeight(58)
        self.export_button.clicked.connect(self.start_job)
        self.export_button.hide()

        self.job_stage = QLabel("")
        self.job_stage.setObjectName("jobStage")
        self.job_stage.hide()

        self.job_status = QLabel("")
        self.job_status.setObjectName("jobStatus")
        self.job_status.setWordWrap(True)
        self.job_status.hide()

        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.setTextVisible(True)
        self.progress.hide()

        self.job_telemetry = QLabel("")
        self.job_telemetry.setWordWrap(True)
        self.job_telemetry.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        self.job_telemetry.hide()

        self._job_started_at = None

        self.telemetry_timer = QTimer(self)
        self.telemetry_timer.setInterval(1000)
        self.telemetry_timer.timeout.connect(
            self._update_job_telemetry
        )
        self.telemetry_timer.start()

        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.clicked.connect(self.cancel_job)
        self.cancel_button.setMaximumWidth(180)
        self.cancel_button.hide()

        page = QWidget()
        page.setObjectName("page")

        layout = QVBoxLayout(page)
        layout.setContentsMargins(48, 34, 48, 42)
        layout.setSpacing(12)

        layout.addWidget(title)
        layout.addWidget(remux)
        layout.addWidget(subtitle)

        layout.addSpacing(18)

        layout.addWidget(self.drop_area)

        layout.addSpacing(14)

        layout.addWidget(self.analysis_title)
        layout.addWidget(self.status)
        layout.addWidget(self.analysis)

        layout.addSpacing(16)
        layout.addWidget(self.plan_title)
        layout.addWidget(self.plan)

        layout.addSpacing(16)

        layout.addWidget(self.output_label)
        layout.addWidget(self.output_path_label)

        layout.addWidget(
            self.choose_output,
            alignment=Qt.AlignmentFlag.AlignLeft,
        )

        layout.addSpacing(8)

        layout.addWidget(self.export_button)

        layout.addSpacing(14)
        layout.addWidget(self.job_stage)
        layout.addWidget(self.job_status)
        layout.addWidget(self.progress)
        layout.addWidget(self.job_telemetry)

        layout.addWidget(
            self.cancel_button,
            alignment=Qt.AlignmentFlag.AlignLeft,
        )

        layout.addStretch(1)

        scroll = QScrollArea()
        scroll.setObjectName("mainScroll")
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        scroll.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAsNeeded
        )
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setWidget(page)

        self.setCentralWidget(scroll)

        self.setStyleSheet("""
            QMainWindow {
                background: #09111d;
            }

            QScrollArea#mainScroll {
                background: #09111d;
                border: none;
            }

            QWidget#page {
                background: #09111d;
                color: #f4f7ff;
            }

            QLabel#logo {
                font-size: 64px;
                font-weight: 800;
                color: #6f8cff;
                background: transparent;
            }

            QLabel#remux {
                font-size: 24px;
                font-weight: 700;
                letter-spacing: 7px;
                background: transparent;
            }

            QLabel#subtitle {
                font-size: 12px;
                color: #b8c2d6;
                background: transparent;
            }

            QLabel#sectionTitle {
                font-size: 13px;
                font-weight: 700;
                color: #8798b9;
                background: transparent;
            }

            QLabel#status {
                font-size: 14px;
                font-weight: 600;
                color: #70a7ff;
                background: transparent;
            }

            QFrame#dropArea {
                background: #101b2c;
                border: 2px solid #315bd8;
                border-radius: 22px;
            }

            QLabel#dropTitle {
                font-size: 22px;
                font-weight: 600;
                background: transparent;
                padding: 4px;
            }

            QLabel#muted {
                font-size: 15px;
                color: #8390a8;
                background: transparent;
            }

            QPushButton {
                padding: 12px 24px;
                font-size: 16px;
                font-weight: 600;

                background: #121d30;

                border: 2px solid #4d63ff;
                border-radius: 22px;
            }

            QPushButton:hover {
                background: #192844;
            }

            QPushButton:pressed {
                background: #0d1727;
            }

            QLabel#outputPath {
                color: #dce7fa;
                font-size: 14px;
                background: #0d1726;
                border: 1px solid #233857;
                border-radius: 12px;
                padding: 12px;
            }

            QPushButton#exportButton {
                font-size: 18px;
                font-weight: 800;
                background: #2948d8;
                border: 2px solid #7087ff;
                border-radius: 26px;
            }

            QPushButton#exportButton:hover {
                background: #3859ef;
            }

            QPushButton#exportButton:disabled {
                color: #68758d;
                background: #111b2b;
                border: 2px solid #29364b;
            }

            QLabel#jobStage {
                font-size: 17px;
                font-weight: 800;
                color: #8aa4ff;
                background: transparent;
            }

            QLabel#jobStatus {
                font-size: 14px;
                color: #c4d0e6;
                background: transparent;
            }

            QProgressBar {
                min-height: 24px;
                border: 1px solid #2d4265;
                border-radius: 12px;
                background: #0d1726;
                text-align: center;
                color: #f4f7ff;
            }

            QProgressBar::chunk {
                border-radius: 11px;
                background: #4d63ff;
            }

            QPlainTextEdit#analysis,
            QPlainTextEdit#plan {
                background: #0d1726;
                color: #dce7fa;

                border: 1px solid #233857;
                border-radius: 15px;

                padding: 16px;

                font-size: 13px;

                selection-background-color: #315bd8;
            }

            QScrollBar:vertical {
                background: #09111d;
                width: 11px;
                margin: 2px;
            }

            QScrollBar::handle:vertical {
                background: #344c72;
                border-radius: 5px;
                min-height: 36px;
            }

            QScrollBar::handle:vertical:hover {
                background: #4d63ff;
            }

            QScrollBar::add-line:vertical,
            QScrollBar::sub-line:vertical {
                height: 0px;
            }
        """)

    def cleanup_temp(self):
        if self.temp_dir:
            shutil.rmtree(self.temp_dir, ignore_errors=True)

        self.temp_dir = None
        self.rpu_path = None

    def analyze_source(self, path):
        self.stop_analysis_processes()
        self.cleanup_temp()

        self.current_source = Path(path)
        self.analysis_result = None
        self.deep_dovi = None
        self.plan_ready = False
        self.output_path = default_output_path(self.current_source)

        self.output_label.show()
        self.output_path_label.show()
        self.choose_output.show()
        self.export_button.show()
        self.output_path_label.setText(str(self.output_path))
        self.export_button.setEnabled(False)

        self.analysis_title.show()
        self.status.show()
        self.analysis.show()

        self.status.setText("Analyzing container and streams…")
        self.analysis.setPlainText("Reading source metadata…")

        process = QProcess(self)
        self.ffprobe_process = process

        process.setProgram("ffprobe")
        process.setArguments([
            "-v", "error",
            "-show_format",
            "-show_streams",
            "-of", "json",
            str(self.current_source),
        ])

        process.finished.connect(self.ffprobe_finished)
        process.errorOccurred.connect(self.process_start_error)

        process.start()

    def stop_analysis_processes(self):
        for process in (
            self.ffprobe_process,
            self.dovi_extract_process,
            self.dovi_info_process,
        ):
            if (
                process
                and process.state()
                != QProcess.ProcessState.NotRunning
            ):
                process.kill()
                process.waitForFinished(1000)

        self.ffprobe_process = None
        self.dovi_extract_process = None
        self.dovi_info_process = None

    def ffprobe_finished(self, exit_code, exit_status):
        process = self.ffprobe_process

        if process is None:
            return

        stdout = bytes(
            process.readAllStandardOutput()
        ).decode(
            "utf-8",
            errors="replace",
        )

        stderr = bytes(
            process.readAllStandardError()
        ).decode(
            "utf-8",
            errors="replace",
        )

        if exit_code != 0:
            self.status.setText("Analysis failed")
            self.analysis.setPlainText(
                stderr.strip()
                or "ffprobe returned an error."
            )
            return

        try:
            self.analysis_result = analyze_ffprobe_json(
                stdout,
                self.current_source,
            )
        except Exception as exc:
            self.status.setText("Analysis failed")

            self.analysis.setPlainText(
                f"Could not parse ffprobe output:\n\n{exc}"
            )

            return

        self.analysis.setPlainText(
            build_summary(self.analysis_result)
        )

        self.update_plan()

        video = self.analysis_result.get("video", [])

        if not video:
            self.status.setText(
                "Analysis complete — no video stream found"
            )
            return

        dovi = video[0].get("dolby_vision")

        if not dovi:
            self.status.setText(
                "Analysis complete — Dolby Vision not detected"
            )
            return

        if self.current_source.suffix.lower() != ".mkv":
            self.status.setText(
                "Dolby Vision detected — basic analysis complete"
            )
            return

        self.start_dovi_analysis()

    def start_dovi_analysis(self):
        self.temp_dir = tempfile.mkdtemp(
            prefix="dvremux-analysis-"
        )

        self.rpu_path = (
            Path(self.temp_dir) / "sample.rpu"
        )

        self.status.setText(
            "Dolby Vision detected — identifying profile structure…"
        )

        process = QProcess(self)
        self.dovi_extract_process = process

        process.setProgram("dovi_tool")

        process.setArguments([
            "extract-rpu",
            "-l", "1",
            str(self.current_source),
            "-o", str(self.rpu_path),
        ])

        process.finished.connect(
            self.dovi_extract_finished
        )

        process.errorOccurred.connect(
            self.process_start_error
        )

        process.start()

    def dovi_extract_finished(
        self,
        exit_code,
        exit_status,
    ):
        process = self.dovi_extract_process

        if process is None:
            return

        stderr = bytes(
            process.readAllStandardError()
        ).decode(
            "utf-8",
            errors="replace",
        )

        if (
            exit_code != 0
            or not self.rpu_path
            or not self.rpu_path.exists()
        ):
            self.status.setText(
                "Basic analysis complete — "
                "Dolby Vision deep analysis failed"
            )

            if stderr.strip():
                current = self.analysis.toPlainText()

                self.analysis.setPlainText(
                    current
                    + "\n\nDOLBY VISION DEEP ANALYSIS\n"
                    + stderr.strip()
                )

            self.cleanup_temp()
            return

        process = QProcess(self)
        self.dovi_info_process = process

        process.setProgram("dovi_tool")

        process.setArguments([
            "info",
            "-s",
            "-i", str(self.rpu_path),
        ])

        process.finished.connect(
            self.dovi_info_finished
        )

        process.errorOccurred.connect(
            self.process_start_error
        )

        process.start()

    def dovi_info_finished(
        self,
        exit_code,
        exit_status,
    ):
        process = self.dovi_info_process

        if process is None:
            return

        stdout = bytes(
            process.readAllStandardOutput()
        ).decode(
            "utf-8",
            errors="replace",
        )

        stderr = bytes(
            process.readAllStandardError()
        ).decode(
            "utf-8",
            errors="replace",
        )

        text = (stdout + "\n" + stderr).strip()

        if exit_code != 0:
            self.status.setText(
                "Basic analysis complete — "
                "Dolby Vision deep analysis failed"
            )

            self.cleanup_temp()
            return

        dovi = parse_dovi_summary(text)
        self.deep_dovi = dovi
        self.update_plan()

        current = self.analysis.toPlainText()

        lines = [
            "",
            "DOLBY VISION DEEP ANALYSIS",
        ]

        if dovi["profile"] is not None:
            profile_text = (
                f'Profile {dovi["profile"]}'
            )

            if dovi["type"]:
                profile_text += (
                    f' ({dovi["type"]})'
                )

            lines.append(profile_text)

        if dovi["dm_version"]:
            lines.append(
                f'Dynamic metadata: '
                f'{dovi["dm_version"]}'
            )

        if not dovi["profile"]:
            lines.append("RPU detected")

        self.analysis.setPlainText(
            current
            + "\n"
            + "\n".join(lines)
        )

        if (
            dovi["profile"] == 7
            and dovi["type"]
        ):
            self.status.setText(
                f'Dolby Vision Profile 7 '
                f'{dovi["type"]} detected'
            )

        elif dovi["profile"]:
            self.status.setText(
                f'Dolby Vision Profile '
                f'{dovi["profile"]} detected'
            )

        else:
            self.status.setText(
                "Dolby Vision RPU detected"
            )

        self.cleanup_temp()

    def update_plan(self):
        if not self.analysis_result:
            return

        result = build_plan(
            self.analysis_result,
            self.deep_dovi,
        )

        self.plan_title.show()
        self.plan.show()
        self.plan.setPlainText(result["text"])

        self.plan_ready = result["ready"]
        self.refresh_export_state()

    def update_plan(self):
        if not self.analysis_result:
            return

        result = build_plan(
            self.analysis_result,
            self.deep_dovi,
        )

        self.plan_title.show()
        self.plan.show()
        self.plan.setPlainText(result["text"])

        self.plan_ready = result["ready"]
        self.refresh_export_state()

    def choose_destination(self):
        if not self.current_source:
            return

        suggested = str(
            self.output_path
            or default_output_path(self.current_source)
        )

        path, _ = QFileDialog.getSaveFileName(
            self,
            "Save DV Remux MP4",
            suggested,
            "MP4 Video (*.mp4)",
        )

        if not path:
            return

        candidate = Path(path)

        if candidate.suffix.lower() != ".mp4":
            candidate = candidate.with_suffix(".mp4")

        self.output_path = candidate
        self.output_path_label.setText(str(candidate))

        self.refresh_export_state()

    def refresh_export_state(self):
        if (
            not self.current_source
            or not self.output_path
            or not self.plan_ready
        ):
            self.export_button.setEnabled(False)
            return

        safe, reason = path_is_safe(
            self.current_source,
            self.output_path,
        )

        self.export_button.setEnabled(safe)

        if safe:
            self.export_button.setToolTip(
                "Ready to create the new MP4."
            )
        else:
            self.export_button.setToolTip(reason)

    def _telemetry_size(self, value):
        try:
            value = float(value)
        except Exception:
            return "0 B"

        for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
            if value < 1024.0 or unit == "TiB":
                return f"{value:.1f} {unit}"
            value /= 1024.0

        return "0 B"

    def _telemetry_elapsed(self, seconds):
        seconds = max(0, int(seconds))

        hours, rem = divmod(seconds, 3600)
        minutes, seconds = divmod(rem, 60)

        if hours:
            return f"{hours:d}:{minutes:02d}:{seconds:02d}"

        return f"{minutes:d}:{seconds:02d}"

    def _update_job_telemetry(self):
        job = getattr(self, "job", None)

        if job is None:
            return

        started = getattr(
            self,
            "_job_started_at",
            None,
        )

        elapsed = (
            time.monotonic() - started
            if started is not None
            else 0
        )

        video_size = 0
        audio_size = 0
        output_size = 0
        temp_size = 0

        current_file = "—"
        newest_mtime = -1

        prepared_video = getattr(
            job,
            "prepared_video",
            None,
        )

        prepared_audio = getattr(
            job,
            "prepared_audio",
            None,
        )

        partial_output = getattr(
            job,
            "partial_output",
            None,
        )

        try:
            if (
                prepared_video is not None
                and prepared_video.is_file()
            ):
                video_size = (
                    prepared_video.stat().st_size
                )
        except OSError:
            pass

        try:
            if (
                prepared_audio is not None
                and prepared_audio.is_file()
            ):
                audio_size = (
                    prepared_audio.stat().st_size
                )
        except OSError:
            pass

        try:
            if (
                partial_output is not None
                and partial_output.is_file()
            ):
                output_size = (
                    partial_output.stat().st_size
                )
        except OSError:
            pass

        workspace = getattr(
            job,
            "workspace",
            None,
        )

        if workspace is not None:
            try:
                if workspace.exists():
                    for item in workspace.rglob("*"):
                        try:
                            if not item.is_file():
                                continue

                            stat = item.stat()
                            temp_size += stat.st_size

                            if stat.st_mtime > newest_mtime:
                                newest_mtime = stat.st_mtime
                                current_file = item.name

                        except OSError:
                            continue
            except OSError:
                pass

        try:
            if (
                partial_output is not None
                and partial_output.is_file()
            ):
                stat = partial_output.stat()

                if stat.st_mtime > newest_mtime:
                    current_file = partial_output.name
        except OSError:
            pass

        overall = self.progress.value()

        self.job_telemetry.setText(
            "LIVE TELEMETRY\n"
            f"Overall      {overall}%\n"
            f"Video        {self._telemetry_size(video_size)}\n"
            f"Audio        {self._telemetry_size(audio_size)}\n"
            f"MP4 Output   {self._telemetry_size(output_size)}\n"
            f"Temp Total   {self._telemetry_size(temp_size)}\n"
            f"Elapsed      {self._telemetry_elapsed(elapsed)}\n"
            f"Writing      {current_file}"
        )

    def start_job(self):
        if (
            not self.current_source
            or not self.output_path
            or not self.plan_ready
        ):
            return

        safe, reason = path_is_safe(
            self.current_source,
            self.output_path,
        )

        if not safe:
            self.job_status.setText(reason)
            self.job_status.show()
            return

        if self.job is not None:
            return

        self.export_button.setEnabled(False)
        self.choose_output.setEnabled(False)
        self.drop_area.setEnabled(False)

        self.job_stage.setText("Preflight")
        self.job_status.setText("Starting DV Remux job…")

        self.job_stage.show()
        self.job_status.show()

        self.progress.setValue(0)
        self.progress.show()

        self._job_started_at = time.monotonic()
        self.job_telemetry.setText(
            "LIVE TELEMETRY • starting…"
        )
        self.job_telemetry.show()

        self.cancel_button.setEnabled(True)
        self.cancel_button.show()

        video = self.analysis_result["video"][0]
        basic_dovi = video.get("dolby_vision") or {}

        dv_profile = basic_dovi.get("profile")

        if self.deep_dovi:
            dv_profile = (
                self.deep_dovi.get("profile")
                or dv_profile
            )

        compatibility_id = basic_dovi.get(
            "compatibility_id"
        )

        self.job = RemuxJob(
            self.current_source,
            self.output_path,
            dv_profile=dv_profile,
            compatibility_id=compatibility_id,
            audio_streams=self.analysis_result.get(
                "audio",
                [],
            ),
            video_fps=video.get(
                "fps_raw",
                "24000/1001",
            ),
        )

        self.job.stage_changed.connect(
            self.job_stage.setText
        )

        self.job.progress_changed.connect(
            self.progress.setValue
        )

        self.job.status_changed.connect(
            self.job_status.setText
        )

        self.job.preflight_complete.connect(
            self.preflight_complete
        )

        self.job.failed.connect(
            self.job_failed
        )

        self.job.cancelled.connect(
            self.job_cancelled
        )

        self.job.video_prepared.connect(
            self.video_prepare_complete
        )

        self.job.audio_prepared.connect(
            self.audio_prepare_complete
        )

        self.job.completed.connect(
            self.job_completed
        )

        self.job.run_preflight()

    def preflight_complete(self):
        if self.job is None:
            return

        if self.job.cancel_requested:
            return

        self.job.start_video_prepare()

    def video_prepare_complete(self, prepared_path):
        if self.job is None:
            return

        if self.job.cancel_requested:
            return

        self.progress.setValue(48)
        self.job_stage.setText("Prepare Audio")

        self.job_status.setText(
            "Video/Dolby Vision preparation passed verification. "
            "Preparing the best Dolby audio track…"
        )

        self.job.start_audio_prepare()

    def audio_prepare_complete(
        self,
        prepared_path,
        codec,
    ):
        if self.job is None:
            return

        if self.job.cancel_requested:
            return

        self.progress.setValue(60)
        self.job_stage.setText("Mux")

        self.job_status.setText(
            f"Video and {codec.upper()} audio are verified. "
            "Building MP4…"
        )

        self.job.start_mux()

    def job_completed(self, output_path):
        if self.job is None:
            return

        self.progress.setValue(100)
        self.job_stage.setText("READY")

        self.job_status.setText(
            "DV REMUX READY\n\n"
            f"{output_path}\n\n"
            "Dolby Vision Profile 8.1 verified. "
            "Original source untouched."
        )

        self.cancel_button.hide()

        self.drop_area.setEnabled(True)
        self.choose_output.setEnabled(True)

        self.job.cleanup()
        self.job = None

        self.refresh_export_state()

    def cancel_job(self):
        if self.job is None:
            return

        self.cancel_button.setEnabled(False)
        self.job_status.setText("Cancelling…")

        self.job.request_cancel()

    def job_failed(self, message):
        if self.job is None:
            return

        self.job_stage.setText(
            f"FAILED • {self.progress.value()}%"
        )
        self.job_status.setText(message)

        self.finish_job_ui()

    def job_cancelled(self):
        if self.job is None:
            return

        self.job_stage.setText("Cancelled")

        self.job_status.setText(
            "Job cancelled. Source file was not modified."
        )

        self.finish_job_ui()

    def finish_job_ui(self):
        if self.job is not None:
            self.job.cleanup()
            self.job = None

        self.cancel_button.hide()

        self.drop_area.setEnabled(True)
        self.choose_output.setEnabled(True)

        self.refresh_export_state()

    def process_start_error(self, error):
        self.status.setText(
            f"Analysis tool failed to start: {error}"
        )

    def closeEvent(self, event):
        self.stop_analysis_processes()
        self.cleanup_temp()

        if self.job is not None:
            self.job.request_cancel()
            self.job.cleanup()
            self.job = None

        super().closeEvent(event)


def main():
    app = QApplication(sys.argv)

    app.setApplicationName("DV Remux")
    app.setWindowIcon(QIcon(str(ICON_PATH)))

    window = DVRemuxWindow()
    window.show()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()

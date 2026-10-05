import re
import shutil
import tempfile
from pathlib import Path

from PySide6.QtCore import QObject, QProcess, Signal


def human_bytes(value):
    value = float(value)

    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            return f"{value:.1f} {unit}"
        value /= 1024

    return "Unknown"


class RemuxJob(QObject):
    stage_changed = Signal(str)
    progress_changed = Signal(int)
    status_changed = Signal(str)

    failed = Signal(str)
    cancelled = Signal()
    preflight_complete = Signal()
    video_prepared = Signal(str)
    audio_prepared = Signal(str, str)
    completed = Signal(str)

    def __init__(
        self,
        source,
        output,
        dv_profile=None,
        compatibility_id=None,
        audio_streams=None,
        video_fps=None,
    ):
        super().__init__()

        self.source = Path(source)
        self.output = Path(output)

        self.dv_profile = dv_profile
        self.compatibility_id = compatibility_id
        self.audio_streams = list(audio_streams or [])
        self.video_fps = video_fps or "24000/1001"

        self.workspace = None
        self.partial_output = None
        self.expected_audio_codec = None
        self.selected_audio = None
        self.prepared_audio = None
        self.prepared_video = None
        self.verify_rpu = None

        self.cancel_requested = False
        self.cancel_emitted = False
        self.failed_state = False

        self.active_processes = []

        self.ffmpeg_process = None
        self.dovi_process = None
        self.verify_extract_process = None
        self.verify_info_process = None
        self.audio_process = None
        self.audio_verify_process = None
        self.mux_process = None
        self.final_verify_process = None

    # --------------------------------------------------------
    # State / process helpers
    # --------------------------------------------------------

    def _is_running(self, process):
        return (
            process is not None
            and process.state()
            != QProcess.ProcessState.NotRunning
        )

    def request_cancel(self):
        if self.cancel_requested:
            return

        self.cancel_requested = True
        self.status_changed.emit("Cancelling…")

        running = False

        for process in list(self.active_processes):
            try:
                if self._is_running(process):
                    running = True
                    process.kill()
            except RuntimeError:
                pass

        if not running:
            self._emit_cancelled()

    def _emit_cancelled(self):
        if self.cancel_emitted:
            return

        self.cancel_emitted = True
        self.cancelled.emit()

    def _release_processes(self):
        for process in list(self.active_processes):
            try:
                process.deleteLater()
            except RuntimeError:
                pass

        self.active_processes.clear()

    def _kill_processes(self):
        for process in list(self.active_processes):
            try:
                if self._is_running(process):
                    process.kill()
                    process.waitForFinished(2000)
            except RuntimeError:
                pass

        self._release_processes()

    def _process_error(self, error):
        if self.cancel_requested or self.failed_state:
            return

        process = self.sender()

        if process is not None:
            message = process.errorString()
        else:
            message = "External process failed to start."

        self._fail(message)

    def _fail(self, message):
        if self.failed_state:
            return

        if self.cancel_requested:
            self._kill_processes()
            self._emit_cancelled()
            return

        self.failed_state = True
        self._kill_processes()
        self.failed.emit(message)

    # --------------------------------------------------------
    # Workspace
    # --------------------------------------------------------

    def create_workspace(self):
        if self.workspace is not None:
            return self.workspace

        root = (
            Path.home()
            / ".cache"
            / "dvremux"
            / "jobs"
        )

        root.mkdir(
            parents=True,
            exist_ok=True,
        )

        source_size = self.source.stat().st_size
        free_space = shutil.disk_usage(root).free

        # Conservative guard. The prepared HEVC is normally
        # smaller than the complete source container.
        required = int(source_size * 1.05)

        if free_space < required:
            raise RuntimeError(
                "Not enough temporary workspace.\n\n"
                f"Needed: about {human_bytes(required)}\n"
                f"Available: {human_bytes(free_space)}\n\n"
                f"Workspace: {root}"
            )

        self.workspace = Path(
            tempfile.mkdtemp(
                prefix="job-",
                dir=root,
            )
        )

        self.prepared_video = (
            self.workspace / "video-p81.hevc"
        )

        self.verify_rpu = (
            self.workspace / "verify-rpu.bin"
        )

        return self.workspace

    # --------------------------------------------------------
    # Preflight
    # --------------------------------------------------------

    def run_preflight(self):
        try:
            self.stage_changed.emit("Preflight")
            self.progress_changed.emit(5)

            self.status_changed.emit(
                "Checking source and destination…"
            )

            if not self.source.is_file():
                raise RuntimeError(
                    "Source file no longer exists."
                )

            if self.source.resolve() == self.output.resolve():
                raise RuntimeError(
                    "Output cannot overwrite the source."
                )

            if self.output.exists():
                raise RuntimeError(
                    "Output already exists. "
                    "DV Remux will never overwrite it."
                )

            if self.output.suffix.lower() != ".mp4":
                raise RuntimeError(
                    "DV Remux output must be an MP4."
                )

            if not self.output.parent.is_dir():
                raise RuntimeError(
                    "Destination folder does not exist."
                )

            if self.dv_profile not in (7, 8):
                raise RuntimeError(
                    "Unsupported Dolby Vision source profile."
                )

            if (
                self.dv_profile == 8
                and self.compatibility_id != 1
            ):
                raise RuntimeError(
                    "Profile 8 source is not confirmed "
                    "as Profile 8.1."
                )

            self.progress_changed.emit(10)

            self.status_changed.emit(
                "Checking safe temporary workspace…"
            )

            self.create_workspace()

            if self.cancel_requested:
                self._emit_cancelled()
                return

            self.progress_changed.emit(15)

            self.stage_changed.emit(
                "Prepare Video / Dolby Vision"
            )

            self.status_changed.emit(
                "Preflight passed. "
                "Original source remains untouched."
            )

            self.preflight_complete.emit()

        except Exception as exc:
            self._fail(str(exc))

    # --------------------------------------------------------
    # Video preparation
    # --------------------------------------------------------

    def start_video_prepare(self):
        if self.cancel_requested:
            self._emit_cancelled()
            return

        if self.workspace is None:
            self._fail(
                "Temporary workspace is missing."
            )
            return

        if self.dv_profile == 7:
            self._start_profile7()
            return

        if (
            self.dv_profile == 8
            and self.compatibility_id == 1
        ):
            self._start_profile81()
            return

        self._fail(
            "No valid video preparation path."
        )

    # --------------------------------------------------------
    # P7 -> P8.1-compatible RPU / BL+RPU
    # --------------------------------------------------------

    def _start_profile7(self):
        self.progress_changed.emit(18)

        self.status_changed.emit(
            "Stream-copying HEVC and converting "
            "Dolby Vision Profile 7 → Profile 8.1. "
            "No video re-encode."
        )

        ffmpeg = QProcess(self)
        dovi = QProcess(self)

        self.ffmpeg_process = ffmpeg
        self.dovi_process = dovi

        self.active_processes = [
            ffmpeg,
            dovi,
        ]

        ffmpeg.setProgram("ffmpeg")

        ffmpeg.setArguments([
            "-nostdin",
            "-hide_banner",
            "-loglevel", "error",
            "-i", str(self.source),
            "-map", "0:v:0",
            "-an",
            "-sn",
            "-dn",
            "-c:v", "copy",
            "-bsf:v", "hevc_mp4toannexb",
            "-f", "hevc",
            "-",
        ])

        dovi.setProgram("dovi_tool")

        dovi.setArguments([
            "-m", "2",
            "convert",
            "--discard",
            "-",
            "-o", str(self.prepared_video),
        ])

        # Direct stream:
        # ffmpeg -> dovi_tool -> prepared HEVC
        ffmpeg.setStandardOutputProcess(dovi)

        ffmpeg.errorOccurred.connect(
            self._process_error
        )

        dovi.errorOccurred.connect(
            self._process_error
        )

        ffmpeg.finished.connect(
            self._profile7_finished
        )

        dovi.finished.connect(
            self._profile7_finished
        )

        # Start consumer first.
        dovi.start()
        ffmpeg.start()

    def _profile7_finished(self, *args):
        if self.failed_state:
            return

        if any(
            self._is_running(process)
            for process in self.active_processes
        ):
            return

        if self.cancel_requested:
            self._release_processes()
            self._emit_cancelled()
            return

        ffmpeg_code = self.ffmpeg_process.exitCode()
        dovi_code = self.dovi_process.exitCode()

        ffmpeg_error = bytes(
            self.ffmpeg_process.readAllStandardError()
        ).decode(
            "utf-8",
            errors="replace",
        ).strip()

        dovi_error = bytes(
            self.dovi_process.readAllStandardError()
        ).decode(
            "utf-8",
            errors="replace",
        ).strip()

        self._release_processes()

        if ffmpeg_code != 0:
            self._fail(
                "FFmpeg could not stream-copy the HEVC video."
                + (
                    f"\n\n{ffmpeg_error}"
                    if ffmpeg_error
                    else ""
                )
            )
            return

        if dovi_code != 0:
            self._fail(
                "Dolby Vision Profile 7 → 8.1 "
                "conversion failed."
                + (
                    f"\n\n{dovi_error}"
                    if dovi_error
                    else ""
                )
            )
            return

        self._prepared_file_written()

    # --------------------------------------------------------
    # Existing P8.1
    # --------------------------------------------------------

    def _start_profile81(self):
        self.progress_changed.emit(18)

        self.status_changed.emit(
            "Source is already Dolby Vision Profile 8.1. "
            "Stream-copying HEVC unchanged. "
            "No Dolby Vision conversion and no video re-encode."
        )

        process = QProcess(self)

        self.ffmpeg_process = process
        self.active_processes = [process]

        process.setProgram("ffmpeg")

        process.setArguments([
            "-nostdin",
            "-hide_banner",
            "-loglevel", "error",
            "-n",
            "-i", str(self.source),
            "-map", "0:v:0",
            "-an",
            "-sn",
            "-dn",
            "-c:v", "copy",
            "-bsf:v", "hevc_mp4toannexb",
            "-f", "hevc",
            str(self.prepared_video),
        ])

        process.errorOccurred.connect(
            self._process_error
        )

        process.finished.connect(
            self._profile81_finished
        )

        process.start()

    def _profile81_finished(
        self,
        exit_code,
        exit_status,
    ):
        if self.failed_state:
            return

        if self.cancel_requested:
            self._release_processes()
            self._emit_cancelled()
            return

        process = self.ffmpeg_process

        stderr = bytes(
            process.readAllStandardError()
        ).decode(
            "utf-8",
            errors="replace",
        ).strip()

        self._release_processes()

        if exit_code != 0:
            self._fail(
                "Could not stream-copy the Profile 8.1 video."
                + (
                    f"\n\n{stderr}"
                    if stderr
                    else ""
                )
            )
            return

        self._prepared_file_written()

    # --------------------------------------------------------
    # Intermediate verification
    # --------------------------------------------------------

    def _prepared_file_written(self):
        if (
            self.prepared_video is None
            or not self.prepared_video.is_file()
            or self.prepared_video.stat().st_size == 0
        ):
            self._fail(
                "Prepared HEVC was not created correctly."
            )
            return

        self.progress_changed.emit(42)

        self.status_changed.emit(
            "Video prepared. Verifying Dolby Vision RPU…"
        )

        self._start_rpu_extract()

    def _start_rpu_extract(self):
        process = QProcess(self)

        self.verify_extract_process = process
        self.active_processes = [process]

        process.setProgram("dovi_tool")

        process.setArguments([
            "extract-rpu",
            str(self.prepared_video),
            "-o", str(self.verify_rpu),
        ])

        process.errorOccurred.connect(
            self._process_error
        )

        process.finished.connect(
            self._rpu_extract_finished
        )

        process.start()

    def _rpu_extract_finished(
        self,
        exit_code,
        exit_status,
    ):
        if self.failed_state:
            return

        if self.cancel_requested:
            self._release_processes()
            self._emit_cancelled()
            return

        process = self.verify_extract_process

        stderr = bytes(
            process.readAllStandardError()
        ).decode(
            "utf-8",
            errors="replace",
        ).strip()

        self._release_processes()

        if (
            exit_code != 0
            or not self.verify_rpu.is_file()
            or self.verify_rpu.stat().st_size == 0
        ):
            self._fail(
                "Could not extract Dolby Vision RPU "
                "from the prepared HEVC."
                + (
                    f"\n\n{stderr}"
                    if stderr
                    else ""
                )
            )
            return

        self.progress_changed.emit(45)

        self._start_rpu_info()

    def _start_rpu_info(self):
        process = QProcess(self)

        self.verify_info_process = process
        self.active_processes = [process]

        process.setProgram("dovi_tool")

        process.setArguments([
            "info",
            "-s",
            "-i", str(self.verify_rpu),
        ])

        process.errorOccurred.connect(
            self._process_error
        )

        process.finished.connect(
            self._rpu_info_finished
        )

        process.start()

    def _rpu_info_finished(
        self,
        exit_code,
        exit_status,
    ):
        if self.failed_state:
            return

        if self.cancel_requested:
            self._release_processes()
            self._emit_cancelled()
            return

        process = self.verify_info_process

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

        self._release_processes()

        if exit_code != 0:
            self._fail(
                "Dolby Vision RPU verification failed."
                + (
                    f"\n\n{stderr.strip()}"
                    if stderr.strip()
                    else ""
                )
            )
            return

        summary = (
            stdout
            + "\n"
            + stderr
        )

        # Current dovi_tool RPU summary reports the major profile
        # as "Profile: 8". Compatibility ID 1 is container
        # signalling and will be verified on the final MP4.
        if not re.search(
            r"Profile:\s*8(?:\.1)?(?:\s|\(|$)",
            summary,
            re.IGNORECASE,
        ):
            self._fail(
                "Prepared video does not contain "
                "a Profile 8 Dolby Vision RPU.\n\n"
                + summary.strip()
            )
            return

        self.progress_changed.emit(48)

        self.stage_changed.emit(
            "Prepare Audio"
        )

        self.status_changed.emit(
            "Video prepared successfully: HEVC stream copy, "
            "Dolby Vision Profile 8 RPU confirmed, no video re-encode. "
            "Final MP4 Profile 8.1 / Compatibility ID 1 "
            "will be verified after mux."
        )

        self.video_prepared.emit(
            str(self.prepared_video)
        )

    # --------------------------------------------------------
    # Prepare Audio
    # --------------------------------------------------------

    def _select_audio_stream(self):
        if not self.audio_streams:
            return None

        def priority(stream):
            codec = (
                stream.get("codec") or ""
            ).lower()

            profile = (
                stream.get("profile") or ""
            ).lower()

            title = (
                stream.get("title") or ""
            ).lower()

            text = f"{profile} {title}"
            atmos = "atmos" in text

            if codec == "truehd" and atmos:
                return 0

            if codec == "truehd":
                return 1

            if codec == "eac3" and atmos:
                return 2

            if codec == "eac3":
                return 3

            if codec == "ac3":
                return 4

            return 999

        candidates = sorted(
            self.audio_streams,
            key=priority,
        )

        if not candidates:
            return None

        if priority(candidates[0]) == 999:
            return None

        return candidates[0]

    def start_audio_prepare(self):
        if self.cancel_requested:
            self._emit_cancelled()
            return

        if self.workspace is None:
            self._fail(
                "Temporary workspace is missing."
            )
            return

        audio = self._select_audio_stream()

        if audio is None:
            self._fail(
                "No supported Dolby audio track was found.\n\n"
                "Current supported preservation paths are "
                "TrueHD, E-AC-3, and AC-3."
            )
            return

        self.selected_audio = audio

        codec = (
            audio.get("codec") or ""
        ).lower()

        formats = {
            "truehd": (
                "mp4",
                "audio.mp4",
            ),
            "eac3": (
                "eac3",
                "audio.eac3",
            ),
            "ac3": (
                "ac3",
                "audio.ac3",
            ),
        }

        if codec not in formats:
            self._fail(
                f"Unsupported audio codec: {codec}"
            )
            return

        mux_format, filename = formats[codec]

        self.prepared_audio = (
            self.workspace / filename
        )

        stream_index = audio.get("index")

        if stream_index is None:
            self._fail(
                "Selected audio track has no stream index."
            )
            return

        profile = audio.get("profile") or ""
        title = audio.get("title") or ""
        description = " ".join(
            item
            for item in (
                codec.upper(),
                profile,
                title,
            )
            if item
        )

        self.stage_changed.emit(
            "Prepare Audio"
        )

        self.progress_changed.emit(50)

        self.status_changed.emit(
            f"Preserving {description}. "
            "Audio is being stream-copied with no transcoding."
        )

        process = QProcess(self)

        self.audio_process = process
        self.active_processes = [process]

        process.setProgram("ffmpeg")

        audio_args = [
            "-nostdin",
            "-hide_banner",
            "-loglevel", "error",
            "-n",
            "-i", str(self.source),
            "-map", f"0:{stream_index}",
            "-map_chapters", "-1",
            "-map_metadata", "-1",
            "-vn",
            "-sn",
            "-dn",
            "-c:a", "copy",
        ]

        if codec == "truehd":
            # FFmpeg supports TrueHD/Atmos in MP4 behind the
            # experimental flag. This keeps the encoded audio
            # bitstream intact while preserving usable timing.
            audio_args += [
                "-strict", "-2",
                "-f", "mp4",
            ]
        else:
            audio_args += [
                "-f", mux_format,
            ]

        audio_args.append(
            str(self.prepared_audio)
        )

        process.setArguments(audio_args)

        process.errorOccurred.connect(
            self._process_error
        )

        process.finished.connect(
            self._audio_prepare_finished
        )

        process.start()

    def _audio_prepare_finished(
        self,
        exit_code,
        exit_status,
    ):
        if self.failed_state:
            return

        if self.cancel_requested:
            self._release_processes()
            self._emit_cancelled()
            return

        process = self.audio_process

        stderr = bytes(
            process.readAllStandardError()
        ).decode(
            "utf-8",
            errors="replace",
        ).strip()

        self._release_processes()

        if exit_code != 0:
            self._fail(
                "Audio stream-copy failed."
                + (
                    f"\n\n{stderr}"
                    if stderr
                    else ""
                )
            )
            return

        if (
            self.prepared_audio is None
            or not self.prepared_audio.is_file()
            or self.prepared_audio.stat().st_size == 0
        ):
            self._fail(
                "Prepared audio file was not created correctly."
            )
            return

        self.progress_changed.emit(56)

        self.status_changed.emit(
            "Audio stream copied. Verifying codec…"
        )

        self._start_audio_verify()

    def _start_audio_verify(self):
        process = QProcess(self)

        self.audio_verify_process = process
        self.active_processes = [process]

        process.setProgram("ffprobe")

        process.setArguments([
            "-v", "error",
            "-select_streams", "a:0",
            "-show_entries",
            (
                "stream=codec_name,profile,"
                "channels,channel_layout"
            ),
            "-of", "json",
            str(self.prepared_audio),
        ])

        process.errorOccurred.connect(
            self._process_error
        )

        process.finished.connect(
            self._audio_verify_finished
        )

        process.start()

    def _audio_verify_finished(
        self,
        exit_code,
        exit_status,
    ):
        if self.failed_state:
            return

        if self.cancel_requested:
            self._release_processes()
            self._emit_cancelled()
            return

        process = self.audio_verify_process

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
        ).strip()

        self._release_processes()

        if exit_code != 0:
            self._fail(
                "Prepared audio verification failed."
                + (
                    f"\n\n{stderr}"
                    if stderr
                    else ""
                )
            )
            return

        try:
            import json

            data = json.loads(stdout)

        except Exception as exc:
            self._fail(
                "Could not parse prepared audio verification.\n\n"
                f"{exc}"
            )
            return

        streams = data.get("streams", [])

        if not streams:
            self._fail(
                "Prepared audio contains no audio stream."
            )
            return

        detected_codec = (
            streams[0].get("codec_name") or ""
        ).lower()

        expected_codec = (
            self.selected_audio.get("codec") or ""
        ).lower()

        if detected_codec != expected_codec:
            self._fail(
                "Prepared audio codec does not match the source.\n\n"
                f"Source: {expected_codec or 'unknown'}\n"
                f"Prepared: {detected_codec or 'unknown'}"
            )
            return

        profile = (
            streams[0].get("profile") or ""
        )

        channels = streams[0].get("channels")
        layout = (
            streams[0].get("channel_layout") or ""
        )

        details = detected_codec.upper()

        if profile:
            details += f" • {profile}"

        if layout:
            details += f" • {layout}"
        elif channels:
            details += f" • {channels} channels"

        self.progress_changed.emit(60)

        self.stage_changed.emit("Mux")

        self.status_changed.emit(
            f"Audio verified: {details}. "
            "Original Dolby bitstream preserved with no audio transcode. "
            "Ready for MP4 mux."
        )

        self.audio_prepared.emit(
            str(self.prepared_audio),
            detected_codec,
        )

    # --------------------------------------------------------
    # MP4 Mux
    # --------------------------------------------------------

    def start_mux(self):
        if self.cancel_requested:
            self._emit_cancelled()
            return

        if (
            self.prepared_video is None
            or not self.prepared_video.is_file()
        ):
            self._fail(
                "Prepared video is missing."
            )
            return

        if (
            self.prepared_audio is None
            or not self.prepared_audio.is_file()
        ):
            self._fail(
                "Prepared audio is missing."
            )
            return

        partial_name = (
            f".{self.output.stem}.dvremux.partial.mp4"
        )

        self.partial_output = (
            self.output.parent / partial_name
        )

        if self.partial_output.exists():
            self._fail(
                "A DV Remux partial output already exists:\n\n"
                f"{self.partial_output}"
            )
            return

        self.expected_audio_codec = (
            self.selected_audio.get("codec") or ""
        ).lower()

        language = (
            self.selected_audio.get("language")
            or "und"
        )

        self.stage_changed.emit("Mux")
        self.progress_changed.emit(64)

        self.status_changed.emit(
            "Muxing Dolby Vision Profile 8.1 MP4. "
            "Video and audio remain bit-for-bit encoded streams; "
            "no transcoding."
        )

        process = QProcess(self)

        self.mux_process = process
        self.active_processes = [process]

        process.setProgram("MP4Box")

        video_arg = (
            f"{self.prepared_video}"
            f":dvp=8.hdr10"
            f":fps={self.video_fps}"
            f":name=Dolby Vision"
        )

        audio_arg = (
            f"{self.prepared_audio}"
            f":lang={language}"
            f":name=Dolby Audio"
        )

        process.setArguments([
            "-tmp", str(self.workspace),
            "-add", video_arg,
            "-add", audio_arg,
            "-new",
            str(self.partial_output),
        ])

        process.errorOccurred.connect(
            self._process_error
        )

        process.finished.connect(
            self._mux_finished
        )

        process.start()

    def _mux_finished(
        self,
        exit_code,
        exit_status,
    ):
        if self.failed_state:
            return

        process = self.mux_process

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

        self._release_processes()

        if self.cancel_requested:
            self._preserve_partial()
            self._emit_cancelled()
            return

        if exit_code != 0:
            self._preserve_partial()

            detail = (
                stderr.strip()
                or stdout.strip()
            )

            self._fail(
                "MP4 mux failed."
                + (
                    f"\n\n{detail}"
                    if detail
                    else ""
                )
            )
            return

        if (
            self.partial_output is None
            or not self.partial_output.is_file()
            or self.partial_output.stat().st_size == 0
        ):
            self._fail(
                "MP4Box completed but no valid output "
                "file was created."
            )
            return

        self.progress_changed.emit(90)
        self.stage_changed.emit("Verify")

        self.status_changed.emit(
            "MP4 created. Verifying final Dolby Vision "
            "and audio properties…"
        )

        self._start_final_verify()

    # --------------------------------------------------------
    # Final verification
    # --------------------------------------------------------

    def _start_final_verify(self):
        process = QProcess(self)

        self.final_verify_process = process
        self.active_processes = [process]

        process.setProgram("ffprobe")

        process.setArguments([
            "-v", "error",
            "-show_streams",
            "-show_format",
            "-of", "json",
            str(self.partial_output),
        ])

        process.errorOccurred.connect(
            self._process_error
        )

        process.finished.connect(
            self._final_verify_finished
        )

        process.start()

    def _final_verify_finished(
        self,
        exit_code,
        exit_status,
    ):
        if self.failed_state:
            return

        process = self.final_verify_process

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
        ).strip()

        self._release_processes()

        if self.cancel_requested:
            self._preserve_partial()
            self._emit_cancelled()
            return

        if exit_code != 0:
            self._preserve_partial()

            self._fail(
                "Final MP4 verification failed."
                + (
                    f"\n\n{stderr}"
                    if stderr
                    else ""
                )
            )
            return

        try:
            import json

            data = json.loads(stdout)

        except Exception as exc:
            self._preserve_partial()

            self._fail(
                "Could not parse final verification data.\n\n"
                f"{exc}"
            )
            return

        streams = data.get("streams", [])

        video = next(
            (
                stream
                for stream in streams
                if stream.get("codec_type") == "video"
            ),
            None,
        )

        audio = next(
            (
                stream
                for stream in streams
                if stream.get("codec_type") == "audio"
            ),
            None,
        )

        if video is None:
            self._preserve_partial()
            self._fail(
                "Final MP4 contains no video stream."
            )
            return

        if video.get("codec_name") != "hevc":
            self._preserve_partial()
            self._fail(
                "Final MP4 video is not HEVC."
            )
            return

        dovi = None

        for side_data in video.get(
            "side_data_list",
            [],
        ):
            if (
                side_data.get("side_data_type")
                == "DOVI configuration record"
            ):
                dovi = side_data
                break

        if dovi is None:
            self._preserve_partial()

            self._fail(
                "Final MP4 does not expose "
                "Dolby Vision configuration."
            )
            return

        try:
            profile = int(
                dovi.get("dv_profile", -1)
            )

            compat = int(
                dovi.get(
                    "dv_bl_signal_compatibility_id",
                    -1,
                )
            )

            rpu = int(
                dovi.get(
                    "rpu_present_flag",
                    0,
                )
            )

            el = int(
                dovi.get(
                    "el_present_flag",
                    -1,
                )
            )

            bl = int(
                dovi.get(
                    "bl_present_flag",
                    0,
                )
            )

        except (TypeError, ValueError):
            self._preserve_partial()

            self._fail(
                "Invalid Dolby Vision metadata "
                "in final MP4."
            )
            return

        if not (
            profile == 8
            and compat == 1
            and rpu == 1
            and el == 0
            and bl == 1
        ):
            self._preserve_partial()

            self._fail(
                "Final MP4 failed Dolby Vision "
                "Profile 8.1 verification.\n\n"
                f"Profile: {profile}\n"
                f"Compatibility ID: {compat}\n"
                f"BL: {bl}\n"
                f"EL: {el}\n"
                f"RPU: {rpu}"
            )
            return

        if audio is None:
            self._preserve_partial()

            self._fail(
                "Final MP4 contains no audio stream."
            )
            return

        final_audio_codec = (
            audio.get("codec_name") or ""
        ).lower()

        if (
            self.expected_audio_codec
            and final_audio_codec
            != self.expected_audio_codec
        ):
            self._preserve_partial()

            self._fail(
                "Final MP4 audio codec changed unexpectedly.\n\n"
                f"Expected: {self.expected_audio_codec}\n"
                f"Found: {final_audio_codec}"
            )
            return

        self.progress_changed.emit(96)
        self.stage_changed.emit("Finalize")

        self.status_changed.emit(
            "Verification passed. Finalizing output…"
        )

        self._finalize_output()

    # --------------------------------------------------------
    # Atomic finalize
    # --------------------------------------------------------

    def _finalize_output(self):
        if self.output.exists():
            self._preserve_partial()

            self._fail(
                "Final destination appeared during the job. "
                "DV Remux refused to overwrite it."
            )
            return

        try:
            self.partial_output.replace(
                self.output
            )

        except Exception as exc:
            self._preserve_partial()

            self._fail(
                "Could not finalize output.\n\n"
                f"{exc}"
            )
            return

        self.partial_output = None

        self.progress_changed.emit(100)
        self.stage_changed.emit("READY")

        audio_name = (
            self.expected_audio_codec.upper()
            if self.expected_audio_codec
            else "Dolby audio"
        )

        self.status_changed.emit(
            "READY • Dolby Vision Profile 8.1 verified • "
            f"{audio_name} preserved • no video re-encode."
        )

        self.completed.emit(
            str(self.output)
        )

    # --------------------------------------------------------
    # Incomplete output safety
    # --------------------------------------------------------

    def _preserve_partial(self):
        if (
            self.partial_output is None
            or not self.partial_output.exists()
        ):
            return None

        base = (
            self.output.parent
            / f"{self.output.stem}.incomplete.mp4"
        )

        candidate = base
        number = 2

        while candidate.exists():
            candidate = (
                self.output.parent
                / (
                    f"{self.output.stem}"
                    f".incomplete-{number}.mp4"
                )
            )

            number += 1

        try:
            self.partial_output.replace(
                candidate
            )

            self.partial_output = None

            return candidate

        except Exception:
            return self.partial_output

    # --------------------------------------------------------
    # Cleanup
    # --------------------------------------------------------

    def cleanup(self):
        self._kill_processes()

        if self.workspace:
            shutil.rmtree(
                self.workspace,
                ignore_errors=True,
            )

        self.workspace = None
        self.prepared_video = None
        self.verify_rpu = None
        self.prepared_audio = None
        self.selected_audio = None

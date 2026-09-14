"""
Video Joiner Execution Engine
Implements Lossless Stream Copy, TS Protocol Concat, and Visually Lossless Transcoding.
"""

import os
import sys
import tempfile
import subprocess
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Callable, Dict, Any, Tuple

from .probe import MediaFileInfo, CompatibilityAnalysis
from .ffmpeg_utils import parse_time_to_seconds


@dataclass
class JoinProgress:
    percent: float = 0.0
    current_time_sec: float = 0.0
    total_duration_sec: float = 0.0
    speed: str = "1.0x"
    fps: float = 0.0
    eta_sec: float = 0.0
    status: str = "Processing..."


def escape_ffmpeg_concat_path(path: Path) -> str:
    """Escape file path for ffmpeg concat demuxer text file."""
    # Convert to POSIX format with forward slashes
    posix_path = path.resolve().as_posix()
    # In ffmpeg concat file, single quotes must be escaped as '\''
    escaped = posix_path.replace("'", "'\\''")
    return f"file '{escaped}'"


def run_ffmpeg_with_progress(
    cmd: List[str],
    total_duration: float,
    progress_callback: Optional[Callable[[JoinProgress], None]] = None,
    log_callback: Optional[Callable[[str], None]] = None
) -> Tuple[bool, str]:
    """
    Execute ffmpeg subprocess, parsing progress pipe in real time.
    Returns (success: bool, error_output: str).
    """
    startupinfo = None
    if sys.platform == "win32":
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW

    # Append progress reporting to stdout/pipe
    final_cmd = list(cmd) + ["-progress", "pipe:1", "-nostats"]
    
    start_time = time.time()
    stderr_lines = []
    
    try:
        proc = subprocess.Popen(
            final_cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            startupinfo=startupinfo
        )
    except Exception as e:
        return False, f"Failed to start FFmpeg: {e}"

    # Read progress from stdout
    out_time_sec = 0.0
    speed_str = "1.0x"
    fps_val = 0.0
    
    # Non-blocking or threaded stderr collector
    import threading
    def read_stderr():
        for line in proc.stderr:
            clean_line = line.strip()
            if clean_line:
                stderr_lines.append(clean_line)
                if log_callback:
                    log_callback(clean_line)
                    
    err_thread = threading.Thread(target=read_stderr, daemon=True)
    err_thread.start()

    for line in proc.stdout:
        line = line.strip()
        if not line:
            continue
            
        if line.startswith("out_time_us="):
            val = line.split("=", 1)[1].strip()
            try:
                out_time_sec = float(val) / 1_000_000.0
            except ValueError:
                pass
        elif line.startswith("out_time="):
            timestr = line.split("=", 1)[1].strip()
            parsed = parse_time_to_seconds(timestr)
            if parsed > 0:
                out_time_sec = parsed
        elif line.startswith("speed="):
            speed_str = line.split("=", 1)[1].strip()
        elif line.startswith("fps="):
            try:
                fps_val = float(line.split("=", 1)[1].strip())
            except ValueError:
                fps_val = 0.0
        elif line.startswith("progress="):
            stage = line.split("=", 1)[1].strip()
            if total_duration > 0:
                pct = min(100.0, max(0.0, (out_time_sec / total_duration) * 100.0))
            else:
                pct = 0.0
                
            elapsed = max(0.001, time.time() - start_time)
            if pct > 0:
                est_total = (elapsed / (pct / 100.0))
                eta_sec = max(0.0, est_total - elapsed)
            else:
                eta_sec = 0.0
                
            if progress_callback:
                prog = JoinProgress()
                prog.percent = pct
                prog.current_time_sec = out_time_sec
                prog.total_duration_sec = total_duration
                prog.speed = speed_str
                prog.fps = fps_val
                prog.eta_sec = eta_sec
                prog.status = "Complete" if stage == "end" else "Joining..."
                progress_callback(prog)

    proc.wait()
    err_thread.join(timeout=2.0)
    try:
        if proc.stdout:
            proc.stdout.close()
        if proc.stderr:
            proc.stderr.close()
    except Exception:
        pass
    
    success = (proc.returncode == 0)
    err_msg = "\n".join(stderr_lines[-30:]) if not success else ""
    return success, err_msg


class VideoJoiner:
    """
    High-performance video joiner supporting Lossless Demux Concat,
    TS protocol join, and High-Fidelity Visually Lossless Transcode.
    """
    def __init__(self, ffmpeg_exe: Path, ffprobe_exe: Path):
        self.ffmpeg_exe = ffmpeg_exe
        self.ffprobe_exe = ffprobe_exe

    def join_lossless_demux(
        self,
        files: List[MediaFileInfo],
        output_path: Path,
        progress_callback: Optional[Callable[[JoinProgress], None]] = None,
        log_callback: Optional[Callable[[str], None]] = None
    ) -> Tuple[bool, str]:
        """
        Execute 100% mathematical lossless concatenation via FFmpeg Concat Demuxer.
        Copies raw video & audio packets directly with ZERO quality degradation.
        """
        output_path.parent.mkdir(parents=True, exist_ok=True)
        total_duration = sum(f.duration for f in files)
        
        # Determine bitstream filters
        # e.g., if converting AAC from TS/ADTS or stream to MP4 container
        out_ext = output_path.suffix.lower()
        has_aac = any(f.audio and "aac" in f.audio.codec.lower() for f in files)
        has_ts = any(f.path.suffix.lower() == ".ts" for f in files)
        
        bsf_args = []
        if has_aac and (out_ext in (".mp4", ".m4v", ".mov") or has_ts):
            bsf_args = ["-bsf:a", "aac_adtstoasc"]

        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8") as concat_file:
            concat_list_path = Path(concat_file.name)
            concat_file.write("ffconcat version 1.0\n")
            for f in files:
                concat_file.write(f"{escape_ffmpeg_concat_path(f.path)}\n")

        try:
            cmd = [
                str(self.ffmpeg_exe),
                "-y",
                "-f", "concat",
                "-safe", "0",
                "-i", str(concat_list_path),
                "-c", "copy"
            ]
            
            # Subtitle / stream mapping
            if out_ext == ".mkv":
                cmd.extend(["-map", "0"])
            else:
                # Map video and audio if present
                cmd.extend(["-map", "0:v?", "-map", "0:a?"])
                
            cmd.extend(bsf_args)
            
            # Timestamp continuity and web-optimization flags
            cmd.extend([
                "-fflags", "+genpts+discardcorrupt",
                "-avoid_negative_ts", "make_zero"
            ])
            
            if out_ext in (".mp4", ".m4v", ".mov"):
                cmd.extend(["-movflags", "+faststart"])
                
            cmd.append(str(output_path))
            
            success, err = run_ffmpeg_with_progress(
                cmd,
                total_duration=total_duration,
                progress_callback=progress_callback,
                log_callback=log_callback
            )
            return success, err
        finally:
            try:
                if concat_list_path.is_file():
                    concat_list_path.unlink()
            except Exception:
                pass

    def join_lossless_remux(
        self,
        files: List[MediaFileInfo],
        output_path: Path,
        progress_callback: Optional[Callable[[JoinProgress], None]] = None,
        log_callback: Optional[Callable[[str], None]] = None
    ) -> Tuple[bool, str]:
        """
        Lossless Intermediate Normalization:
        Remuxes mixed containers (.ts, .mp4, .mkv) losslessly (-c copy) into
        intermediate transport stream (.ts) chunks, then merges them with concat protocol.
        Guarantees 100% mathematical zero loss across mixed containers.
        """
        output_path.parent.mkdir(parents=True, exist_ok=True)
        total_duration = sum(f.duration for f in files)
        out_ext = output_path.suffix.lower()

        temp_dir = Path(tempfile.mkdtemp(prefix="joiner_remux_"))
        temp_ts_files: List[Path] = []

        try:
            for i, f in enumerate(files):
                if f.path.suffix.lower() == ".ts":
                    temp_ts_files.append(f.path)
                    continue

                temp_ts = temp_dir / f"chunk_{i:04d}.ts"
                temp_ts_files.append(temp_ts)

                v_codec = (f.video.codec if f.video else "").lower()
                bsf_v = []
                if "264" in v_codec or "avc" in v_codec:
                    bsf_v = ["-bsf:v", "h264_mp4toannexb"]
                elif "265" in v_codec or "hevc" in v_codec:
                    bsf_v = ["-bsf:v", "hevc_mp4toannexb"]

                cmd = [
                    str(self.ffmpeg_exe),
                    "-y",
                    "-i", str(f.path),
                    "-c", "copy",
                    "-map", "0:v?",
                    "-map", "0:a?"
                ]
                cmd.extend(bsf_v)
                cmd.append(str(temp_ts))

                startupinfo = None
                if sys.platform == "win32":
                    startupinfo = subprocess.STARTUPINFO()
                    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW

                subprocess.run(
                    cmd,
                    check=True,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    startupinfo=startupinfo
                )

            # Join all TS chunks via concat protocol
            concat_url = "concat:" + "|".join(str(p.resolve()) for p in temp_ts_files)
            cmd = [
                str(self.ffmpeg_exe),
                "-y",
                "-i", concat_url,
                "-c", "copy",
                "-fflags", "+genpts",
                "-avoid_negative_ts", "make_zero"
            ]

            if out_ext in (".mp4", ".m4v", ".mov"):
                cmd.extend(["-bsf:a", "aac_adtstoasc", "-movflags", "+faststart"])

            cmd.append(str(output_path))

            return run_ffmpeg_with_progress(
                cmd,
                total_duration=total_duration,
                progress_callback=progress_callback,
                log_callback=log_callback
            )
        finally:
            import shutil
            shutil.rmtree(temp_dir, ignore_errors=True)

    def join_lossless_smart(
        self,
        files: List[MediaFileInfo],
        output_path: Path,
        progress_callback: Optional[Callable[[JoinProgress], None]] = None,
        log_callback: Optional[Callable[[str], None]] = None
    ) -> Tuple[bool, str]:
        """
        Smart lossless joiner:
        - If all files are .ts: uses concat protocol
        - If mixed containers (.ts, .mp4, .mkv): uses lossless intermediate remux
        - If matching containers: tries concat demuxer, falling back to remux if needed.
        """
        extensions = {f.path.suffix.lower() for f in files}
        
        if extensions == {".ts"}:
            return self.join_ts_protocol(files, output_path, progress_callback, log_callback)
        elif len(extensions) > 1 or ".ts" in extensions:
            return self.join_lossless_remux(files, output_path, progress_callback, log_callback)
        else:
            success, err = self.join_lossless_demux(files, output_path, progress_callback, log_callback)
            if not success:
                # Fallback to remux
                return self.join_lossless_remux(files, output_path, progress_callback, log_callback)
            return success, err

    def join_ts_protocol(
        self,
        files: List[MediaFileInfo],
        output_path: Path,
        progress_callback: Optional[Callable[[JoinProgress], None]] = None,
        log_callback: Optional[Callable[[str], None]] = None
    ) -> Tuple[bool, str]:
        """
        Lossless concatenation using FFmpeg's concat: protocol for MPEG-TS streams.
        Ideal for .ts chunks (e.g. from HLS, broadcast streams).
        """
        output_path.parent.mkdir(parents=True, exist_ok=True)
        total_duration = sum(f.duration for f in files)
        out_ext = output_path.suffix.lower()
        
        # Build concat string: "concat:f1.ts|f2.ts|f3.ts"
        ts_inputs = "|".join(str(f.path.resolve()) for f in files)
        concat_url = f"concat:{ts_inputs}"
        
        cmd = [
            str(self.ffmpeg_exe),
            "-y",
            "-i", concat_url,
            "-c", "copy",
            "-fflags", "+genpts",
            "-avoid_negative_ts", "make_zero"
        ]
        
        if out_ext in (".mp4", ".m4v"):
            cmd.extend(["-bsf:a", "aac_adtstoasc", "-movflags", "+faststart"])
            
        cmd.append(str(output_path))
        
        return run_ffmpeg_with_progress(
            cmd,
            total_duration=total_duration,
            progress_callback=progress_callback,
            log_callback=log_callback
        )

    def join_visually_lossless_transcode(
        self,
        files: List[MediaFileInfo],
        analysis: CompatibilityAnalysis,
        output_path: Path,
        crf: int = 17,
        preset: str = "slow",
        progress_callback: Optional[Callable[[JoinProgress], None]] = None,
        log_callback: Optional[Callable[[str], None]] = None
    ) -> Tuple[bool, str]:
        """
        High-Fidelity Visually Lossless Transcoding.
        Used when input videos have mismatched codecs, resolutions, or framerates.
        Scales and pads all videos to target canvas with exact aspect ratio preservation,
        resamples audio, and concatenates using libx264 with visually transparent CRF.
        """
        output_path.parent.mkdir(parents=True, exist_ok=True)
        total_duration = analysis.total_duration
        target_w = analysis.target_width
        target_h = analysis.target_height
        target_fps = analysis.target_fps
        target_sr = analysis.target_audio_sample_rate
        out_ext = output_path.suffix.lower()
        
        # Build filter_complex
        cmd = [str(self.ffmpeg_exe), "-y"]
        for f in files:
            cmd.extend(["-i", str(f.path)])
            
        filter_complex_parts = []
        n = len(files)
        
        for i in range(n):
            # Video normalization:
            # 1. Scale preserving aspect ratio inside target canvas
            # 2. Pad to exact target canvas (centered, black borders if aspect ratio differs)
            # 3. Set SAR to 1:1 square pixels
            # 4. Standardize FPS
            v_filter = (
                f"[{i}:v]scale={target_w}:{target_h}:force_original_aspect_ratio=decrease,"
                f"pad={target_w}:{target_h}:(ow-iw)/2:(oh-ih)/2:black,"
                f"setsar=1,fps={target_fps}[v{i}];"
            )
            filter_complex_parts.append(v_filter)
            
            # Audio normalization:
            # Check if file has audio
            if files[i].audio:
                a_filter = f"[{i}:a]aresample={target_sr},aformat=sample_fmts=fltp:channel_layouts=stereo[a{i}];"
            else:
                # Generate silent audio matching video duration to keep sync
                dur = files[i].duration if files[i].duration > 0 else 1.0
                a_filter = f"anullsrc=r={target_sr}:cl=stereo:d={dur}[a{i}];"
            filter_complex_parts.append(a_filter)
            
        # Concat filter
        concat_inputs = "".join(f"[v{i}][a{i}]" for i in range(n))
        concat_filter = f"{concat_inputs}concat=n={n}:v=1:a=1[outv][outa]"
        filter_complex_parts.append(concat_filter)
        
        full_filter = "".join(filter_complex_parts)
        
        cmd.extend([
            "-filter_complex", full_filter,
            "-map", "[outv]",
            "-map", "[outa]",
            "-c:v", "libx264",
            "-crf", str(crf),
            "-preset", preset,
            "-pix_fmt", "yuv420p",
            "-c:a", "aac",
            "-b:a", "320k"
        ])
        
        if out_ext in (".mp4", ".m4v", ".mov"):
            cmd.extend(["-movflags", "+faststart"])
            
        cmd.append(str(output_path))
        
        return run_ffmpeg_with_progress(
            cmd,
            total_duration=total_duration,
            progress_callback=progress_callback,
            log_callback=log_callback
        )

#!/usr/bin/env python3
"""
🎬 Standalone Lossless Video Joiner for GitHub Actions & CLI
Downloads videos/folders from public Google Drive links and merges them losslessly.
Self-contained script: Zero local subfolder dependencies.
"""

import os
import sys
import re
import shutil
import subprocess
import tempfile
import threading
import time
import argparse
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Tuple, Dict, Any, Union, Callable

# Optional dependencies
try:
    import requests
except ImportError:
    requests = None

try:
    import gdown
except ImportError:
    gdown = None


# =====================================================================
# 1. FILE FILTERING & NATURAL SORTING
# =====================================================================

SUPPORTED_EXTENSIONS = {
    ".mp4", ".ts", ".mkv", ".mov", ".avi", ".webm",
    ".flv", ".wmv", ".m4v", ".mts", ".m2ts", ".vob", ".3gp"
}


def natural_sort_key(s: Union[str, Path]):
    """Alphanumeric natural order key (e.g. video_2 before video_10)."""
    text = str(s.name if isinstance(s, Path) else s)
    return [int(c) if c.isdigit() else c.lower() for c in re.split(r"(\d+)", text)]


def filter_video_files(paths: List[Path]) -> List[Path]:
    """Filter paths by supported video file extensions."""
    return [p for p in paths if p.is_file() and p.suffix.lower() in SUPPORTED_EXTENSIONS]


def sort_files(files: List[Path], sort_mode: str = "natural", reverse: bool = False) -> List[Path]:
    """Sort files according to specified mode."""
    if sort_mode == "natural":
        sorted_list = sorted(files, key=natural_sort_key)
    elif sort_mode == "alphabetical":
        sorted_list = sorted(files, key=lambda f: f.name.lower())
    elif sort_mode == "date":
        sorted_list = sorted(files, key=lambda f: f.stat().st_mtime)
    elif sort_mode == "size":
        sorted_list = sorted(files, key=lambda f: f.stat().st_size)
    else:
        sorted_list = list(files)
        
    if reverse:
        sorted_list.reverse()
    return sorted_list


# =====================================================================
# 2. FFMPEG UTILITIES & BINARY DETECTION
# =====================================================================

def parse_time_to_seconds(timestr: str) -> float:
    """Parse HH:MM:SS.micro or SS into float seconds."""
    timestr = timestr.strip()
    if not timestr or timestr == "N/A":
        return 0.0
    try:
        parts = timestr.split(":")
        if len(parts) == 3:
            return float(parts[0]) * 3600 + float(parts[1]) * 60 + float(parts[2])
        elif len(parts) == 2:
            return float(parts[0]) * 60 + float(parts[1])
        return float(timestr)
    except ValueError:
        return 0.0


def find_binary(name: str) -> Optional[Path]:
    """Find binary in system PATH or common paths."""
    exe_name = f"{name}.exe" if sys.platform == "win32" else name
    
    # 1. System PATH
    which_path = shutil.which(name) or shutil.which(exe_name)
    if which_path:
        return Path(which_path).resolve()
        
    # 2. Local folder / bin
    for folder in [Path.cwd(), Path.cwd() / "bin", Path(__file__).parent, Path(__file__).parent / "bin"]:
        cand = folder / exe_name
        if cand.is_file():
            return cand.resolve()
            
    # 3. Windows standard locations
    if sys.platform == "win32":
        for folder in [
            Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "WinGet" / "Links",
            Path("C:/ProgramData/chocolatey/bin"),
            Path("C:/ffmpeg/bin"),
            Path("C:/Program Files/ffmpeg/bin"),
        ]:
            cand = folder / exe_name
            if cand.is_file():
                return cand.resolve()
    return None


def ensure_ffmpeg(auto_prompt: bool = True) -> Tuple[Path, Path]:
    """Ensure ffmpeg and ffprobe binaries are accessible."""
    ffmpeg = find_binary("ffmpeg")
    ffprobe = find_binary("ffprobe")
    
    if ffmpeg and not ffprobe:
        sibling = ffmpeg.parent / ("ffprobe.exe" if sys.platform == "win32" else "ffprobe")
        if sibling.is_file():
            ffprobe = sibling.resolve()
            
    if ffmpeg and ffprobe:
        return ffmpeg, ffprobe
        
    raise RuntimeError("FFmpeg and ffprobe must be installed on your system PATH.")


# =====================================================================
# 3. STREAM PROBING & COMPATIBILITY ENGINE
# =====================================================================

@dataclass
class VideoStreamInfo:
    codec: str = ""
    codec_tag: str = ""
    width: int = 0
    height: int = 0
    fps: float = 0.0
    bitrate: int = 0
    pixel_format: str = ""
    aspect_ratio: str = ""


@dataclass
class AudioStreamInfo:
    codec: str = ""
    sample_rate: int = 0
    channels: int = 0
    bitrate: int = 0


@dataclass
class MediaFileInfo:
    path: Path
    duration: float = 0.0
    size_bytes: int = 0
    video: Optional[VideoStreamInfo] = None
    audio: Optional[AudioStreamInfo] = None
    container_format: str = ""
    error: Optional[str] = None


@dataclass
class CompatibilityAnalysis:
    is_lossless_ready: bool = False
    reasons_against_copy: List[str] = field(default_factory=list)
    target_width: int = 0
    target_height: int = 0
    target_fps: float = 0.0
    target_audio_sample_rate: int = 48000
    total_duration: float = 0.0
    total_size_bytes: int = 0


def probe_file(file_path: Path, ffprobe_exe: Path, ffmpeg_exe: Path) -> MediaFileInfo:
    """Inspect video file metadata using ffprobe."""
    info = MediaFileInfo(path=file_path)
    if not file_path.is_file():
        info.error = "File does not exist"
        return info

    info.size_bytes = file_path.stat().st_size
    cmd = [
        str(ffprobe_exe),
        "-v", "error",
        "-show_entries", "format=duration,format_name:stream=codec_type,codec_name,codec_tag_string,width,height,r_frame_rate,avg_frame_rate,bit_rate,pix_fmt,sample_rate,channels",
        "-of", "json",
        str(file_path)
    ]
    
    try:
        startupinfo = None
        if sys.platform == "win32":
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            
        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", startupinfo=startupinfo, timeout=30)
        if res.returncode != 0:
            info.error = res.stderr.strip() or "ffprobe failed"
            return info
            
        import json
        data = json.loads(res.stdout)
        fmt = data.get("format", {})
        info.duration = float(fmt.get("duration", 0.0))
        info.container_format = fmt.get("format_name", "")
        
        for st in data.get("streams", []):
            ctype = st.get("codec_type")
            if ctype == "video" and not info.video:
                v = VideoStreamInfo()
                v.codec = st.get("codec_name", "")
                v.codec_tag = st.get("codec_tag_string", "")
                v.width = int(st.get("width", 0))
                v.height = int(st.get("height", 0))
                v.pixel_format = st.get("pix_fmt", "")
                
                # Parse FPS
                r_fps = st.get("r_frame_rate", "") or st.get("avg_frame_rate", "")
                if r_fps and "/" in r_fps:
                    num, den = r_fps.split("/")
                    if float(den) > 0:
                        v.fps = round(float(num) / float(den), 2)
                elif r_fps:
                    try:
                        v.fps = round(float(r_fps), 2)
                    except ValueError:
                        v.fps = 0.0
                info.video = v
                
            elif ctype == "audio" and not info.audio:
                a = AudioStreamInfo()
                a.codec = st.get("codec_name", "")
                a.sample_rate = int(st.get("sample_rate", 0))
                a.channels = int(st.get("channels", 0))
                info.audio = a

    except Exception as e:
        info.error = str(e)

    return info


def analyze_compatibility(files: List[MediaFileInfo]) -> CompatibilityAnalysis:
    """Analyze whether videos can be joined losslessly without transcoding."""
    analysis = CompatibilityAnalysis()
    valid_files = [f for f in files if not f.error and f.video]
    if not valid_files:
        analysis.reasons_against_copy.append("No valid video streams found.")
        return analysis

    analysis.total_duration = sum(f.duration for f in valid_files)
    analysis.total_size_bytes = sum(f.size_bytes for f in valid_files)

    first_v = valid_files[0].video
    analysis.target_width = first_v.width
    analysis.target_height = first_v.height
    analysis.target_fps = first_v.fps if first_v.fps > 0 else 30.0

    has_audio_any = any(f.audio for f in valid_files)
    first_a = next((f.audio for f in valid_files if f.audio), None)
    if first_a:
        analysis.target_audio_sample_rate = first_a.sample_rate or 48000

    can_copy = True
    for idx, f in enumerate(valid_files[1:], start=2):
        v = f.video
        if v.codec.lower() != first_v.codec.lower():
            analysis.reasons_against_copy.append(f"File #{idx} video codec '{v.codec}' != File #1 '{first_v.codec}'")
            can_copy = False
        if v.width != first_v.width or v.height != first_v.height:
            analysis.reasons_against_copy.append(f"File #{idx} resolution {v.width}x{v.height} != File #1 {first_v.width}x{first_v.height}")
            can_copy = False
        if v.pixel_format and first_v.pixel_format and v.pixel_format != first_v.pixel_format:
            analysis.reasons_against_copy.append(f"File #{idx} pixel format '{v.pixel_format}' != File #1 '{first_v.pixel_format}'")
            can_copy = False
        if has_audio_any:
            if not f.audio:
                analysis.reasons_against_copy.append(f"File #{idx} is missing an audio track while others have audio.")
                can_copy = False
            elif first_a and f.audio.codec.lower() != first_a.codec.lower():
                analysis.reasons_against_copy.append(f"File #{idx} audio codec '{f.audio.codec}' != File #1 '{first_a.codec}'")
                can_copy = False

    analysis.is_lossless_ready = can_copy
    return analysis


# =====================================================================
# 4. DOWNLOADER ENGINE (GOOGLE DRIVE FILES & FOLDERS)
# =====================================================================

def extract_gdrive_id(url: str) -> Optional[str]:
    """Extract file/folder ID from Google Drive URLs."""
    url = url.strip()
    m = re.search(r"(?:/file/d/|/d/|/folders/)([a-zA-Z0-9_-]{25,})", url)
    if m:
        return m.group(1)
    m = re.search(r"[?&]id=([a-zA-Z0-9_-]{25,})", url)
    if m:
        return m.group(1)
    if re.match(r"^[a-zA-Z0-9_-]{25,}$", url):
        return url
    return None


def is_gdrive_folder(url: str) -> bool:
    """Check if URL points to a Google Drive folder."""
    return bool(re.search(r"(?:/drive/(?:u/\d+/)?folders/|/folders/)", url.strip()))


def extract_urls_from_text(text: str) -> List[str]:
    """Parse multiple URLs separated by newlines, commas, or whitespace."""
    if not text:
        return []
    urls: List[str] = []
    normalized = text.replace(",", "\n").replace(";", "\n")
    for raw_line in normalized.splitlines():
        line = raw_line.strip().strip('"').strip("'").strip()
        if not line or line.startswith("#") or line.startswith("//"):
            continue
        for token in line.split():
            token = token.strip().strip('"').strip("'").strip()
            if not token:
                continue
            if token.startswith("http://") or token.startswith("https://") or extract_gdrive_id(token):
                if token not in urls:
                    urls.append(token)
    return urls


def parse_links_file(file_path: Path) -> List[str]:
    """Read URLs from text file."""
    if not file_path.is_file():
        raise FileNotFoundError(f"Links file not found: {file_path}")
    return extract_urls_from_text(file_path.read_text(encoding="utf-8", errors="replace"))


def detect_video_extension(path: Path) -> str:
    """Inspect magic bytes to detect video container format."""
    try:
        with open(path, "rb") as f:
            header = f.read(64)
            if len(header) >= 8:
                if len(header) >= 12 and header[4:8] == b"ftyp":
                    return ".mp4"
                if header.startswith(b"\x1a\x45\xdf\xa3"):
                    return ".mkv"
                if len(header) >= 12 and header.startswith(b"RIFF") and header[8:12] == b"AVI ":
                    return ".avi"
                if header.startswith(b"FLV"):
                    return ".flv"
                if header[0] == 0x47:
                    return ".ts"
    except Exception:
        pass
    return ".mp4"


def ensure_video_extension(file_path: Path, default_ext: str = ".mp4") -> Path:
    """Ensure downloaded file has a recognized video extension."""
    if file_path.suffix.lower() in SUPPORTED_EXTENSIONS:
        return file_path
    detected_ext = detect_video_extension(file_path) or default_ext
    target_path = file_path.with_name(f"{file_path.name}{detected_ext}")
    counter = 1
    while target_path.exists() and target_path != file_path:
        target_path = file_path.with_name(f"{file_path.stem}_{counter}{detected_ext}")
        counter += 1
    try:
        file_path.rename(target_path)
        return target_path
    except Exception:
        return file_path


def download_with_gdown(url_or_id: str, dest_dir: Path, index: int = 1) -> Path:
    """Download single Google Drive file via gdown."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    file_id = extract_gdrive_id(url_or_id)
    url = f"https://drive.google.com/uc?id={file_id}" if file_id else url_or_id
    dest_param = str(dest_dir.resolve()) + os.sep
    
    res = None
    try:
        res = gdown.download(url=url, output=dest_param, quiet=False, fuzzy=True)
    except Exception:
        pass
    if (not res or not Path(res).is_file()) and file_id:
        try:
            res = gdown.download(id=file_id, output=dest_param, quiet=False, fuzzy=True)
        except Exception:
            pass
    if not res or not Path(res).is_file():
        fallback = dest_dir / f"video_{index:02d}.mp4"
        res = gdown.download(url=url, output=str(fallback), quiet=False, fuzzy=True)
        
    if not res or not Path(res).is_file():
        raise RuntimeError(f"Failed to download Google Drive file: {url_or_id}")
    return ensure_video_extension(Path(res).resolve())


def download_gdrive_folder(url: str, dest_dir: Path) -> List[Path]:
    """Download all video files from a public Google Drive folder."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    folder_id = extract_gdrive_id(url)
    folder_dest = dest_dir / f"folder_{folder_id or 'shared'}"
    folder_dest.mkdir(parents=True, exist_ok=True)
    
    print(f"\n📁 Batch downloading Google Drive shared folder: {url}...")
    try:
        gdown.download_folder(url=url, output=str(folder_dest.resolve()), quiet=False)
    except Exception as e:
        if folder_id:
            try:
                gdown.download_folder(id=folder_id, output=str(folder_dest.resolve()), quiet=False)
            except Exception:
                pass
                
    found_videos = []
    for p in folder_dest.rglob("*"):
        if p.is_file():
            p_fixed = ensure_video_extension(p)
            if p_fixed.suffix.lower() in SUPPORTED_EXTENSIONS and p_fixed not in found_videos:
                found_videos.append(p_fixed)
    print(f"✓ Found {len(found_videos)} video files in folder.")
    return found_videos


def download_with_requests(url_or_id: str, dest_dir: Path, index: int = 1) -> Path:
    """Download Google Drive file using requests with token handling."""
    if not requests:
        raise ImportError("requests is required for downloading.")
    file_id = extract_gdrive_id(url_or_id)
    download_url = "https://docs.google.com/uc?export=download" if file_id else url_or_id
    session = requests.Session()
    params = {"id": file_id} if file_id else {}
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) VideoJoiner/1.0"}
    
    resp = session.get(download_url, params=params, headers=headers, stream=True)
    token = None
    for k, v in resp.cookies.items():
        if k.startswith("download_warning"):
            token = v
            break
    if not token and resp.text:
        m = re.search(r'confirm=([0-9A-Za-z_]+)', resp.text)
        if m:
            token = m.group(1)
    if token:
        params["confirm"] = token
        resp = session.get(download_url, params=params, headers=headers, stream=True)
        
    cd = resp.headers.get("content-disposition", "")
    filename = None
    if cd:
        m = re.search(r'filename="?([^";]+)"?', cd)
        if m:
            filename = m.group(1).strip()
    if not filename:
        filename = f"drive_video_{index:02d}.mp4"
        
    dest_path = dest_dir / filename
    with open(dest_path, "wb") as f:
        for chunk in resp.iter_content(chunk_size=128 * 1024):
            if chunk:
                f.write(chunk)
    return ensure_video_extension(dest_path)


def download_video(url_or_id: str, dest_dir: Path, index: int = 1) -> Path:
    """Download a video using gdown with requests fallback."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    file_id = extract_gdrive_id(url_or_id)
    if gdown and file_id:
        try:
            return download_with_gdown(url_or_id, dest_dir, index=index)
        except Exception:
            pass
    return download_with_requests(url_or_id, dest_dir, index=index)


def download_all_videos(urls: List[str], dest_dir: Path) -> List[Path]:
    """Download all videos from URLs (files or folders)."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    downloaded = []
    total = len(urls)
    for i, u in enumerate(urls, start=1):
        if is_gdrive_folder(u):
            folder_vids = download_gdrive_folder(u, dest_dir)
            downloaded.extend(folder_vids)
        else:
            print(f"📥 Downloading [{i}/{total}]: {u}")
            p = download_video(u, dest_dir, index=len(downloaded) + 1)
            if p and p.is_file() and p not in downloaded:
                downloaded.append(p)
    return downloaded


# =====================================================================
# 5. VIDEO JOINER EXECUTION ENGINE (LOSSLESS & TRANSCODE)
# =====================================================================

@dataclass
class JoinProgress:
    percent: float = 0.0
    current_time_sec: float = 0.0
    total_duration_sec: float = 0.0
    speed: str = "1.0x"
    fps: float = 0.0
    eta_sec: float = 0.0


def escape_ffmpeg_concat_path(path: Path) -> str:
    """Escape path for ffmpeg concat demuxer text file."""
    posix_path = path.resolve().as_posix()
    escaped = posix_path.replace("'", "'\\''")
    return f"file '{escaped}'"


def run_ffmpeg_with_progress(cmd: List[str], total_duration: float, progress_callback: Optional[Callable[[JoinProgress], None]] = None) -> Tuple[bool, str]:
    """Execute ffmpeg subprocess with live progress parsing."""
    startupinfo = None
    if sys.platform == "win32":
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW

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

    def read_stderr():
        for line in proc.stderr:
            clean = line.strip()
            if clean:
                stderr_lines.append(clean)

    err_thread = threading.Thread(target=read_stderr, daemon=True)
    err_thread.start()

    out_time_sec = 0.0
    speed_str = "1.0x"
    fps_val = 0.0

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
            parsed = parse_time_to_seconds(line.split("=", 1)[1].strip())
            if parsed > 0:
                out_time_sec = parsed
        elif line.startswith("speed="):
            speed_str = line.split("=", 1)[1].strip()
        elif line.startswith("fps="):
            try:
                fps_val = float(line.split("=", 1)[1].strip())
            except ValueError:
                pass
        elif line.startswith("progress="):
            pct = min(100.0, max(0.0, (out_time_sec / total_duration) * 100.0)) if total_duration > 0 else 0.0
            elapsed = max(0.001, time.time() - start_time)
            eta = max(0.0, (elapsed / (pct / 100.0)) - elapsed) if pct > 0 else 0.0
            if progress_callback:
                p = JoinProgress(percent=pct, current_time_sec=out_time_sec, total_duration_sec=total_duration, speed=speed_str, fps=fps_val, eta_sec=eta)
                progress_callback(p)

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
    """Concatenation engine supporting stream copy and fallback transcoding."""
    def __init__(self, ffmpeg_exe: Path, ffprobe_exe: Path):
        self.ffmpeg_exe = ffmpeg_exe
        self.ffprobe_exe = ffprobe_exe

    def join_lossless_demux(self, files: List[MediaFileInfo], output_path: Path, progress_callback=None) -> Tuple[bool, str]:
        """FFmpeg Concat Demuxer: 100% mathematical zero-loss copy (-c copy)."""
        output_path.parent.mkdir(parents=True, exist_ok=True)
        total_duration = sum(f.duration for f in files)
        out_ext = output_path.suffix.lower()

        has_aac = any(f.audio and "aac" in f.audio.codec.lower() for f in files)
        has_ts = any(f.path.suffix.lower() == ".ts" for f in files)
        bsf_args = ["-bsf:a", "aac_adtstoasc"] if (has_aac and (out_ext in (".mp4", ".m4v", ".mov") or has_ts)) else []

        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8") as concat_file:
            concat_list_path = Path(concat_file.name)
            concat_file.write("ffconcat version 1.0\n")
            for f in files:
                concat_file.write(f"{escape_ffmpeg_concat_path(f.path)}\n")

        try:
            cmd = [
                str(self.ffmpeg_exe), "-y",
                "-f", "concat", "-safe", "0",
                "-i", str(concat_list_path),
                "-c", "copy"
            ]
            if out_ext == ".mkv":
                cmd.extend(["-map", "0"])
            else:
                cmd.extend(["-map", "0:v?", "-map", "0:a?"])
            cmd.extend(bsf_args)
            cmd.extend(["-fflags", "+genpts+discardcorrupt", "-avoid_negative_ts", "make_zero"])
            if out_ext in (".mp4", ".m4v", ".mov"):
                cmd.extend(["-movflags", "+faststart"])
            cmd.append(str(output_path))
            return run_ffmpeg_with_progress(cmd, total_duration, progress_callback)
        finally:
            try:
                if concat_list_path.is_file():
                    concat_list_path.unlink()
            except Exception:
                pass

    def join_lossless_remux(self, files: List[MediaFileInfo], output_path: Path, progress_callback=None) -> Tuple[bool, str]:
        """Intermediate remux into transport streams then concat copy."""
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
                bsf_v = ["-bsf:v", "h264_mp4toannexb"] if "264" in v_codec else (["-bsf:v", "hevc_mp4toannexb"] if "265" in v_codec or "hevc" in v_codec else [])
                
                cmd = [str(self.ffmpeg_exe), "-y", "-i", str(f.path), "-c", "copy", "-map", "0:v?", "-map", "0:a?"] + bsf_v + [str(temp_ts)]
                startupinfo = None
                if sys.platform == "win32":
                    startupinfo = subprocess.STARTUPINFO()
                    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, startupinfo=startupinfo)

            concat_url = "concat:" + "|".join(str(p.resolve()) for p in temp_ts_files)
            cmd = [str(self.ffmpeg_exe), "-y", "-i", concat_url, "-c", "copy", "-fflags", "+genpts", "-avoid_negative_ts", "make_zero"]
            if out_ext in (".mp4", ".m4v", ".mov"):
                cmd.extend(["-bsf:a", "aac_adtstoasc", "-movflags", "+faststart"])
            cmd.append(str(output_path))
            return run_ffmpeg_with_progress(cmd, total_duration, progress_callback)
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

    def join_lossless_smart(self, files: List[MediaFileInfo], output_path: Path, progress_callback=None) -> Tuple[bool, str]:
        """Smart lossless joiner: chooses best direct stream copy strategy."""
        exts = {f.path.suffix.lower() for f in files}
        if len(exts) > 1 or ".ts" in exts:
            return self.join_lossless_remux(files, output_path, progress_callback)
        success, err = self.join_lossless_demux(files, output_path, progress_callback)
        if not success:
            return self.join_lossless_remux(files, output_path, progress_callback)
        return success, err

    def join_visually_lossless_transcode(self, files: List[MediaFileInfo], analysis: CompatibilityAnalysis, output_path: Path, crf: int = 17, preset: str = "slow", progress_callback=None) -> Tuple[bool, str]:
        """Harmonize mismatched resolutions/codecs using high-fidelity CRF 17."""
        output_path.parent.mkdir(parents=True, exist_ok=True)
        total_duration = analysis.total_duration
        target_w = analysis.target_width
        target_h = analysis.target_height
        target_fps = analysis.target_fps
        target_sr = analysis.target_audio_sample_rate
        out_ext = output_path.suffix.lower()

        cmd = [str(self.ffmpeg_exe), "-y"]
        for f in files:
            cmd.extend(["-i", str(f.path)])

        filters = []
        n = len(files)
        for i in range(n):
            filters.append(
                f"[{i}:v]scale={target_w}:{target_h}:force_original_aspect_ratio=decrease,"
                f"pad={target_w}:{target_h}:(ow-iw)/2:(oh-ih)/2:black,setsar=1,fps={target_fps}[v{i}];"
            )
            if files[i].audio:
                filters.append(f"[{i}:a]aresample={target_sr},aformat=sample_fmts=fltp:channel_layouts=stereo[a{i}];")
            else:
                dur = files[i].duration if files[i].duration > 0 else 1.0
                filters.append(f"anullsrc=r={target_sr}:cl=stereo:d={dur}[a{i}];")

        concat_in = "".join(f"[v{i}][a{i}]" for i in range(n))
        filters.append(f"{concat_in}concat=n={n}:v=1:a=1[outv][outa]")

        cmd.extend([
            "-filter_complex", "".join(filters),
            "-map", "[outv]", "-map", "[outa]",
            "-c:v", "libx264", "-crf", str(crf), "-preset", preset, "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "320k"
        ])
        if out_ext in (".mp4", ".m4v", ".mov"):
            cmd.extend(["-movflags", "+faststart"])
        cmd.append(str(output_path))
        return run_ffmpeg_with_progress(cmd, total_duration, progress_callback)


# =====================================================================
# 6. OUTPUT FORMATTING & CI SUMMARY
# =====================================================================

BANNER = r"""
  ╔═══════════════════════════════════════════════════════════╗
  ║          🎬 ADVANCED LOSSLESS VIDEO JOINER                ║
  ║  Intact Quality Concatenation for MP4, TS, MKV, MOV, etc. ║
  ║       With Google Drive URL Downloader Integration        ║
  ╚═══════════════════════════════════════════════════════════╝
"""


def format_duration(seconds: float) -> str:
    """Format seconds into HH:MM:SS."""
    hrs = int(seconds // 3600)
    mins = int((seconds % 3600) // 60)
    secs = int(seconds % 60)
    return f"{hrs:02d}:{mins:02d}:{secs:02d}" if hrs > 0 else f"{mins:02d}:{secs:02d}"


def format_size(size_bytes: int) -> str:
    """Format bytes into MB or GB."""
    mb = size_bytes / (1024 * 1024)
    return f"{mb / 1024:.2f} GB" if mb >= 1024 else f"{mb:.1f} MB"


def write_github_step_summary(files: List[MediaFileInfo], analysis: CompatibilityAnalysis, output_path: Path, mode_used: str, success: bool, error_msg: str = ""):
    """Write metrics to GitHub Step Summary if running in GitHub Actions."""
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not summary_path:
        return
    try:
        with open(summary_path, "a", encoding="utf-8") as f:
            f.write("# 🎬 Video Joiner Execution Summary\n\n")
            if success and output_path.is_file():
                f.write("### 🎉 Success: Video Merged Successfully!\n\n")
                f.write(f"- **Output File:** `{output_path.name}`\n")
                f.write(f"- **File Size:** `{format_size(output_path.stat().st_size)}`\n")
                f.write(f"- **Total Duration:** `{format_duration(analysis.total_duration)}`\n")
                f.write(f"- **Processing Mode:** `{mode_used}`\n\n")
                if "Lossless" in mode_used:
                    f.write("> [!NOTE]\n> **Quality Guarantee: 100% Lossless Direct Stream Copy** (`-c copy`).\n> Zero re-encoding occurred.\n\n")
                else:
                    f.write("> [!IMPORTANT]\n> **Visually Lossless Transcode** was applied due to varying stream properties.\n\n")
            else:
                f.write("### ❌ Error: Video Joining Failed\n\n")
                if error_msg:
                    f.write(f"```text\n{error_msg}\n```\n\n")
            f.write("### 📊 Input Video Clips\n\n| # | File Name | Resolution | Video Codec | FPS | Audio | Duration | Size |\n|---|---|---|---|---|---|---|---|\n")
            for idx, item in enumerate(files, start=1):
                v, a = item.video, item.audio
                res = f"{v.width}x{v.height}" if (v and v.width) else "N/A"
                v_codec = v.codec if v else "None"
                fps = f"{v.fps:.2f}" if (v and v.fps) else "N/A"
                a_codec = f"{a.codec} {a.channels}ch" if a else "No Audio"
                f.write(f"| {idx} | `{item.path.name}` | {res} | {v_codec} | {fps} | {a_codec} | {format_duration(item.duration)} | {format_size(item.size_bytes)} |\n")
    except Exception:
        pass


def execute_join(files: List[Path], output_path: Path, mode: str = "auto", crf: int = 17, preset: str = "slow", sort_mode: str = "natural", reverse_sort: bool = False, overwrite: bool = False) -> bool:
    """Execute video joining workflow."""
    print(BANNER)
    is_ci = bool(os.environ.get("CI") or os.environ.get("GITHUB_ACTIONS"))
    ffmpeg_exe, ffprobe_exe = ensure_ffmpeg(auto_prompt=not is_ci)

    valid_files = filter_video_files(files)
    if not valid_files:
        print("❌ Error: No supported video files found to join.")
        return False
    if len(valid_files) < 2:
        print(f"❌ Error: At least 2 video files are required to join. Found {len(valid_files)}.")
        return False

    sorted_paths = sort_files(valid_files, sort_mode=sort_mode, reverse=reverse_sort)
    print(f"🔍 Probing {len(sorted_paths)} media files...")
    probed_files = [probe_file(p, ffprobe_exe, ffmpeg_exe) for p in sorted_paths]

    errors = [f for f in probed_files if f.error]
    if errors:
        for err in errors:
            print(f"  ❌ Error probing {err.path.name}: {err.error}")
        return False

    analysis = analyze_compatibility(probed_files)
    output_path = output_path.resolve()

    if output_path.is_file() and not overwrite and not is_ci:
        ans = input(f"Output file '{output_path.name}' already exists. Overwrite? [y/N]: ").strip().lower()
        if ans not in ("y", "yes"):
            return False

    joiner = VideoJoiner(ffmpeg_exe, ffprobe_exe)
    use_lossless = (mode == "copy") or (mode == "auto" and analysis.is_lossless_ready)
    mode_reported = "Lossless Stream Copy (-c copy)" if use_lossless else f"Visually Lossless Transcode (CRF {crf})"

    def progress_callback(p: JoinProgress):
        cur_t, tot_t = format_duration(p.current_time_sec), format_duration(p.total_duration_sec)
        sys.stdout.write(f"\r  [{'█'*int(30*(p.percent/100.0)):<30}] {p.percent:5.1f}% ({cur_t} / {tot_t}) | Speed: {p.speed}")
        sys.stdout.flush()

    print(f"\n🚀 Joining into '{output_path.name}' ({mode_reported})...")
    success = False
    err_msg = ""

    if use_lossless:
        success, err_msg = joiner.join_lossless_smart(probed_files, output_path, progress_callback)
        if not success and mode == "auto":
            print("\n🔄 Falling back to Visually Lossless Transcode...")
            mode_reported = f"Fallback Visually Lossless Transcode (CRF {crf})"
            success, err_msg = joiner.join_visually_lossless_transcode(probed_files, analysis, output_path, crf=crf, preset=preset, progress_callback=progress_callback)
    else:
        success, err_msg = joiner.join_visually_lossless_transcode(probed_files, analysis, output_path, crf=crf, preset=preset, progress_callback=progress_callback)

    print()
    write_github_step_summary(probed_files, analysis, output_path, mode_reported, success, err_msg)

    if success and output_path.is_file():
        print(f"\n🎉 SUCCESS! Merged video saved: {output_path} ({format_size(output_path.stat().st_size)})")
        return True
    print(f"\n❌ JOINING FAILED: {err_msg}")
    return False


# =====================================================================
# 7. MAIN CLI & CI ENTRYPOINT
# =====================================================================

def main():
    parser = argparse.ArgumentParser(description="🎬 Advanced Lossless Video Joiner (Google Drive & Local)")
    parser.add_argument("-i", "--input", nargs="+", help="Input video file paths.")
    parser.add_argument("-d", "--dir", help="Directory containing video clips.")
    parser.add_argument("-l", "--list", help="Text file with video paths.")
    parser.add_argument("-g", "--gdrive", help="Text file containing Google Drive links.")
    parser.add_argument("--urls", nargs="+", help="Google Drive URLs to download and merge.")
    parser.add_argument("--download-dir", default="downloads", help="Download directory.")
    parser.add_argument("--clean-downloads", action="store_true", help="Remove downloaded clips after merge.")
    parser.add_argument("-o", "--output", default="merged_video.mp4", help="Output filename.")
    parser.add_argument("-m", "--mode", choices=["auto", "copy", "transcode"], default="auto", help="Join mode.")
    parser.add_argument("--crf", type=int, default=17, help="Transcode CRF.")
    parser.add_argument("--preset", default="slow", help="x264 preset.")
    parser.add_argument("--sort", choices=["natural", "alphabetical", "date", "size", "none"], default="natural", help="Sort order.")
    parser.add_argument("--reverse", action="store_true", help="Reverse sort.")
    parser.add_argument("-y", "--yes", action="store_true", help="Overwrite without asking.")

    args = parser.parse_args()
    is_ci = bool(os.environ.get("CI") or os.environ.get("GITHUB_ACTIONS"))
    if is_ci:
        args.yes = True

    download_dir = Path(args.download_dir).resolve()
    input_paths: List[Path] = []
    is_downloaded = False

    if args.gdrive:
        urls = parse_links_file(Path(args.gdrive))
        input_paths = download_all_videos(urls, dest_dir=download_dir)
        is_downloaded = True
    elif args.urls:
        parsed_urls = []
        for item in args.urls:
            parsed_urls.extend(extract_urls_from_text(item))
        input_paths = download_all_videos(parsed_urls, dest_dir=download_dir)
        is_downloaded = True
    elif os.environ.get("GDRIVE_URLS") or os.environ.get("VIDEO_URLS"):
        env_val = os.environ.get("GDRIVE_URLS") or os.environ.get("VIDEO_URLS") or ""
        parsed_urls = extract_urls_from_text(env_val)
        input_paths = download_all_videos(parsed_urls, dest_dir=download_dir)
        is_downloaded = True
    elif args.input:
        for item in args.input:
            p = Path(item)
            if p.is_file():
                input_paths.append(p)
    elif args.dir:
        input_paths = [p for p in Path(args.dir).iterdir() if p.is_file()]

    if not input_paths:
        print("❌ Error: No input video files or Google Drive URLs provided.")
        sys.exit(1)

    output_path = Path(args.output)
    success = execute_join(
        files=input_paths,
        output_path=output_path,
        mode=args.mode,
        crf=args.crf,
        preset=args.preset,
        sort_mode=args.sort,
        reverse_sort=args.reverse,
        overwrite=args.yes
    )

    if success and is_downloaded and args.clean_downloads:
        for f in input_paths:
            try:
                f.unlink()
            except Exception:
                pass

    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()

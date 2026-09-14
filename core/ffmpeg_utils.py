"""
FFmpeg Utilities & Auto-Detection Engine
Detects system ffmpeg/ffprobe, handles local binaries, and provides auto-downloading.
"""

import os
import sys
import shutil
import subprocess
import urllib.request
import zipfile
import tempfile
import re
from pathlib import Path
from typing import Optional, Tuple, Dict, Any, Callable

# Common windows install paths to check
COMMON_WINDOWS_PATHS = [
    Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "WinGet" / "Links",
    Path("C:/ProgramData/chocolatey/bin"),
    Path(os.environ.get("USERPROFILE", "")) / "scoop" / "shims",
    Path("C:/ffmpeg/bin"),
    Path("C:/Program Files/ffmpeg/bin"),
]

def find_binary(name: str) -> Optional[Path]:
    """Find binary in PATH, current folder, local bin folder, or common paths."""
    exe_name = f"{name}.exe" if sys.platform == "win32" else name
    
    # 1. Check local directory and ./bin
    local_candidates = [
        Path.cwd() / exe_name,
        Path.cwd() / "bin" / exe_name,
        Path(__file__).parent.parent / exe_name,
        Path(__file__).parent.parent / "bin" / exe_name,
    ]
    for cand in local_candidates:
        if cand.is_file():
            return cand.resolve()
            
    # 2. Check system PATH via shutil.which
    which_path = shutil.which(name)
    if which_path:
        return Path(which_path).resolve()
        
    # 3. Check common Windows paths
    if sys.platform == "win32":
        for folder in COMMON_WINDOWS_PATHS:
            cand = folder / exe_name
            if cand.is_file():
                return cand.resolve()
                
    # 4. Check imageio_ffmpeg if installed
    try:
        import imageio_ffmpeg
        if name == "ffmpeg":
            exe = imageio_ffmpeg.get_ffmpeg_exe()
            if exe and os.path.isfile(exe):
                return Path(exe).resolve()
    except Exception:
        pass
        
    return None


def get_ffmpeg_and_ffprobe() -> Tuple[Optional[Path], Optional[Path]]:
    """Return paths to ffmpeg and ffprobe binaries."""
    ffmpeg = find_binary("ffmpeg")
    ffprobe = find_binary("ffprobe")
    
    # If ffmpeg found in a bin folder, look for sibling ffprobe
    if ffmpeg and not ffprobe:
        sibling_probe = ffmpeg.parent / ("ffprobe.exe" if sys.platform == "win32" else "ffprobe")
        if sibling_probe.is_file():
            ffprobe = sibling_probe.resolve()
            
    return ffmpeg, ffprobe


def download_ffmpeg(target_dir: Optional[Path] = None, progress_callback: Optional[Callable[[int, int], None]] = None) -> Tuple[Path, Path]:
    """
    Download official static FFmpeg Essentials build for Windows and extract to target_dir.
    Returns (ffmpeg_path, ffprobe_path).
    """
    if target_dir is None:
        target_dir = Path(__file__).parent.parent / "bin"
    target_dir.mkdir(parents=True, exist_ok=True)
    
    # Check if already downloaded
    ffmpeg_exe = target_dir / ("ffmpeg.exe" if sys.platform == "win32" else "ffmpeg")
    ffprobe_exe = target_dir / ("ffprobe.exe" if sys.platform == "win32" else "ffprobe")
    if ffmpeg_exe.is_file() and ffprobe_exe.is_file():
        return ffmpeg_exe, ffprobe_exe
        
    if sys.platform != "win32":
        raise RuntimeError(
            "Automatic download is configured for Windows. Please install ffmpeg via your package manager (e.g. apt, brew)."
        )
        
    url = "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"
    print(f"\n[FFmpeg Downloader] Downloading FFmpeg from {url}...")
    print("This is a one-time download (~90MB) to enable lossless video processing.\n")
    
    tmp_obj = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
    try:
        tmpdir = tmp_obj.name
        zip_path = Path(tmpdir) / "ffmpeg.zip"
        
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) VideoJoiner/1.0"}
        )
        with urllib.request.urlopen(req) as resp, open(zip_path, "wb") as out_f:
            total_size = int(resp.headers.get("Content-Length", 0))
            block_size = 1024 * 64
            downloaded = 0
            while True:
                chunk = resp.read(block_size)
                if not chunk:
                    break
                out_f.write(chunk)
                downloaded += len(chunk)
                if progress_callback:
                    progress_callback(downloaded, total_size)
                else:
                    if total_size > 0:
                        pct = min(100.0, downloaded * 100.0 / total_size)
                        bar = "█" * int(pct // 4) + "░" * (25 - int(pct // 4))
                        print(f"\r  [{bar}] {pct:5.1f}% ({downloaded/(1024*1024):.1f}MB / {total_size/(1024*1024):.1f}MB)", end="", flush=True)
        print("\n  Download complete. Extracting binaries...")
        
        with zipfile.ZipFile(zip_path, "r") as zf:
            for member in zf.namelist():
                filename = os.path.basename(member)
                if filename.lower() in ("ffmpeg.exe", "ffprobe.exe"):
                    dest_file = target_dir / filename
                    with zf.open(member) as source, open(dest_file, "wb") as f_out:
                        shutil.copyfileobj(source, f_out)
    finally:
        try:
            tmp_obj.cleanup()
        except Exception:
            pass
        
    print(f"  [OK] FFmpeg installed locally to: {target_dir.resolve()}\n")
    return target_dir / "ffmpeg.exe", target_dir / "ffprobe.exe"


def ensure_ffmpeg(auto_prompt: bool = True) -> Tuple[Path, Path]:
    """
    Ensure ffmpeg and ffprobe are available. If missing, prompt or auto-download.
    """
    ffmpeg, ffprobe = get_ffmpeg_and_ffprobe()
    if ffmpeg and ffprobe:
        return ffmpeg, ffprobe
        
    if sys.platform == "win32":
        if auto_prompt:
            print("\n" + "=" * 60)
            print("  FFmpeg was not detected on your system.")
            print("  FFmpeg is required to inspect and join videos losslessly.")
            print("=" * 60)
            choice = input("  Would you like to auto-download official FFmpeg now? [Y/n]: ").strip().lower()
            if choice in ("", "y", "yes"):
                return download_ffmpeg()
            else:
                raise SystemExit("FFmpeg is required. Please install it or place ffmpeg.exe in the script folder.")
        else:
            return download_ffmpeg()
    else:
        raise RuntimeError("FFmpeg and ffprobe must be installed on your system PATH.")


def parse_time_to_seconds(timestr: str) -> float:
    """Parse HH:MM:SS.micro or SS to float seconds."""
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

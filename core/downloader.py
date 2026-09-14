"""
Google Drive & URL Downloader Engine
Parses Google Drive links, extracts file/folder IDs, and downloads videos with resume,
large-file confirmation, folder batch-downloading, and automatic format detection.
"""

import os
import re
import sys
import tempfile
import urllib.parse
from pathlib import Path
from typing import List, Optional, Callable, Dict, Any, Tuple

from core.sorter import SUPPORTED_EXTENSIONS

# Try importing requests and gdown
try:
    import requests
except ImportError:
    requests = None

try:
    import gdown
except ImportError:
    gdown = None


def extract_gdrive_id(url: str) -> Optional[str]:
    """
    Extract Google Drive File or Folder ID from various URL formats.
    Supported patterns:
    - https://drive.google.com/file/d/FILE_ID/view?usp=sharing
    - https://drive.google.com/open?id=FILE_ID
    - https://drive.google.com/uc?id=FILE_ID&export=download
    - https://drive.google.com/drive/folders/FOLDER_ID
    - https://drive.google.com/drive/u/0/folders/FOLDER_ID
    - Raw ID string (alphanumeric, length >= 25)
    """
    url = url.strip()
    
    # 1. /file/d/ID/ or /d/ID/ or /folders/ID
    m = re.search(r"(?:/file/d/|/d/|/folders/)([a-zA-Z0-9_-]{25,})", url)
    if m:
        return m.group(1)
        
    # 2. ?id=ID or &id=ID
    m = re.search(r"[?&]id=([a-zA-Z0-9_-]{25,})", url)
    if m:
        return m.group(1)
        
    # 3. Direct raw ID string (alphanumeric with dash/underscore, length >= 25)
    if re.match(r"^[a-zA-Z0-9_-]{25,}$", url):
        return url
        
    return None


def is_gdrive_folder(url: str) -> bool:
    """Check if the URL points to a Google Drive folder."""
    return bool(re.search(r"(?:/drive/(?:u/\d+/)?folders/|/folders/)", url.strip()))


def extract_urls_from_text(text: str) -> List[str]:
    """
    Extract video and Google Drive URLs from multiline, comma-separated,
    semicolon-separated, or space-separated strings.
    """
    if not text:
        return []
        
    urls: List[str] = []
    # Normalize common separators
    normalized = text.replace(",", "\n").replace(";", "\n")
    for raw_line in normalized.splitlines():
        line = raw_line.strip().strip('"').strip("'").strip()
        if not line or line.startswith("#") or line.startswith("//"):
            continue
            
        # Support space-separated items on single line
        tokens = line.split()
        for token in tokens:
            token = token.strip().strip('"').strip("'").strip()
            if not token:
                continue
            if token.startswith("http://") or token.startswith("https://") or extract_gdrive_id(token):
                if token not in urls:
                    urls.append(token)
                    
    return urls


def parse_links_file(file_path: Path) -> List[str]:
    """
    Read URLs from a text file, ignoring comments (#) and blank lines.
    """
    if not file_path.is_file():
        raise FileNotFoundError(f"Links file not found: {file_path}")
        
    content = file_path.read_text(encoding="utf-8", errors="replace")
    return extract_urls_from_text(content)


def detect_video_extension(path: Path) -> str:
    """
    Determine appropriate video file extension by inspecting container magic bytes.
    Defaults to '.mp4' if unrecognized video stream.
    """
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
    """
    Ensure the downloaded file has a valid video file extension recognized by the joiner.
    If extension is missing or generic (e.g. .bin, .tmp), auto-detects container format
    and renames the file accordingly.
    """
    ext = file_path.suffix.lower()
    if ext in SUPPORTED_EXTENSIONS:
        return file_path
        
    detected_ext = detect_video_extension(file_path) or default_ext
    target_path = file_path.with_name(f"{file_path.name}{detected_ext}")
    
    # If target already exists, append unique counter
    counter = 1
    while target_path.exists() and target_path != file_path:
        target_path = file_path.with_name(f"{file_path.stem}_{counter}{detected_ext}")
        counter += 1
        
    try:
        file_path.rename(target_path)
        return target_path
    except Exception:
        return file_path


def get_filename_from_cd(cd_header: Optional[str]) -> Optional[str]:
    """Extract filename from Content-Disposition header."""
    if not cd_header:
        return None
    m = re.search(r'filename\*=UTF-8\'\'([^;]+)', cd_header, re.IGNORECASE)
    if m:
        return urllib.parse.unquote(m.group(1))
    m = re.search(r'filename="?([^";]+)"?', cd_header, re.IGNORECASE)
    if m:
        return m.group(1).strip()
    return None


def download_with_gdown(
    url_or_id: str,
    dest_dir: Path,
    index: int = 1,
    quiet: bool = False
) -> Path:
    """
    Download single Google Drive file using gdown package, preserving
    remote file names and valid video extensions.
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    file_id = extract_gdrive_id(url_or_id)
    url = f"https://drive.google.com/uc?id={file_id}" if file_id else url_or_id
    
    # Destination ending in separator tells gdown to preserve remote filename in dest_dir
    dest_param = str(dest_dir.resolve()) + os.sep
    
    res = None
    err_notes = []
    try:
        res = gdown.download(url=url, output=dest_param, quiet=quiet)
    except Exception as e:
        err_notes.append(str(e))
        
    if (not res or not Path(res).is_file()) and file_id:
        try:
            res = gdown.download(id=file_id, output=dest_param, quiet=quiet)
        except Exception as e:
            err_notes.append(str(e))
            
    if not res or not Path(res).is_file():
        fallback_file = dest_dir / f"drive_video_{index:02d}.mp4"
        try:
            res = gdown.download(url=url, output=str(fallback_file), quiet=quiet)
        except Exception as e:
            err_notes.append(str(e))
        
    if not res or not Path(res).is_file():
        full_err = " | ".join(err_notes)
        if "Cannot retrieve the public link" in full_err or "permission" in full_err.lower():
            raise PermissionError(
                f"Google Drive access restricted for: {url_or_id}\n"
                f"File is PRIVATE or requires Google Sign-in.\n"
                f"👉 Fix: In Google Drive, right-click file -> Share -> Change 'General access' to 'Anyone with the link' (Viewer)."
            )
        raise RuntimeError(f"Failed to download Google Drive file: {url_or_id} ({full_err})")
        
    res_path = Path(res).resolve()
    return ensure_video_extension(res_path)


def download_gdrive_folder(
    url: str,
    dest_dir: Path,
    quiet: bool = False
) -> List[Path]:
    """
    Download all video files from a public Google Drive folder using gdown.
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    if not gdown:
        raise ImportError("The 'gdown' package is required to download Google Drive folders.")
        
    folder_id = extract_gdrive_id(url)
    print(f"\n📁 Batch downloading Google Drive shared folder: {url} (ID: {folder_id or 'N/A'})...")
    
    folder_dest = dest_dir / f"folder_{folder_id or 'shared'}"
    folder_dest.mkdir(parents=True, exist_ok=True)
    
    res_list = None
    try:
        res_list = gdown.download_folder(url=url, output=str(folder_dest.resolve()), quiet=quiet)
    except Exception as e:
        if folder_id:
            try:
                res_list = gdown.download_folder(id=folder_id, output=str(folder_dest.resolve()), quiet=quiet)
            except Exception as e2:
                print(f"⚠️ Failed to download folder: {e2}")
        else:
            print(f"⚠️ Failed to download folder: {e}")
            
    # Collect all video files downloaded in folder_dest
    found_videos: List[Path] = []
    for p in folder_dest.rglob("*"):
        if p.is_file():
            p_fixed = ensure_video_extension(p)
            if p_fixed.suffix.lower() in SUPPORTED_EXTENSIONS and p_fixed not in found_videos:
                found_videos.append(p_fixed)
                
    print(f"✓ Retrieved {len(found_videos)} video files from Google Drive folder.")
    return found_videos


def download_with_requests(
    url_or_id: str,
    dest_dir: Path,
    index: int = 1,
    progress_callback: Optional[Callable[[int, int, str], None]] = None
) -> Path:
    """
    Resilient Google Drive downloader using requests.Session with
    virus scan confirmation token handling, permission validation, and streaming.
    """
    if not requests:
        raise ImportError("The 'requests' package is required. Install via: pip install requests")
        
    file_id = extract_gdrive_id(url_or_id)
    if not file_id:
        download_url = url_or_id
    else:
        download_url = f"https://drive.google.com/uc?id={file_id}&export=download"

    session = requests.Session()
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }
    
    response = session.get(download_url, headers=headers, stream=True, allow_redirects=True)

    # Check for authentication redirect (Restricted / Private file)
    if "accounts.google.com" in response.url or "/signin" in response.url or "ServiceLogin" in response.url:
        raise PermissionError(
            f"Google Drive access restricted for: {url_or_id}\n"
            f"File is PRIVATE or requires Google Sign-in.\n"
            f"👉 Fix: In Google Drive, right-click file -> Share -> Change 'General access' to 'Anyone with the link' (Viewer)."
        )

    content_type = response.headers.get("content-type", "").lower()
    if "text/html" in content_type:
        html_text = next(response.iter_content(65536), b"").decode("utf-8", errors="replace")

        # 1. Check for sign-in / restricted access
        if "accounts.google.com" in response.url or "accounts.google.com" in html_text or "ServiceLogin" in html_text:
            raise PermissionError(
                f"Google Drive access restricted for: {url_or_id}\n"
                f"File is PRIVATE or requires Google Sign-in.\n"
                f"👉 Fix: In Google Drive, right-click file -> Share -> Change 'General access' to 'Anyone with the link' (Viewer)."
            )

        # 2. Check for quota exceeded
        if "quota" in html_text.lower() or "too many users" in html_text.lower():
            raise RuntimeError(f"Google Drive download quota exceeded for: {url_or_id}")

        # 3. Check for Google Drive Virus Scan Warning form (for files > 100MB)
        # Google provides a <form id="download-form" action="https://drive.usercontent.google.com/download" method="get">
        # with hidden inputs: id, export, confirm, uuid
        form_inputs = {}
        for m in re.finditer(r'<input[^>]+name="([^"]+)"[^>]+value="([^"]*)"', html_text):
            form_inputs[m.group(1)] = m.group(2)

        action_match = re.search(r'<form[^>]+action="([^"]+)"', html_text)
        action_url = action_match.group(1) if action_match else "https://drive.usercontent.google.com/download"

        if form_inputs and "confirm" in form_inputs:
            response = session.get(action_url, params=form_inputs, headers=headers, stream=True, allow_redirects=True)
        else:
            # Fallback legacy token search
            m_token = re.search(r'confirm=([0-9A-Za-z_-]+)', html_text)
            if m_token and file_id:
                token = m_token.group(1)
                confirm_url = f"https://drive.usercontent.google.com/download?id={file_id}&export=download&confirm={token}"
                response = session.get(confirm_url, headers=headers, stream=True, allow_redirects=True)
            else:
                raise RuntimeError(
                    f"Google Drive returned an HTML page instead of video data for: {url_or_id}\n"
                    f"Please verify the file sharing permission is set to 'Anyone with the link'."
                )

    # Determine filename
    cd = response.headers.get("content-disposition", "")
    filename = get_filename_from_cd(cd)
    
    if not filename:
        parsed = urllib.parse.urlparse(url_or_id)
        path_name = os.path.basename(parsed.path)
        if path_name and "." in path_name:
            filename = path_name
        else:
            filename = f"drive_video_{index:02d}.mp4"

    dest_path = dest_dir / filename
    total_size = int(response.headers.get("content-length", 0))
    
    downloaded = 0
    chunk_size = 1024 * 128  # 128 KB chunks
    
    try:
        with open(dest_path, "wb") as f:
            for chunk in response.iter_content(chunk_size=chunk_size):
                if chunk:
                    f.write(chunk)
                    downloaded += len(chunk)
                    if progress_callback:
                        progress_callback(downloaded, total_size, filename)
    except Exception:
        if dest_path.exists():
            dest_path.unlink(missing_ok=True)
        raise

    # Validation: Ensure file is not 0 bytes and not an HTML error document
    if not dest_path.is_file() or dest_path.stat().st_size == 0:
        if dest_path.exists():
            dest_path.unlink(missing_ok=True)
        raise RuntimeError(f"Download produced an empty file (0 bytes): {url_or_id}")

    with open(dest_path, "rb") as f:
        magic = f.read(512)
    if magic.strip().startswith(b"<!DOCTYPE") or magic.strip().startswith(b"<html") or b"<head>" in magic.lower():
        dest_path.unlink(missing_ok=True)
        raise PermissionError(
            f"Google Drive returned an HTML page instead of video data for: {url_or_id}\n"
            f"The file is PRIVATE or requires Google account sign-in.\n"
            f"👉 Fix: Set file sharing to 'Anyone with the link' (Viewer) in Google Drive."
        )

    return ensure_video_extension(dest_path)


def download_video(
    url_or_id: str,
    dest_dir: Path,
    index: int = 1,
    progress_callback: Optional[Callable[[int, int, str], None]] = None
) -> Path:
    """
    Download video from Google Drive or direct URL into dest_dir.
    Uses gdown first if available, falls back to requests.
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    file_id = extract_gdrive_id(url_or_id)
    last_err: Optional[Exception] = None
    
    # If gdown is available and we have a Google Drive ID, try gdown
    if gdown and file_id:
        try:
            res_path = download_with_gdown(url_or_id, dest_dir=dest_dir, index=index, quiet=False)
            if res_path.is_file():
                if progress_callback:
                    sz = res_path.stat().st_size
                    progress_callback(sz, sz, res_path.name)
                return res_path
        except PermissionError:
            raise
        except Exception as e:
            last_err = e
            print(f"⚠️ gdown attempt note: {e}, falling back to requests session...")
            
    # Fallback to requests session
    try:
        return download_with_requests(
            url_or_id=url_or_id,
            dest_dir=dest_dir,
            index=index,
            progress_callback=progress_callback
        )
    except Exception as req_err:
        if last_err and isinstance(req_err, PermissionError):
            raise req_err
        raise req_err


def download_all_videos(
    urls: List[str],
    dest_dir: Path,
    overall_callback: Optional[Callable[[int, int, str], None]] = None
) -> List[Path]:
    """
    Download a sequence of videos from a list of URLs (supporting file links and folder links).
    Returns list of downloaded file paths.
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    downloaded_files: List[Path] = []
    failed_downloads: List[Tuple[str, str]] = []
    total_count = len(urls)

    for i, url in enumerate(urls, start=1):
        if is_gdrive_folder(url):
            if overall_callback:
                overall_callback(i, total_count, f"Downloading Google Drive folder [{i}/{total_count}]...")
            try:
                folder_vids = download_gdrive_folder(url, dest_dir=dest_dir)
                downloaded_files.extend(folder_vids)
            except Exception as e:
                failed_downloads.append((url, str(e)))
        else:
            if overall_callback:
                overall_callback(i, total_count, f"Starting download {i}/{total_count}...")
                
            def item_progress(curr, tot, fname):
                if overall_callback:
                    overall_callback(i, total_count, f"Downloading [{i}/{total_count}]: {fname}")

            try:
                p = download_video(
                    url_or_id=url,
                    dest_dir=dest_dir,
                    index=len(downloaded_files) + 1,
                    progress_callback=item_progress
                )
                if p and p.is_file() and p not in downloaded_files:
                    downloaded_files.append(p)
            except Exception as e:
                print(f"❌ Error downloading [{i}/{total_count}]: {e}")
                failed_downloads.append((url, str(e)))

    if failed_downloads:
        print("\n" + "=" * 62)
        print("❌ DOWNLOAD ERRORS DETECTED:")
        for u, err in failed_downloads:
            print(f"  • {u}\n    {err}")
        print("=" * 62 + "\n")
        raise RuntimeError(f"{len(failed_downloads)} download(s) failed. See error details above.")

    return downloaded_files

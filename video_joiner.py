#!/usr/bin/env python3
"""
🎬 Advanced Video Joiner
High-performance, quality-preserving video joiner supporting .mp4, .ts, .mkv, .mov, and more.
Includes lossless direct stream copy, TS protocol joining, and high-fidelity smart transcoding.

Usage Examples:
    # 1. Join specific files losslessly:
    python video_joiner.py -i part1.ts part2.mp4 part3.mkv -o combined.mp4

    # 2. Join an entire folder with natural sorting (part1, part2, ... part10):
    python video_joiner.py -d ./my_clips/ -o output.mkv

    # 3. Interactive CLI mode (guided wizard):
    python video_joiner.py

    # 4. Download from Google Drive and merge:
    python video_joiner.py -g links.txt -o combined.mp4
"""

import sys
import os
import argparse
from pathlib import Path
from typing import List, Optional

# Add package root to sys.path
SCRIPT_DIR = Path(__file__).parent.resolve()
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from core.ffmpeg_utils import ensure_ffmpeg, download_ffmpeg, get_ffmpeg_and_ffprobe
from core.probe import probe_file, analyze_compatibility, MediaFileInfo, CompatibilityAnalysis
from core.joiner import VideoJoiner, JoinProgress
from core.sorter import filter_video_files, sort_files, SUPPORTED_EXTENSIONS
from core.downloader import download_all_videos, parse_links_file, download_video, extract_urls_from_text


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
    if hrs > 0:
        return f"{hrs:02d}:{mins:02d}:{secs:02d}"
    return f"{mins:02d}:{secs:02d}"


def format_size(size_bytes: int) -> str:
    """Format bytes into MB or GB."""
    mb = size_bytes / (1024 * 1024)
    if mb >= 1024:
        return f"{mb / 1024:.2f} GB"
    return f"{mb:.1f} MB"


def print_stream_table(files: List[MediaFileInfo], analysis: CompatibilityAnalysis):
    """Print an attractive summary table of probed media streams."""
    print("\n" + "─" * 94)
    print(f"{'#':<3} | {'File Name':<28} | {'Resolution':<11} | {'Video':<8} | {'FPS':<7} | {'Audio':<14} | {'Duration':<9} | {'Size':<8}")
    print("─" * 94)
    
    for idx, f in enumerate(files, start=1):
        name = f.path.name
        if len(name) > 28:
            name = name[:25] + "..."
            
        if f.error:
            print(f"{idx:<3} | {name:<28} | ERROR: {f.error}")
            continue
            
        v = f.video
        a = f.audio
        res = f"{v.width}x{v.height}" if (v and v.width) else "N/A"
        v_codec = v.codec if v else "None"
        fps = f"{v.fps:.2f}" if (v and v.fps) else "N/A"
        
        if a:
            sr_k = f"{a.sample_rate // 1000}k" if a.sample_rate else ""
            a_codec = f"{a.codec} {sr_k} {a.channels}ch".strip()
        else:
            a_codec = "No Audio"
            
        dur = format_duration(f.duration)
        sz = format_size(f.size_bytes)
        
        print(f"{idx:<3} | {name:<28} | {res:<11} | {v_codec:<8} | {fps:<7} | {a_codec:<14} | {dur:<9} | {sz:<8}")
        
    print("─" * 94)
    print(f"Total Files: {len(files)} | Total Duration: {format_duration(analysis.total_duration)} | Total Size: {format_size(analysis.total_size_bytes)}")
    print("─" * 94)
    
    if analysis.is_lossless_ready:
        print("  🟢 STATUS: 100% LOSSLESS READY!")
        print("     All video codecs, resolutions, frame rates, and audio parameters match.")
        print("     Zero re-encoding required -> 100% original quality preserved with blazing speed!\n")
    else:
        print("  🟡 STATUS: STREAM VARIATION DETECTED")
        for reason in analysis.reasons_against_copy:
            print(f"     • {reason}")
        print("     Intact-Quality Transcode mode will be used to harmonize streams without quality loss.\n")


def make_cli_progress_callback():
    """Create terminal progress bar renderer."""
    last_len = [0]

    def callback(p: JoinProgress):
        bar_width = 30
        filled = int(bar_width * (p.percent / 100.0))
        bar = "█" * filled + "░" * (bar_width - filled)
        
        cur_t = format_duration(p.current_time_sec)
        tot_t = format_duration(p.total_duration_sec)
        
        eta_str = f"ETA {int(p.eta_sec)}s" if p.eta_sec > 0 else "Finishing..."
        speed_str = f"Speed: {p.speed}" if p.speed else ""
        
        line = f"\r  [{bar}] {p.percent:5.1f}% ({cur_t} / {tot_t}) | {speed_str} | {eta_str}"
        
        pad = max(0, last_len[0] - len(line))
        sys.stdout.write(line + " " * pad)
        sys.stdout.flush()
        last_len[0] = len(line)

    return callback


def write_github_step_summary(
    files: List[MediaFileInfo],
    analysis: CompatibilityAnalysis,
    output_path: Path,
    mode_used: str,
    success: bool,
    error_msg: str = ""
):
    """Write execution metrics to GitHub Step Summary if running in GitHub Actions."""
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
                    f.write("> [!NOTE]\n")
                    f.write("> **Quality Guarantee: 100% Lossless Direct Stream Copy** (`-c copy`).\n")
                    f.write("> Video and audio packets were concatenated directly with zero re-encoding and zero quality degradation.\n\n")
                else:
                    f.write("> [!IMPORTANT]\n")
                    f.write("> **Visually Lossless Transcode** was applied because input streams had differing resolutions/codecs.\n\n")
            else:
                f.write("### ❌ Error: Video Joining Failed\n\n")
                if error_msg:
                    f.write(f"```text\n{error_msg}\n```\n\n")

            f.write("### 📊 Input Video Clips\n\n")
            f.write("| # | File Name | Resolution | Video Codec | FPS | Audio | Duration | Size |\n")
            f.write("|---|-----------|------------|-------------|-----|-------|----------|------|\n")
            for idx, item in enumerate(files, start=1):
                v = item.video
                a = item.audio
                res = f"{v.width}x{v.height}" if (v and v.width) else "N/A"
                v_codec = v.codec if v else "None"
                fps = f"{v.fps:.2f}" if (v and v.fps) else "N/A"
                a_codec = f"{a.codec} {a.channels}ch" if a else "No Audio"
                f.write(f"| {idx} | `{item.path.name}` | {res} | {v_codec} | {fps} | {a_codec} | {format_duration(item.duration)} | {format_size(item.size_bytes)} |\n")
            f.write("\n")
    except Exception as e:
        print(f"Notice: Could not write GitHub step summary: {e}")


def execute_join(
    files: List[Path],
    output_path: Path,
    mode: str = "auto",
    crf: int = 17,
    preset: str = "slow",
    sort_mode: str = "natural",
    reverse_sort: bool = False,
    overwrite: bool = False
) -> bool:
    """Execute the full video joining workflow."""
    print(BANNER)
    is_ci = bool(os.environ.get("CI") or os.environ.get("GITHUB_ACTIONS"))
    
    # 1. Check & Ensure FFmpeg
    ffmpeg_exe, ffprobe_exe = ensure_ffmpeg(auto_prompt=not is_ci)
    
    # 2. Filter & Sort Files
    valid_files = filter_video_files(files)
    if not valid_files:
        print("❌ Error: No supported video files found to join.")
        return False
        
    if len(valid_files) < 2:
        print(f"❌ Error: At least 2 video files are required to join. Found {len(valid_files)}.")
        return False
        
    sorted_paths = sort_files(valid_files, sort_mode=sort_mode, reverse=reverse_sort)
    
    print(f"🔍 Probing {len(sorted_paths)} media files...")
    probed_files: List[MediaFileInfo] = []
    for p in sorted_paths:
        info = probe_file(p, ffprobe_exe, ffmpeg_exe)
        probed_files.append(info)
        
    # Check if any probing error
    errors = [f for f in probed_files if f.error]
    if errors:
        for err in errors:
            print(f"  ❌ Error probing {err.path.name}: {err.error}")
        return False

    # 3. Analyze Compatibility
    analysis = analyze_compatibility(probed_files)
    print_stream_table(probed_files, analysis)
    
    # 4. Check Output File
    output_path = output_path.resolve()
    if output_path.is_file() and not overwrite and not is_ci:
        ans = input(f"Output file '{output_path.name}' already exists. Overwrite? [y/N]: ").strip().lower()
        if ans not in ("y", "yes"):
            print("Operation aborted by user.")
            return False

    # 5. Determine Mode
    joiner = VideoJoiner(ffmpeg_exe, ffprobe_exe)
    progress_cb = make_cli_progress_callback()
    
    use_lossless = False
    if mode == "copy":
        use_lossless = True
        print("⚡ Mode: Forced Lossless Stream Copy (-c copy)")
    elif mode == "transcode":
        use_lossless = False
        print(f"🎨 Mode: High-Fidelity Intact Transcode (CRF {crf}, preset: {preset})")
    else:  # auto
        if analysis.is_lossless_ready:
            use_lossless = True
            print("⚡ Mode: Auto -> Lossless Stream Copy (Zero quality loss, fastest)")
        else:
            use_lossless = False
            print(f"🎨 Mode: Auto -> Visually Lossless Transcode (CRF {crf}, preset: {preset})")

    print(f"\n🚀 Processing: Joining into '{output_path.name}'...")
    
    success = False
    err_msg = ""
    mode_reported = "Lossless Stream Copy (-c copy)" if use_lossless else f"Visually Lossless Transcode (CRF {crf})"
    
    if use_lossless:
        success, err_msg = joiner.join_lossless_smart(probed_files, output_path, progress_callback=progress_cb)
            
        # If lossless copy failed in auto mode, fallback to transcode automatically
        if not success and mode == "auto":
            print(f"\n⚠️  Lossless stream copy encountered container/stream incompatibility: {err_msg}")
            print("🔄 Seamlessly falling back to High-Fidelity Intact Transcode...")
            mode_reported = f"Fallback Visually Lossless Transcode (CRF {crf})"
            success, err_msg = joiner.join_visually_lossless_transcode(
                probed_files, analysis, output_path, crf=crf, preset=preset, progress_callback=progress_cb
            )
    else:
        success, err_msg = joiner.join_visually_lossless_transcode(
            probed_files, analysis, output_path, crf=crf, preset=preset, progress_callback=progress_cb
        )

    print()  # newline after progress bar
    
    # Write summary for GitHub Actions
    write_github_step_summary(
        files=probed_files,
        analysis=analysis,
        output_path=output_path,
        mode_used=mode_reported,
        success=success,
        error_msg=err_msg
    )
    
    if success and output_path.is_file():
        out_size = format_size(output_path.stat().st_size)
        print("\n" + "═" * 60)
        print("  🎉 SUCCESS! Videos joined successfully.")
        print(f"  📁 Output: {output_path.resolve()}")
        print(f"  📊 Final Size: {out_size}")
        print("═" * 60 + "\n")
        return True
    else:
        print("\n" + "═" * 60)
        print("  ❌ JOINING FAILED")
        print(f"  FFmpeg Error:\n{err_msg}")
        print("═" * 60 + "\n")
        return False


def run_interactive_wizard():
    """Interactive guided terminal wizard."""
    print(BANNER)
    ffmpeg_exe, ffprobe_exe = ensure_ffmpeg(auto_prompt=True)
    
    print("Welcome to the Video Joiner Interactive Wizard!\n")
    print("How would you like to provide input files?")
    print("  [1] Select a local folder containing video clips")
    print("  [2] Select files using Windows File Dialog")
    print("  [3] 📥 Download and merge from Google Drive links file (.txt)")
    print("  [4] 🌐 Paste Google Drive links directly")
    print("  [5] Type local file paths manually")
    
    choice = input("\nChoose an option [1-5] (default 1): ").strip()
    
    files: List[Path] = []
    download_dir = Path.cwd() / "downloads"
    is_downloaded = False
        
    elif choice == "3":
        txt_path = input("Enter path to .txt file with Google Drive links [default: example_links.txt]: ").strip().strip('"').strip("'")
        if not txt_path:
            txt_path = "example_links.txt"
        p_txt = Path(txt_path)
        if not p_txt.is_file():
            print(f"❌ Error: File not found: {p_txt}")
            return
        urls = parse_links_file(p_txt)
        if not urls:
            print(f"❌ Error: No valid URLs found in {p_txt}")
            return
        print(f"\n📥 Found {len(urls)} links. Downloading to '{download_dir.name}/'...")
        files = download_all_videos(urls, dest_dir=download_dir)
        is_downloaded = True

    elif choice == "4":
        print("\nEnter Google Drive links one by one (press Enter on empty line to finish):")
        urls = []
        while True:
            line = input("  Google Drive URL: ").strip().strip('"').strip("'")
            if not line:
                break
            urls.append(line)
        if not urls:
            print("No URLs entered.")
            return
        print(f"\n📥 Downloading {len(urls)} videos to '{download_dir.name}/'...")
        files = download_all_videos(urls, dest_dir=download_dir)
        is_downloaded = True

    elif choice == "2":
        try:
            import tkinter as tk
            from tkinter import filedialog
            root = tk.Tk()
            root.withdraw()
            selected = filedialog.askopenfilenames(
                title="Select Videos to Join",
                filetypes=[("Video Files", "*.mp4 *.ts *.mkv *.mov *.avi *.webm *.m4v *.mts"), ("All Files", "*.*")]
            )
            root.destroy()
            if selected:
                files = [Path(p) for p in selected]
        except Exception as e:
            print(f"Could not open file dialog: {e}")
            
    elif choice == "5":
        print("\nEnter video file paths one by one (press Enter on empty line to finish):")
        while True:
            line = input("  File path: ").strip().strip('"').strip("'")
            if not line:
                break
            p = Path(line)
            if p.is_file():
                files.append(p)
            else:
                print(f"    ⚠️ File not found: {line}")
                
    else:  # default 1: folder
        folder_str = input("\nEnter folder path (or press Enter for current folder): ").strip().strip('"').strip("'")
        folder = Path(folder_str) if folder_str else Path.cwd()
        if not folder.is_dir():
            print(f"❌ Error: '{folder}' is not a valid directory.")
            return
        candidates = [p for p in folder.iterdir() if p.is_file()]
        files = filter_video_files(candidates)
        print(f"Found {len(files)} supported video files in '{folder.name}'.")

    if len(files) < 2:
        print(f"❌ Need at least 2 video files to join. Found {len(files)}.")
        return

    # Natural sort
    files = sort_files(files, sort_mode="natural")
    
    # Output file
    default_name = f"joined_video{files[0].suffix.lower() if files[0].suffix else '.mp4'}"
    out_name = input(f"\nEnter output filename [default: {default_name}]: ").strip()
    if not out_name:
        out_name = default_name
    output_path = Path.cwd() / out_name

    # Mode
    print("\nSelect Joining Mode:")
    print("  [1] Smart Auto (Lossless copy if matching, high-fidelity transcode if different) [RECOMMENDED]")
    print("  [2] Force Lossless Copy (100% mathematical zero loss; fails if codecs differ)")
    print("  [3] High-Fidelity Intact Transcode (Harmonize mismatched resolutions/codecs with CRF 17)")
    mode_choice = input("Choice [1-3] (default 1): ").strip()
    
    mode_map = {"1": "auto", "2": "copy", "3": "transcode"}
    mode = mode_map.get(mode_choice, "auto")

    success = execute_join(files=files, output_path=output_path, mode=mode)
    
    if success and is_downloaded:
        ans = input("\nDo you want to delete the downloaded temporary video files? [y/N]: ").strip().lower()
        if ans in ("y", "yes"):
            for f in files:
                try:
                    f.unlink()
                except Exception:
                    pass
            print("Downloaded temporary files removed.")


def main():
    parser = argparse.ArgumentParser(
        description="🎬 Advanced Video Joiner - Lossless & High Fidelity Concatenation for MP4, TS, MKV, and more (with Google Drive support).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # 1. Download Google Drive links from text file and join losslessly:
  python video_joiner.py -g links.txt -o merged.mp4

  # 2. Download from Google Drive URLs on command line:
  python video_joiner.py --urls "https://drive.google.com/file/d/ID1/view" "https://drive.google.com/file/d/ID2/view" -o merged.mp4

  # 3. Join local files:
  python video_joiner.py -i clip1.ts clip2.mp4 -o merged.mp4

  # 4. Join all videos in a directory:
  python video_joiner.py -d ./clips -o merged.mp4
        """
    )
    
    parser.add_argument("-i", "--input", nargs="+", help="Input video file paths or glob patterns.")
    parser.add_argument("-d", "--dir", help="Directory containing video clips to join.")
    parser.add_argument("-l", "--list", help="Text file containing one local video path per line.")
    parser.add_argument("-g", "--gdrive", help="Text file containing Google Drive links (downloads first, then merges).")
    parser.add_argument("--urls", nargs="+", help="One or more Google Drive or direct video URLs to download and merge.")
    parser.add_argument("--download-dir", default="downloads", help="Directory to save downloaded videos (default: ./downloads).")
    parser.add_argument("--clean-downloads", action="store_true", help="Remove downloaded video files after successful merge.")
    parser.add_argument("-o", "--output", default="joined_video.mp4", help="Output video path (default: joined_video.mp4).")
    parser.add_argument("-m", "--mode", choices=["auto", "copy", "transcode"], default="auto",
                        help="Join mode: 'auto' (smart lossless), 'copy' (force stream copy), 'transcode' (force CRF transcode).")
    parser.add_argument("--crf", type=int, default=17,
                        help="CRF quality factor for transcoding (0=lossless, 17=visually transparent, default: 17).")
    parser.add_argument("--preset", default="slow", choices=["ultrafast", "superfast", "veryfast", "faster", "fast", "medium", "slow", "slower", "veryslow"],
                        help="x264 encoding preset (default: slow).")
    parser.add_argument("--sort", choices=["natural", "alphabetical", "date", "size", "none"], default="natural",
                        help="Sorting mode for input files (default: natural).")
    parser.add_argument("--reverse", action="store_true", help="Reverse sort order.")
    parser.add_argument("-y", "--yes", action="store_true", help="Overwrite existing output file without confirmation.")
    parser.add_argument("--download-ffmpeg", action="store_true", help="Download official static FFmpeg Essentials build.")

    args = parser.parse_args()

    if args.download_ffmpeg:
        download_ffmpeg()
        return

    is_ci = bool(os.environ.get("CI") or os.environ.get("GITHUB_ACTIONS"))
    if is_ci:
        args.yes = True

    # Check if Google Drive URLs provided
    download_dir = Path(args.download_dir).resolve()
    input_paths: List[Path] = []
    is_downloaded = False

    if args.gdrive:
        gdrive_file = Path(args.gdrive)
        urls = parse_links_file(gdrive_file)
        print(f"📥 Found {len(urls)} links in {gdrive_file.name}. Starting download to {download_dir}...")
        input_paths = download_all_videos(urls, dest_dir=download_dir)
        is_downloaded = True

    elif args.urls:
        parsed_urls = []
        for item in args.urls:
            parsed_urls.extend(extract_urls_from_text(item))
        print(f"📥 Found {len(parsed_urls)} Google Drive URLs. Starting download to {download_dir}...")
        input_paths = download_all_videos(parsed_urls, dest_dir=download_dir)
        is_downloaded = True

    elif os.environ.get("GDRIVE_URLS") or os.environ.get("VIDEO_URLS"):
        env_val = os.environ.get("GDRIVE_URLS") or os.environ.get("VIDEO_URLS") or ""
        parsed_urls = extract_urls_from_text(env_val)
        if parsed_urls:
            print(f"📥 Detected {len(parsed_urls)} URLs from environment. Starting download to {download_dir}...")
            input_paths = download_all_videos(parsed_urls, dest_dir=download_dir)
            is_downloaded = True

    elif args.input:
        for item in args.input:
            p = Path(item)
            if "*" in item or "?" in item:
                parent = p.parent if p.parent != Path(".") else Path.cwd()
                pattern = p.name
                input_paths.extend(parent.glob(pattern))
            elif p.is_file():
                input_paths.append(p)
                
    elif args.dir:
        dir_path = Path(args.dir)
        if not dir_path.is_dir():
            print(f"❌ Error: Directory not found: {dir_path}")
            sys.exit(1)
        input_paths = [p for p in dir_path.iterdir() if p.is_file()]
        
    elif args.list:
        list_file = Path(args.list)
        if not list_file.is_file():
            print(f"❌ Error: List file not found: {list_file}")
            sys.exit(1)
        with open(list_file, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    p = Path(line)
                    if p.is_file():
                        input_paths.append(p)

    # If no inputs provided via CLI flags, launch interactive wizard or fail in CI
    if not input_paths:
        if is_ci:
            print("❌ Error: No input video files or Google Drive URLs provided in CI environment.")
            sys.exit(1)
        run_interactive_wizard()
        return

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
        print("🧹 Cleaning up downloaded raw video files...")
        for f in input_paths:
            try:
                f.unlink()
            except Exception:
                pass
        print("Done.")

    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()

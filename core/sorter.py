"""
Natural File Sorting and Filtering Engine
Handles proper numerical ordering (e.g. part1, part2, ... part10) and format filtering.
"""

import re
from pathlib import Path
from typing import List, Union

SUPPORTED_EXTENSIONS = {
    ".mp4", ".ts", ".mkv", ".mov", ".avi", ".webm",
    ".flv", ".wmv", ".m4v", ".mts", ".m2ts", ".vob", ".3gp"
}


def natural_sort_key(s: Union[str, Path]):
    """Key for natural alphanumeric sorting (e.g. video_2 comes before video_10)."""
    text = str(s.name if isinstance(s, Path) else s)
    return [int(c) if c.isdigit() else c.lower() for c in re.split(r"(\d+)", text)]


def filter_video_files(paths: List[Path]) -> List[Path]:
    """Filter files by supported video extensions."""
    return [p for p in paths if p.is_file() and p.suffix.lower() in SUPPORTED_EXTENSIONS]


def sort_files(files: List[Path], sort_mode: str = "natural", reverse: bool = False) -> List[Path]:
    """
    Sort files according to specified mode:
    - 'natural': Alphanumeric natural order (video_1, video_2, video_10)
    - 'alphabetical': Standard string sort
    - 'date': Modification time (oldest first)
    - 'size': File size (smallest first)
    """
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

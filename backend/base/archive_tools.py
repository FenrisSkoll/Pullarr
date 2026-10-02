"""Separate redistributable UnRAR reader from optional user-licensed RAR writer."""
from pathlib import Path
from shutil import which
from sys import platform


def archive_executable(*, write: bool = False) -> str:
    if write:
        executable = which('rar')
        if executable:
            return executable
        raise FileNotFoundError('RAR creation requires a separately installed licensed RAR tool')
    name = ('unrar_windows_64.exe' if platform == 'win32' else
            'unrar_linux_64' if platform.startswith('linux') else '')
    bundled = Path(__file__).resolve().parents[1] / 'lib' / name
    if name and bundled.is_file():
        return str(bundled)
    executable = which('unrar')
    if executable:
        return executable
    raise FileNotFoundError('CBR reading requires the UnRAR command-line utility')

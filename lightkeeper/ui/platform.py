"""Small desktop adapters; terminal clipboard fallback belongs to Textual."""
import shutil
import subprocess
import sys


def clipboard(value, fallback):
    commands = ([['pbcopy']] if sys.platform == 'darwin' else
                [['wl-copy'], ['xclip', '-selection', 'clipboard'], ['xsel', '--clipboard', '--input']])
    for command in commands:
        if shutil.which(command[0]):
            try:
                subprocess.run(command, input=value, encoding='utf-8', check=True, timeout=5,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                return True
            except (OSError, subprocess.SubprocessError):
                continue
    fallback(value)
    return False  # OSC 52 has no acknowledgement; also display the text for manual copying.


def open_folder(path):
    command = 'open' if sys.platform == 'darwin' else 'xdg-open'
    if not shutil.which(command):
        raise FileNotFoundError(command)
    subprocess.run([command, str(path)], check=True, timeout=5,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

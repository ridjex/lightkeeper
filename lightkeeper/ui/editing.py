"""Validated temp-copy editing; the Runner keeps running while Textual is suspended."""
import os
from pathlib import Path
import shlex
import subprocess
import tempfile
import threading

from ..settings import prepare_config_dir, refuse_symlink, write_lock


def nonempty_utf8(path: Path) -> None:
    if not path.read_text(encoding="utf-8").strip():
        raise ValueError("file must contain non-empty UTF-8 text")


def read_file(path: Path) -> bytes | None:
    refuse_symlink(path)
    return path.read_bytes() if path.exists() else None


def temporary_copy(path, content, config):
    prepare_config_dir(config)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.parent.chmod(0o700)
    fd, name = tempfile.mkstemp(prefix=f".{path.stem}-", suffix=path.suffix, dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    return temporary


def commit_file(path, temporary, original, validate, config):
    """Both editors validate and compare under the same final write lock."""
    temporary.chmod(0o600)
    validate(temporary)
    temporary.chmod(0o600)
    with temporary.open("rb") as stream:
        os.fsync(stream.fileno())
    with write_lock(config):
        if read_file(path) != original:
            raise ValueError("file changed on disk; reopen the editor")
        if temporary.read_bytes() != original:
            os.replace(temporary, path)
    temporary.unlink(missing_ok=True)


def save_text(path, content, original, validate, *, config_dir):
    """Save a built-in editor buffer; on failure it remains available on screen."""
    refuse_symlink(path)
    temporary = temporary_copy(path, content.encode("utf-8"), config_dir)
    try:
        commit_file(path, temporary, original, validate, config_dir)
    finally:
        temporary.unlink(missing_ok=True)


def edit_file(app, path, validate, *, config_dir=None) -> str | None:
    """Return None on success, otherwise an inline error. Never edit the live path."""
    path = Path(path)
    temporary = None
    try:
        original = read_file(path)
        config = Path(config_dir) if config_dir is not None else (
            path.parent.parent if path.parent.name == "repos" else path.parent)
        temporary = temporary_copy(path, original or b"", config)
        editor = shlex.split(os.environ.get("VISUAL") or os.environ.get("EDITOR") or "vi")

        def launch():
            # Blocking the Textual loop prevents frames reaching the editor's terminal.
            error = None
            with app.suspend():
                try:
                    result = subprocess.call([*editor, str(temporary)])
                except Exception as exc:
                    # Leave suspend normally so Textual also resumes after launch failures.
                    # Nobody edited the copy, so there is nothing to keep.
                    temporary.unlink(missing_ok=True)
                    error = exc
            if error is not None:
                raise error
            return result

        result = (launch() if threading.current_thread() is threading.main_thread()
                  else app.call_from_thread(launch))
        if result:
            raise ValueError(f"editor exited {result}; original kept")
        commit_file(path, temporary, original, validate, config)
        return None
    except Exception as exc:
        # Includes Textual SuspendNotSupported and arbitrary validator exceptions.
        retained = ""
        if temporary is not None and temporary.exists():
            temporary.chmod(0o600)
            retained = f"; edited copy kept at {temporary}"
        return f"editor unavailable: {exc}{retained}"

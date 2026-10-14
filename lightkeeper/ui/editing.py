"""Validated temp-copy editing; the Runner keeps running while Textual is suspended."""
import os
from pathlib import Path
import shlex
import subprocess
import tempfile

from ..settings import prepare_config_dir, refuse_symlink, write_lock


def nonempty_utf8(path: Path) -> None:
    if not path.read_text(encoding="utf-8").strip():
        raise ValueError("file must contain non-empty UTF-8 text")


def edit_file(app, path, validate, *, config_dir=None) -> str | None:
    """Return None on success, otherwise an inline error. Never edit the live path."""
    path = Path(path)
    temporary = None
    try:
        refuse_symlink(path)
        original = path.read_bytes() if path.exists() else None
        config = Path(config_dir) if config_dir is not None else (
            path.parent.parent if path.parent.name == "repos" else path.parent)
        prepare_config_dir(config)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        path.parent.chmod(0o700)
        fd, name = tempfile.mkstemp(prefix=f".{path.stem}-", suffix=path.suffix, dir=path.parent)
        temporary = Path(name)
        with os.fdopen(fd, "wb") as stream:
            stream.write(original or b"")
        editor = shlex.split(os.environ.get("VISUAL") or os.environ.get("EDITOR") or "vi")
        with app.suspend():
            result = subprocess.call([*editor, str(temporary)])
        if result:
            raise ValueError(f"editor exited {result}; original kept")
        temporary.chmod(0o600)
        validate(temporary)
        temporary.chmod(0o600)
        with temporary.open("rb") as stream:
            os.fsync(stream.fileno())
        with write_lock(config):
            refuse_symlink(path)
            if (path.read_bytes() if path.exists() else None) != original:
                raise ValueError("file changed on disk; reopen the editor")
            if temporary.read_bytes() != original:
                os.replace(temporary, path)
        temporary.unlink(missing_ok=True)
        return None
    except Exception as exc:
        # Includes Textual SuspendNotSupported and arbitrary validator exceptions.
        retained = ""
        if temporary is not None and temporary.exists():
            temporary.chmod(0o600)
            retained = f"; edited copy kept at {temporary}"
        return f"editor unavailable: {exc}{retained}"

"""Validated settings and per-repository judgement identities."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, replace
import fcntl
import hashlib
from importlib.resources import files
import json
import os
from pathlib import Path
import re
import tempfile
import tomllib

ACTIONS = ("approve", "request_changes", "comment")


class SettingsError(ValueError):
    def __init__(self, file: str | Path, key: str, message: str):
        self.file, self.key, self.message = Path(file), key, message
        super().__init__(f"{file}: {key}: {message}")


@dataclass(frozen=True)
class AgentSettings:
    engine: str = "claude"
    model: str = "opus"
    effort: str = "medium"
    profile: str | None = None
    timeout_s: int = 600


@dataclass(frozen=True)
class CommentSettings:
    language: str = "en"
    disclose: bool = True


@dataclass(frozen=True)
class RepoSettings:
    name: str
    enabled: bool = True
    start: str = "request"
    post: str = "dry-run"
    allowed: tuple[str, ...] = ACTIONS
    remarks: str = "important"
    deployments: bool = False


@dataclass(frozen=True)
class AlertSettings:
    reviews: bool = False


@dataclass(frozen=True)
class ScreenSettings:
    idle: str = "always"
    tempo: float = .8
    clock: bool = True
    moon: bool = True
    events: bool = True
    updates: bool = True


@dataclass(frozen=True)
class Settings:
    login: str
    agent: AgentSettings
    comment: CommentSettings
    repos: dict[str, RepoSettings]
    rules: str
    template: str
    notes: dict[str, str]
    config_dir: Path
    fresh: bool = False
    screen: ScreenSettings = ScreenSettings()
    alerts: AlertSettings = AlertSettings()

    @property
    def resolved_profile(self) -> str | None:
        """Resolve only at the engine boundary; the stored profile is round-trippable."""
        return (str((self.config_dir / Path(self.agent.profile).expanduser()).resolve())
                if self.agent.profile else None)

    def judgement(self, name: str) -> Judgement:
        from .reviewer import FRAME_VERSION, PR_FRAME, DEPLOY_FRAME, PR_SCHEMA_V2, DEPLOY_SCHEMA_V2, REMARKS_TEXT
        frame = FRAME_VERSION + "\n" + PR_FRAME + "\n" + DEPLOY_FRAME + "\n" + json.dumps(REMARKS_TEXT, sort_keys=True)
        return judgement(frame,
                         self.rules, self.notes.get(name, ""), self.repo(name).remarks,
                         json.dumps([PR_SCHEMA_V2, DEPLOY_SCHEMA_V2], sort_keys=True))

    def prompt_rules(self, name: str) -> str:
        notes = self.notes.get(name, "")
        return self.rules + ("\n\n# Repository notes\n\n" + notes if notes.strip() else "")

    def repo(self, name: str) -> RepoSettings:
        """Look up an enrolled repository; never silently enrol an unknown one."""
        return self.repos[name]


@dataclass(frozen=True)
class Judgement:
    hash: str
    components: dict[str, str]


def judgement(frame_version: str, rules: str, notes: str, remarks: str, schema_text: str) -> Judgement:
    values = dict(frame_version=frame_version, rules=rules, notes=notes, remarks=remarks, schema_text=schema_text)
    digest = hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest()[:12]
    components = {key: hashlib.sha256(value.encode()).hexdigest()[:8] for key, value in values.items()}
    return Judgement(digest, components)


def config_path(config_dir: str | Path | None = None) -> Path:
    return Path(config_dir if config_dir is not None else
                os.environ.get("LIGHTKEEPER_CONFIG", "~/.config/lightkeeper")).expanduser().resolve()


def prepare_config_dir(config_dir: str | Path | None = None) -> Path:
    path = config_path(config_dir)
    try:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        path.chmod(0o700)
        for directory in (path / "repos",):
            if directory.is_dir() and not directory.is_symlink():
                directory.chmod(0o700)
        for file in (path / "settings.toml", path / "rules.md", path / "template.md",
                     *sorted((path / "repos").glob("*.md"))):
            if file.is_file() and not file.is_symlink():
                file.chmod(0o600)
    except OSError as exc:
        raise SettingsError(path, "directory", str(exc)) from exc
    return path


def init_config(config_dir: str | Path | None = None) -> list[tuple[Path, bool]]:
    """Install packaged defaults without replacing any existing file."""
    config = prepare_config_dir(config_dir)
    result = []
    for name in ("settings.toml", "rules.md"):
        path = config / name
        try:
            with open(path, "xb") as stream:  # Exclusive create: an existing file is never touched.
                os.fchmod(stream.fileno(), 0o600)
                stream.write(files("lightkeeper").joinpath("templates", name).read_bytes())
        except FileExistsError:
            result.append((path, False))
        except OSError:
            path.unlink(missing_ok=True)  # The file was ours; leave nothing half-written.
            raise
        else:
            result.append((path, True))
    return result


def _read_text(path: Path, *, optional: bool = False) -> str:
    try:
        text = path.read_text(encoding="utf-8")
        return text
    except FileNotFoundError:
        if optional:
            return ""
        raise SettingsError(path, "file", "file not found") from None
    except (OSError, UnicodeError) as exc:
        raise SettingsError(path, "file", str(exc)) from exc


def _table(raw, file: Path, key: str, allowed: set[str]) -> dict:
    if not isinstance(raw, dict):
        raise SettingsError(file, key, "expected a table")
    for name in sorted(raw):
        if name not in allowed:
            raise SettingsError(file, f"{key}.{name}" if key else name, "unknown key")
    return raw


def _value(raw, name, default, kind, file, prefix, choices=()):
    value = raw.get(name, default)
    key = f"{prefix}.{name}"
    if type(value) is not kind or (kind is str and not value.strip()):
        raise SettingsError(file, key, f"expected {'a non-empty string' if kind is str else kind.__name__}")
    if choices and value not in choices:
        message = "only 'request' is supported" if name == "start" else f"expected one of {', '.join(choices)}"
        raise SettingsError(file, key, message)
    return value


def load_settings(config_dir: str | Path | None = None, *, include_text: bool = True) -> Settings:
    config = config_path(config_dir)
    file = config / "settings.toml"
    rules = _read_text(config / "rules.md", optional=True) if include_text else ""
    template = _read_text(config / "template.md", optional=True) if include_text else ""
    if not file.exists():
        return Settings("", AgentSettings(), CommentSettings(), {}, rules, template, {}, config, fresh=True)
    try:
        raw = tomllib.loads(_read_text(file))
    except tomllib.TOMLDecodeError as exc:
        raise SettingsError(file, "toml", str(exc)) from exc
    _table(raw, file, "", {"identity", "agent", "comment", "repos", "screen", "alerts"})
    alerts = _table(raw.get("alerts", {}), file, "alerts", {"reviews"})
    alert_settings = AlertSettings(_value(alerts, "reviews", False, bool, file, "alerts"))
    screen = _table(raw.get("screen", {}), file, "screen", {"idle", "tempo", "clock", "moon", "events", "updates"})
    tempo = screen.get("tempo", .8)
    if type(tempo) not in (int, float):
        raise SettingsError(file, "screen.tempo", "expected a number")
    tempo = float(tempo)
    if not .2 <= tempo <= 1.5:
        raise SettingsError(file, "screen.tempo", "expected 0.2..1.5")
    screen_settings = ScreenSettings(
        idle=_value(screen, "idle", "always", str, file, "screen", ("quiet", "always")), tempo=tempo,
        clock=_value(screen, "clock", True, bool, file, "screen"),
        moon=_value(screen, "moon", True, bool, file, "screen"),
        events=_value(screen, "events", True, bool, file, "screen"),
        updates=_value(screen, "updates", True, bool, file, "screen"))
    identity = _table(raw.get("identity", {}), file, "identity", {"login"})
    login = _value(identity, "login", None, str, file, "identity")
    if not re.fullmatch(r"[A-Za-z0-9-]{1,39}", login):
        raise SettingsError(file, "identity.login", "expected a GitHub login (1-39 letters, digits or hyphens)")
    agent = _table(raw.get("agent", {}), file, "agent", {"engine", "model", "effort", "profile", "timeout_s"})
    agent_values = {key: _value(agent, key, default, str, file, "agent")
                    for key, default in (("engine", "claude"), ("model", "opus"), ("effort", "medium"))}
    if agent_values["engine"] != "claude":
        raise SettingsError(file, "agent.engine", "only 'claude' is supported")
    timeout = _value(agent, "timeout_s", 600, int, file, "agent")
    if timeout <= 0:
        raise SettingsError(file, "agent.timeout_s", "expected a positive integer")
    profile = None
    if "profile" in agent:
        profile = _value(agent, "profile", None, str, file, "agent")
    comment = _table(raw.get("comment", {}), file, "comment", {"language", "disclose"})
    comments = CommentSettings(_value(comment, "language", "en", str, file, "comment"),
                               _value(comment, "disclose", True, bool, file, "comment"))
    repos_raw = raw.get("repos", {})
    if not isinstance(repos_raw, dict):
        raise SettingsError(file, "repos", "expected a table")
    repos, notes = {}, {}
    for name, values in repos_raw.items():
        prefix = f'repos."{name}"'
        if not re.fullmatch(r"[A-Za-z0-9-]+/[A-Za-z0-9_.-]+", name) or name.split("/")[1] in (".", ".."):
            raise SettingsError(file, prefix, "expected an org/name repository name")
        _table(values, file, prefix, {"enabled", "start", "post", "allowed", "remarks", "deployments"})
        allowed = values.get("allowed", list(ACTIONS))
        if not isinstance(allowed, list) or any(not isinstance(a, str) or a not in ACTIONS for a in allowed):
            raise SettingsError(file, f"{prefix}.allowed", f"expected an array containing only {', '.join(ACTIONS)}")
        repos[name] = RepoSettings(
            name, _value(values, "enabled", True, bool, file, prefix),
            _value(values, "start", "request", str, file, prefix, ("request",)),
            _value(values, "post", "dry-run", str, file, prefix, ("live", "dry-run")), tuple(allowed),
            _value(values, "remarks", "important", str, file, prefix, ("blockers", "important", "all")),
            _value(values, "deployments", False, bool, file, prefix))
        if include_text:
            notes[name] = _read_text(config / "repos" / (name.replace("/", "__") + ".md"), optional=True)
    if include_text and any(repo.enabled for repo in repos.values()) and not rules.strip():
        raise SettingsError(config / "rules.md", "rules", "shared rules are missing or blank; "
                            "run lightkeeper --init to create missing files, then edit rules.md")
    return Settings(login, AgentSettings(**agent_values, profile=profile, timeout_s=timeout), comments,
                    repos, rules, template, notes, config, screen=screen_settings, alerts=alert_settings)


def changed_components(before: dict[str, str], after: dict[str, str], repo: str) -> str:
    labels = {"rules": "rules changed", "notes": f"notes changed for {repo}", "remarks": "remarks changed",
              "frame_version": "prompt frame changed", "schema_text": "answer schema changed"}
    return "; ".join(label for key, label in labels.items() if before.get(key) != after.get(key))


@dataclass(frozen=True)
class Saved:
    settings: Settings
    digest: str


def settings_digest(config_dir: str | Path) -> str:
    path = Path(config_dir) / "settings.toml"
    return hashlib.sha256(path.read_bytes() if path.exists() else b"").hexdigest()


def validate_template(template: str) -> None:
    from .models import AnswerV2
    from .posting import render_body
    render_body(AnswerV2("comment", "Validation", (), "", why="Looks solid."), template, CommentSettings(),
                RepoSettings("example/repo"), head_sha="0" * 40, judgement="0" * 12,
                engine_label="claude/validation", login="reviewer")


def validate_settings_file(path: Path, config_dir: Path) -> Settings:
    """Load a candidate in isolation, including the current supporting text files."""
    with tempfile.TemporaryDirectory(prefix="lightkeeper-settings-") as directory:
        root = Path(directory).resolve()
        (root / "settings.toml").write_bytes(path.read_bytes())
        for name in ("rules.md", "template.md"):
            source = config_dir / name
            if source.exists():
                (root / name).write_bytes(source.read_bytes())
        notes = config_dir / "repos"
        if notes.is_dir():
            (root / "repos").mkdir(mode=0o700)
            for source in notes.glob("*.md"):
                (root / "repos" / source.name).write_bytes(source.read_bytes())
        try:
            settings = load_settings(root)
        except SettingsError as exc:
            if exc.file.is_relative_to(root):  # Name the real file, not the temporary copy.
                raise SettingsError(config_dir / exc.file.relative_to(root), exc.key, exc.message) from None
            raise
        if (root / "template.md").exists():
            validate_template(settings.template)
        return replace(settings, config_dir=config_dir)


def refuse_symlink(path: Path) -> None:
    if path.is_symlink():
        raise SettingsError(path, "file", "refusing to replace a symlinked file")


@contextmanager
def write_lock(config: Path):
    """Serialize cooperating writers; external editors do not take this lock."""
    fd = os.open(config / ".write-lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        os.fchmod(fd, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def save_settings(config_dir: str | Path, patch: dict, expected_digest: str) -> Saved:
    """Apply one repository patch, one agent/comment field, or screen preferences.

    Shape: {"repos": {"org/name": {field: value, ...}}}; None removes a repo.
    Missing repos use RepoSettings defaults. Other sections use {section: {field: value}}.
    """
    from .migrate import settings_toml
    config = prepare_config_dir(config_dir)
    refuse_symlink(config / "settings.toml")
    def check_digest():
        if settings_digest(config) != expected_digest:
            raise SettingsError("settings.toml", "conflict", "settings changed on disk; reopen the form")
    check_digest()
    current = load_settings(config)
    check_digest()
    if current.fresh or not current.login.strip():
        raise SettingsError("settings.toml", "identity.login", "configure a login before saving")
    if not isinstance(patch, dict) or len(patch) != 1:
        raise SettingsError("settings.toml", "patch", "expected exactly one patch")
    section, fields = next(iter(patch.items()))
    if (section not in ("repos", "agent", "comment", "screen") or not isinstance(fields, dict)
            or not fields or (section != "screen" and len(fields) != 1)):
        raise SettingsError("settings.toml", "patch", "expected one repository, agent/comment field, or screen patch")
    try:
        if section == "repos":
            name, changes = next(iter(fields.items()))
            repos = dict(current.repos)
            if changes is None:
                del repos[name]
            else:
                if not isinstance(changes, dict) or "name" in changes:
                    raise ValueError("expected repository fields")
                repos[name] = replace(repos.get(name, RepoSettings(name)), **changes)
            candidate = replace(current, repos=repos)
        else:
            candidate = replace(current, **{section: replace(getattr(current, section), **fields)})
        content = ("# written by lightkeeper; comments are not kept\n"
                   + settings_toml(candidate)).encode("utf-8")
    except (KeyError, TypeError, ValueError) as exc:
        raise SettingsError("settings.toml", "patch", str(exc)) from exc
    fd, temporary = tempfile.mkstemp(prefix=".settings-", suffix=".toml", dir=config)
    path = Path(temporary)
    try:
        with os.fdopen(fd, "wb") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        validated = validate_settings_file(path, config)
        with write_lock(config):
            refuse_symlink(config / "settings.toml")
            check_digest()
            os.replace(path, config / "settings.toml")
        return Saved(validated, hashlib.sha256(content).hexdigest())
    finally:
        path.unlink(missing_ok=True)

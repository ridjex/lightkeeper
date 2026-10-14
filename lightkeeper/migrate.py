"""Resumable legacy migration and journal grandfathering."""
from __future__ import annotations

from dataclasses import asdict, dataclass, replace
import hashlib
import json
import os
from pathlib import Path
import tomllib

from .journal import now_iso
from .policy import PolicyError, load_policy
from .settings import (AgentSettings, CommentSettings, RepoSettings, Settings, SettingsError,
                       load_settings, prepare_config_dir)


def legacy_policy_hash(raw_toml: dict, rules_text: str) -> str:
    """Frozen semantics 5 hash. Never use policy.SEMANTICS_VERSION here."""
    return hashlib.sha256(json.dumps(["5", raw_toml, rules_text], sort_keys=True).encode()).hexdigest()[:6]


def _toml_value(value) -> str:
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False).replace("\x7f", "\\u007f")
    if type(value) is bool:
        return "true" if value else "false"
    if type(value) in (int, float):
        return str(value)
    if isinstance(value, (tuple, list)) and all(isinstance(item, str) for item in value):
        return "[" + ", ".join(_toml_value(item) for item in value) + "]"
    raise TypeError(f"unsupported TOML value: {type(value).__name__}")


def settings_toml(settings: Settings) -> str:
    """Write only our fixed settings shape, with quoted repository table keys."""
    sections = [("identity", {"login": settings.login}), ("agent", asdict(settings.agent)),
                ("comment", asdict(settings.comment)), ("screen", asdict(settings.screen))]
    sections.extend((f"repos.{_toml_value(name)}", {k: v for k, v in asdict(repo).items() if k != "name"})
                    for name, repo in settings.repos.items())
    lines = []
    for table, values in sections:
        lines.append(f"[{table}]")
        lines.extend(f"{key} = {_toml_value(value)}" for key, value in values.items() if value is not None)
        lines.append("")
    return "\n".join(lines)


@dataclass(frozen=True)
class MigrationReport:
    policy_path: str
    policy_sha256: str
    rules_sha256: str
    legacy_hash: str
    created: str
    written: tuple[Path, ...]

    def __str__(self) -> str:
        return ("Migrated: " + ", ".join(str(path) for path in self.written)
                + "\nRepository-specific rules belong in repos/<org>__<name>.md.")


def _write_file(path: Path, content: bytes, *, replace_manifest: bool = False) -> None:
    """Publish without clobbering; only tool-owned metadata may be replaced."""
    if not replace_manifest and (path.exists() or path.is_symlink()):
        raise SettingsError(path, "migration", "destination exists without migration.json; refusing to overwrite")
    partial = path.with_name(f"{path.name}.{os.getpid()}.partial")
    # Unlink an abandoned partial before opening, so a stale symlink is not followed.
    partial.unlink(missing_ok=True)
    fd = os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as fh:
        os.fchmod(fh.fileno(), 0o600)
        fh.write(content)
        fh.flush()
        os.fsync(fh.fileno())
    if replace_manifest:
        os.replace(partial, path)                       # only tool-owned metadata
    else:
        try:
            os.link(partial, path)                     # atomic no-clobber publication
        except FileExistsError as exc:
            raise SettingsError(path, "migration", "destination appeared; refusing to overwrite") from exc
        partial.unlink()


def _migration_inputs(policy_path: Path, config: Path) -> tuple[dict, dict[str, bytes], dict[str, str]]:
    source = Path(policy_path).expanduser()
    policy = load_policy(source)
    policy_bytes = source.read_bytes()
    rules_bytes = policy.rules_path.read_bytes()
    raw = tomllib.loads(policy_bytes.decode("utf-8"))
    settings = Settings(policy.login, AgentSettings(model=policy.model, effort=policy.effort,
                        profile=policy.claude_config_dir or None), CommentSettings(),
                        {name: RepoSettings(name, post=policy.mode, deployments=True) for name in policy.repos},
                        policy.rules, "", {}, config)
    provenance = dict(policy_path=str(source.resolve()), policy_sha256=hashlib.sha256(policy_bytes).hexdigest(),
                      rules_sha256=hashlib.sha256(rules_bytes).hexdigest(),
                      legacy_hash=legacy_policy_hash(raw, policy.rules), created=now_iso())
    return (provenance, {"settings.toml": settings_toml(settings).encode("utf-8"), "rules.md": rules_bytes},
            {name: settings.judgement(name).hash for name in settings.repos})


def migrate(policy_path: Path | None, config_dir: Path) -> MigrationReport:
    config = Path(config_dir).expanduser().resolve()
    marker = config / "migration.json"
    written = tuple(config / name for name in ("settings.toml", "rules.md", "migration.json"))
    contents = None
    if marker.exists() or marker.is_symlink():
        try:
            manifest = json.loads(marker.read_text(encoding="utf-8"))
            provenance = {key: manifest[key] for key in
                          ("policy_path", "policy_sha256", "rules_sha256", "legacy_hash", "created")}
            if (not all(isinstance(value, str) for value in provenance.values())
                    or type(manifest["done"]) is not bool
                    or not isinstance(manifest["files"], dict)
                    or set(manifest["files"]) != {"settings.toml", "rules.md"}
                    or any(not isinstance(digest, str) or len(digest) != 64
                           or any(c not in "0123456789abcdef" for c in digest)
                           for digest in manifest["files"].values())):
                raise ValueError("invalid manifest fields")
            report = MigrationReport(**provenance, written=written)
        except (OSError, ValueError, TypeError, KeyError) as exc:
            raise SettingsError(marker, "migration", f"invalid completion report: {exc}") from exc
        if manifest["done"]:
            return report
        source = Path(manifest["policy_path"])
        if not source.is_absolute():
            raise SettingsError(marker, "migration", "recorded policy_path must be absolute")
        try:
            inputs, contents, _ = _migration_inputs(source, config)
        except (OSError, PolicyError) as exc:
            raise SettingsError(source, "migration", f"cannot resume from recorded source; "
                                f"restore the original policy and rules files: {exc}") from exc
        for key, name in (("policy_sha256", "settings.toml"), ("rules_sha256", "rules.md")):
            if inputs[key] != manifest[key]:
                raise SettingsError(config / name, "migration", f"legacy input changed: recorded input hashes changed "
                                    f"for {source}; restore the original policy and rules files")
    else:
        if policy_path is None:
            raise SettingsError(marker, "migration", "no recorded source; pass --policy PATH to migrate")
        for path in (config / "settings.toml", config / "rules.md", config / "template.md",
                     *sorted((config / "repos").glob("*.md"))):
            if path.exists() or path.is_symlink():
                raise SettingsError(path, "migration", "destination exists without migration.json; refusing to overwrite")
        provenance, contents, baselines = _migration_inputs(policy_path, config)
        report = MigrationReport(**provenance, written=written)
        manifest = dict(provenance, done=False, baseline_judgements=baselines,
                        files={name: hashlib.sha256(content).hexdigest() for name, content in contents.items()})
        prepare_config_dir(config)
        _write_file(marker, (json.dumps(manifest, indent=2) + "\n").encode("utf-8"))

    missing = []
    for name, digest in manifest["files"].items():
        path = config / name
        if path.exists() or path.is_symlink():
            if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
                raise SettingsError(path, "migration", "destination conflicts with migration.json; refusing to overwrite")
        else:
            missing.append(name)
    if missing:
        refreshed = False
        for name in missing:
            digest = hashlib.sha256(contents[name]).hexdigest()
            if digest != manifest["files"][name]:
                manifest["files"][name] = digest
                refreshed = True
        if refreshed:
            # Inputs were verified above. Persist today's serializer digests before publishing files,
            # so another interruption can resume without mistaking our own output for a conflict.
            _write_file(marker, (json.dumps(manifest, indent=2) + "\n").encode("utf-8"), replace_manifest=True)
        for name in missing:
            _write_file(config / name, contents[name])
    load_settings(config)                              # validate before declaring migration complete
    if "baseline_judgements" not in manifest:
        manifest["baseline_judgements"] = _migration_baselines(manifest, config)
    manifest["done"] = True
    _write_file(marker, (json.dumps(manifest, indent=2) + "\n").encode("utf-8"), replace_manifest=True)
    return report


def _migration_baselines(source: dict, config: Path) -> dict[str, str]:
    baselines = source.get("baseline_judgements")
    if baselines is None:
        # Older manifests have no frozen hashes. Only the recorded migration inputs can reconstruct them;
        # later repository notes and templates were never part of the migrated judgement.
        files = source["files"]
        if set(files) != {"settings.toml", "rules.md"}:
            raise SettingsError(config / "migration.json", "rehash", "invalid migrated file digests")
        def verify():
            for name, digest in files.items():
                if hashlib.sha256((config / name).read_bytes()).hexdigest() != digest:
                    raise SettingsError(config / name, "rehash",
                                        "baseline_judgements missing and migrated file digest changed; "
                                        "cannot safely reconstruct migration baseline")
        verify()
        migrated = replace(load_settings(config), notes={}, template="")
        verify()
        baselines = {name: migrated.judgement(name).hash for name in migrated.repos}
    if (not isinstance(baselines, dict)
            or any(not isinstance(name, str) or not isinstance(value, str) or not value
                   for name, value in baselines.items())):
        raise SettingsError(config / "migration.json", "rehash", "invalid baseline_judgements")
    return baselines


def _rehash_legacy(settings: Settings, journal) -> dict | None:
    """Run under the state lock, after replay. Recover either missing half of the durable mapping."""
    manifest = settings.config_dir / "migration.json"
    marker = journal.path.parent / "rehash.json"
    if not manifest.exists():
        return None
    try:
        source = json.loads(manifest.read_text(encoding="utf-8"))
        if not source["done"]:
            raise ValueError("migration is incomplete")
        legacy = source["legacy_hash"]
        saved = json.loads(marker.read_text(encoding="utf-8")) if marker.exists() else None
        record = journal.rehashes.get(legacy)
        if saved and saved["legacy_hash"] == legacy:
            if record is not None:
                return None
            record = journal.append_rehash(legacy, saved["judgements"])
        elif record is None:
            record = journal.append_rehash(legacy, _migration_baselines(source, settings.config_dir))
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise SettingsError(manifest, "rehash", str(exc)) from exc
    result = dict(legacy_hash=legacy, judgements=record["to"], at=record["at"])
    _write_file(marker, (json.dumps(result, indent=2) + "\n").encode(), replace_manifest=True)
    return result


def rehash(settings: Settings, journal) -> dict | None:
    """Preserve settled judgements across legacy migration and the frame-3 wording change."""
    result = _rehash_legacy(settings, journal)
    # Frame 3 only changes public wording. Keep settlements, not cached answers, from frame 2.
    from ._frame_v2 import FRAME, SCHEMA
    from .settings import judgement
    aliases, updated = {}, {}
    for name, repo in settings.repos.items():
        previous = judgement(FRAME, settings.rules, settings.notes.get(name, ""), repo.remarks, SCHEMA).hash
        current = settings.judgement(name).hash
        if previous != current and (previous in journal.settled_policies(name)
                or any(record["to"].get(name) == previous for record in journal.rehashes.values())):
            aliases.setdefault(previous, {})[name] = current
    for previous, targets in aliases.items():
        recorded = journal.rehashes.get(previous, {}).get("to", {})
        if any(recorded.get(name) != value for name, value in targets.items()):
            journal.append_rehash(previous, {**recorded, **targets})
            updated.update(targets)
    if updated:
        result = {**(result or {}), "frame_2": updated}
    return result

"""Complete review material, with only script-generated names on disk."""
from dataclasses import dataclass, field
import os
from pathlib import Path

INLINE_MAX_BYTES = 200_000  # About 90k tokens, so a typical request is still answered in one step.
REVIEW_MAX_TURNS = 60  # Room for batched reads and a verdict without an unbounded paid session.


def escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("\t", "\\t").replace("\r", "\\r").replace("\n", "\\n")


def write_private(path: Path, text: str) -> None:
    with path.open("w", encoding="utf-8") as stream:
        os.fchmod(stream.fileno(), 0o600)
        stream.write(text)


@dataclass
class Workspace:
    files: dict[str, str] = field(default_factory=dict)
    problems: list[str] = field(default_factory=list)

    def put(self, name: str, text: str) -> str:
        self.files[name] = text
        return name

    def size(self, name: str) -> int:
        return sum(len(text.encode("utf-8")) for path, text in self.files.items()
                   if path == name or path.startswith(name.rstrip("/") + "/"))

    def changes(self, files, prefix="") -> None:
        rows = ["number\tstatus\tadditions\tdeletions\tpatch bytes\tfile\tpath\tprevious path"]
        for number, file in enumerate(files, 1):
            name = f"diff/{number:04d}.patch"
            patch = (file.patch if file.patch is not None else
                     "(no patch: empty file or pure rename)" if file.is_complete else
                     "(patch omitted by GitHub: binary or too large)")
            self.put(prefix + name, f"path: {escape(file.path)}\n{patch}")
            rows.append("\t".join(map(str, (number, escape(file.status), file.additions, file.deletions,
                        len((file.patch or "").encode("utf-8")), name, escape(file.path), escape(file.previous_path or "")))))
        self.put(prefix + "files.tsv", "\n".join(rows) + "\n")

    def write(self, root: Path) -> None:
        root.chmod(0o700)
        for name, text in self.files.items():
            path = root / name
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            for parent in path.parents:
                if parent == root:
                    break
                parent.chmod(0o700)
            write_private(path, text)

    def overview(self, timeout_s: int) -> str:
        groups = dict.fromkeys(name.split("/")[0] + ("/" if "/" in name else "") for name in self.files)
        rows = []
        for name in groups:
            count = sum(path == name or path.startswith(name) for path in self.files)
            rows.append(f"- {name}: {self.size(name)} bytes, {count} file(s)")
        return ("## Material on disk\n" + "\n".join(rows)
                + f"\nAllowance: {REVIEW_MAX_TURNS} turns, {timeout_s} seconds.\n"
                + "\n".join(self.problems))

    def prompt(self, header: str, sections: list[tuple[str, str, str]], timeout_s: int) -> str:
        """Reserve all pointers first; inline whole sections in order, counting UTF-8 bytes."""
        self.put("request.md", header)
        pointers = [f"(in {name}: {self.size(name)} bytes)" for _, name, _ in sections]
        overview = self.overview(timeout_s)
        lead = f"# Request\n(in request.md: {self.size('request.md')} bytes)\n\n" + overview
        def render(bodies):
            return lead + "".join(f"\n\n## {heading}\n{body}" for (heading, _, _), body in zip(sections, bodies)) + "\n"
        header_lead = header + "\n\n" + overview
        if len(render(pointers).encode("utf-8")) + len(header_lead.encode("utf-8")) - len(lead.encode("utf-8")) <= INLINE_MAX_BYTES:
            lead = header_lead
        for index, (_, _, body) in enumerate(sections):
            candidate = pointers.copy()
            candidate[index] = body
            if len(render(candidate).encode("utf-8")) <= INLINE_MAX_BYTES:
                pointers = candidate
        return render(pointers)

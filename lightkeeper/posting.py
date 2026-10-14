"""Pure v2 comment rendering; private reasons never enter the template."""
from dataclasses import dataclass
import re
import unicodedata

from .models import AnswerV2, MARKER
from .settings import CommentSettings, RepoSettings, SettingsError

BODY_MAX_CHARS = 4_000
DEFAULT_TEMPLATE = "🔭 {why}\n\n{watch}"
PLACEHOLDER = re.compile(r"\{[^{}]*\}")
REMARKS = {"blockers": ("blocker",), "important": ("blocker", "important"),
           "all": ("blocker", "important", "minor")}


@dataclass(frozen=True)
class Rendered:
    body: str
    published_findings: int
    withheld_reason: str | None = None


def _model_text(text: str, *, neutralise_mentions: bool = True) -> str:
    if neutralise_mentions:
        parts = text.split("`")
        parts[::2] = [re.sub(r"(?<![A-Za-z0-9_./@])@+(?=[A-Za-z0-9_])", "", part)
                     for part in parts[::2]]
        text = "`".join(parts)
    # Escape block fence lines without changing inline code spans.
    text = re.sub(r"(?m)^([ \t]*)(`{3,}|~{3,})", r"\1\\\2", text)
    # Model text must never open or close an HTML comment, including a forged v2 trace.
    text = text.replace("<!--", "<!‑‑").replace("-->", "‑‑>")
    # Neutralise visible v1 marker-like lines so readers cannot mistake them for provenance: a no-break
    # space before the parenthesis looks the same and never matches the marker pattern.
    return re.sub(r"(?m)^([ \t>]*(?:lightkeeper|approval-delegate)) +\(", "\\1\u00a0(", text)


def owner_approval_body(note: str, login: str) -> str:
    """The owner's manual review; shared by the confirmation preview and Runner."""
    note = re.sub(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b\[[0-?]*[ -/]*[@-~]|\x1b[@-_]", "", note)
    note = "".join(" " if c != "\n" and (unicodedata.category(c) in ("Cc", "Cf", "Cs", "Zs", "Zl", "Zp")
                   or c in "\u2800\u115f\u1160\u3164") else c for c in note)
    note = "".join(c for c in note if c not in "\u034f\u17b4\u17b5\uffa0"
                   and not "\ufe00" <= c <= "\ufe0f" and not "\U000e0100" <= c <= "\U000e01ef")
    note = re.sub(r"[^\S\n]+", " ", note)
    note = _model_text("\n".join(line.rstrip() for line in note.split("\n")).strip(), neutralise_mentions=False)
    return (note + "\n\n" if note else "") + f"Approved by @{login} from the lightkeeper screen."


def render_body(answer: AnswerV2, template: str, comment: CommentSettings, repo: RepoSettings, *,
                head_sha: str, judgement: str, engine_label: str, login: str) -> Rendered:
    template = template or DEFAULT_TEMPLATE
    for match in PLACEHOLDER.finditer(template):
        if match.group() not in ("{why}", "{watch}", "{summary}", "{findings}", "{disclosure}"):
            raise SettingsError("template.md", match.group(), "unknown placeholder")
    if answer.action in ("request_changes", "comment") and not answer.why.strip():
        return Rendered("", 0, "no publishable rationale: why is blank")
    # Blank findings cannot supply rationale, even when a caller bypasses the v2 parser.
    findings = [f for f in answer.findings if f.severity in REMARKS[repo.remarks] and f.text.strip()]

    # Only code-owned identifiers enter the trace; refuse an incomplete or multiline trace.
    if not re.fullmatch(r"[0-9a-fA-F]{7,40}", head_sha):
        raise ValueError("head_sha must be 7 to 40 hex characters")
    if not re.fullmatch(r"[0-9a-fA-F]{12}", judgement):
        raise ValueError("judgement must be 12 hex characters")
    if not re.fullmatch(r"[^\s<>]+", engine_label):
        raise ValueError("engine_label must be non-empty, without whitespace or angle brackets")
    trace = f"<!-- {MARKER} v2 head={head_sha[:7]} judgement={judgement} engine={engine_label} -->"

    bullets = []
    for finding in findings:
        path = _model_text(finding.path, neutralise_mentions=False) if finding.path else ""
        url = _model_text(finding.url, neutralise_mentions=False) if finding.url else ""
        location = (f" [{path}]({url})" if url else f" [{path}]") if path else (f" ({url})" if url else "")
        bullets.append(f"- **{finding.severity}** ({finding.origin}): {_model_text(finding.text)}" + location)
    disclosure = f"Automated review for @{login} (lightkeeper)"
    if comment.disclose and "{disclosure}" not in template:
        template += "\n\n{disclosure}"
    values = {"{why}": _model_text(answer.why), "{watch}": _model_text(answer.watch),
              "{summary}": _model_text(answer.summary), "{findings}": "\n".join(bullets),
              "{disclosure}": disclosure if comment.disclose else ""}
    # Substitute only tokens in the template, never tokens introduced by model text.
    def assemble():
        parts, finding_ends = [], []
        cursor = size = 0
        for match in PLACEHOLDER.finditer(template):
            literal = template[cursor:match.start()]
            parts.append(literal)
            size += len(literal)
            if match.group() == "{findings}":
                end = size
                for index, bullet in enumerate(bullets):
                    end += len(bullet) + (1 if index else 0)
                    finding_ends.append((index, end))
            value = values[match.group()]
            parts.append(value)
            size += len(value)
            cursor = match.end()
        parts.append(template[cursor:])
        body = "".join(parts)
        finding_ends = [(index, len(re.sub(r"\n{3,}", "\n\n", body[:end])))
                        for index, end in finding_ends]
        body = re.sub(r"\n{3,}", "\n\n", body).rstrip()
        leading = len(body) - len(body.lstrip())
        return body.lstrip(), leading, finding_ends

    body, leading, finding_ends = assemble()
    limit = BODY_MAX_CHARS - len(trace) - 2
    footer = ""
    if len(body) > limit and comment.disclose and "{disclosure}" in template:
        # On truncation, preserve the disclosure as a footer outside the clipped content.
        footer = "\n\n" + disclosure
        limit -= len(footer)
        values["{disclosure}"] = ""
        body, leading, finding_ends = assemble()
    kept = len(body)
    if kept > limit:
        # Keep whole lines, reserving room for the newline and the ellipsis.
        boundary = body.rfind("\n", 0, limit - 1)
        kept = max(0, boundary)
        body = body[:kept] + "\n…" if kept else "…"
    # Withhold only after rendering and capping, ignoring disclosure, whitespace and the added ellipsis.
    if answer.action in ("request_changes", "comment") and not body[:kept].replace(disclosure, "").strip():
        return Rendered("", 0, "no publishable rationale after the remarks filter")
    published = len({index for index, end in finding_ends if end - leading <= kept})
    return Rendered(body + footer + "\n\n" + trace, published)

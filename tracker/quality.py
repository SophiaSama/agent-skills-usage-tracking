"""Static quality audit for inventoried agent skills.

Evaluates ``SkillCopy`` entries from ``tracker.inventory`` using stdlib-only
static checks (YAML frontmatter completeness, trigger description quality,
``SKILL.md`` progressive-disclosure size limit, local markdown links, and
unreferenced support files).

Runs on demand, writes nothing unless CSV export is requested, and omits host
filesystem paths from rendered Markdown and dashboard metadata payloads.
"""

from __future__ import annotations

import csv
import os
import re
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

from tracker import inventory

MIN_DESCRIPTION_CHARS = 40
CUE_DESCRIPTION_CHARS = 80
MAX_DESCRIPTION_CHARS = 1024
MAX_SKILL_MD_LINES = 500
MAX_SUPPORT_SCAN = 200

METADATA_DIRS = {"agents"}
METADATA_PREFIXES = ("license", "readme", "changelog")
METADATA_FILES = {
    "package.json",
    "package-lock.json",
    "pyproject.toml",
    "plugin.json",
    "manifest.json",
    "__init__.py",
}

ISSUE_CODES = (
    "missing-frontmatter",
    "missing-name",
    "missing-description",
    "weak-description",
    "overlong-description",
    "oversized-skill-md",
    "broken-link",
    "unreferenced-file",
)

_TRIGGER_CUE_RE = re.compile(
    r"\b(?:"
    r"use|used|using|when|whenever|trigger|triggered|triggers|for|help|helps|helping|"
    r"search|searches|query|queries|build|builds|building|debug|debugs|debugging|"
    r"run|runs|running|generate|generates|generating|analyze|analyzes|analyzing|"
    r"analyse|analyses|manage|manages|managing|create|creates|creating|"
    r"check|checks|checking|audit|audits|auditing|inspect|inspects|inspecting|"
    r"validate|validates|validating|test|tests|testing|deploy|deploys|deploying|"
    r"migrate|migrates|migrating|convert|converts|converting|extract|extracts|"
    r"parse|parses|parsing|render|renders|rendering|sync|syncs|syncing|"
    r"track|tracks|tracking|review|reviews|reviewing|refactor|refactors|refactoring|"
    r"scaffold|scaffolds|prototype|prototypes|plan|plans|planning|format|formats|"
    r"lint|lints|fix|fixes|fixing|edit|edits|editing|write|writes|writing|"
    r"read|reads|reading|fetch|fetches|monitor|monitors|monitoring|"
    r"configure|configures|configuring|install|installs|installing|"
    r"guide|guides|workflow|workflows|activate|activates|invoke|invokes|"
    r"automate|automates|summarize|summarizes|compare|compares|compile|compiles|"
    r"verify|verifies|design|designs|implement|implements|troubleshoot|"
    r"explain|explains|explore|explores|browse|browses|find|finds|detect|detects|"
    r"connect|connects|update|updates|export|exports|import|imports|handle|handles"
    r")\b",
    re.IGNORECASE,
)

_TOP_KEY_RE = re.compile(r"^([A-Za-z0-9_-]+)[ \t]*:(?:[ \t]*(.*))?$")
_BLOCK_SCALAR_RE = re.compile(r"^([|>])([+-]|\d+[+-]?|[+-]\d+)?(?:[ \t]+#.*)?$")
_SCHEME_RE = re.compile(r"^([A-Za-z][A-Za-z0-9+.-]*):")
_WINDOWS_DRIVE_RE = re.compile(r"^[A-Za-z]:[/\\]")
_FENCE_OPEN_RE = re.compile(r"^[ \t]*(`{3,}|~{3,})")
_INLINE_CODE_RE = re.compile(r"(`+)([^\n]*?)\1")
_PLACEHOLDER_RE = re.compile(r"\{[^{}\s]+\}")
_ANGLE_PLACEHOLDER_RE = re.compile(r"<[^<>\s]+>")


@dataclass(frozen=True)
class ParsedSkill:
    frontmatter: dict[str, str]
    body: str
    raw: str
    line_count: int
    has_frontmatter: bool
    readable: bool = True


@dataclass(frozen=True)
class QualityIssue:
    code: str
    severity: str  # "warning" | "info"
    detail: str

    def to_dict(self) -> dict[str, str]:
        return {"code": self.code, "severity": self.severity, "detail": self.detail}


@dataclass
class SkillQualityRow:
    name: str
    copies: list = field(default_factory=list)
    sources: list[str] = field(default_factory=list)
    agents: list[str] = field(default_factory=list)
    issues: list[QualityIssue] = field(default_factory=list)
    warning_count: int = 0
    info_count: int = 0

    @property
    def ok(self) -> bool:
        return self.warning_count == 0

    @property
    def status(self) -> str:
        if self.warning_count > 0:
            return "warning"
        if self.info_count > 0:
            return "info"
        return "clean"


@dataclass
class QualitySummary:
    rows: list[SkillQualityRow]
    total_skills: int
    clean_skills: int
    warning_skills: int
    info_only_skills: int
    counts_by_code: dict[str, int]


class _QualityLookupMap(dict):
    """Dict of canonical skill name -> quality metadata with case-insensitive lookup."""

    def __init__(self, data: dict[str, dict]):
        super().__init__(data)
        self._lower = {k.lower(): v for k, v in data.items()}

    def __contains__(self, key: object) -> bool:
        if super().__contains__(key):
            return True
        return isinstance(key, str) and key.lower() in self._lower

    def __getitem__(self, key: str) -> dict:
        if super().__contains__(key):
            return super().__getitem__(key)
        if isinstance(key, str) and key.lower() in self._lower:
            return self._lower[key.lower()]
        raise KeyError(key)

    def get(self, key: str, default=None):
        if super().__contains__(key):
            return super().get(key, default)
        if isinstance(key, str):
            return self._lower.get(key.lower(), default)
        return default


# ---------------------------------------------------------------------------
# Frontmatter & Markdown parsing (stdlib only)


def _strip_inline_comment(value: str) -> str:
    for idx, ch in enumerate(value):
        if ch == "#" and (idx == 0 or value[idx - 1] in (" ", "\t")):
            return value[:idx].rstrip()
    return value.rstrip()


def _unescape_double_quoted(text: str) -> str:
    escapes = {"n": "\n", "r": "\r", "t": "\t", '"': '"', "\\": "\\", "/": "/"}
    out: list[str] = []
    i = 0
    while i < len(text):
        if text[i] == "\\" and i + 1 < len(text):
            nxt = text[i + 1]
            out.append(escapes.get(nxt, nxt))
            i += 2
        else:
            out.append(text[i])
            i += 1
    return "".join(out)


def _fold_yaml_lines(lines: list[str]) -> str:
    """Fold YAML `>` block lines: single newlines fold to spaces; blank/indented lines stay."""
    if not lines:
        return ""
    out: list[str] = []
    prev_blank = False
    prev_indented = False
    for idx, line in enumerate(lines):
        if line == "":
            out.append("\n")
            prev_blank = True
            prev_indented = False
            continue
        is_indented = line.startswith((" ", "\t"))
        if idx == 0:
            out.append(line)
        elif prev_blank:
            out.append(line)
        elif is_indented or prev_indented:
            out.append("\n" + line)
        else:
            out.append(" " + line)
        prev_blank = False
        prev_indented = is_indented
    return "".join(out)


def _apply_chomping(text: str, indicator: str) -> str:
    chomp = "+" if "+" in indicator else ("-" if "-" in indicator else "")
    if chomp == "-":
        return text.rstrip("\n")
    if chomp == "+":
        return text
    stripped = text.rstrip("\n")
    return stripped + ("\n" if stripped else "")


def _parse_block_scalar(lines: list[str], start: int, style: str, indicator: str) -> tuple[str, int]:
    collected: list[str] = []
    idx = start
    while idx < len(lines):
        raw = lines[idx]
        if raw.strip() == "":
            collected.append("")
            idx += 1
            continue
        if raw[0] not in (" ", "\t"):
            break
        collected.append(raw)
        idx += 1

    non_empty = [ln for ln in collected if ln.strip() != ""]
    if not non_empty:
        return "", idx

    explicit_digits = "".join(ch for ch in indicator if ch.isdigit())
    if explicit_digits:
        indent = int(explicit_digits)
    else:
        indent = min(len(ln) - len(ln.lstrip(" \t")) for ln in non_empty)

    trimmed = [ln[indent:] if len(ln) >= indent else "" for ln in collected]
    if style == "|":
        body = "\n".join(trimmed) + "\n"
    else:
        # Trim trailing empty lines before folding unless keep ('+') chomping is set
        if "+" not in indicator:
            while trimmed and trimmed[-1] == "":
                trimmed.pop()
        body = _fold_yaml_lines(trimmed) + ("\n" if trimmed else "")
    return _apply_chomping(body, indicator), idx


def _parse_quoted_scalar(first_rest: str, lines: list[str], next_idx: int) -> tuple[str, int]:
    quote = first_rest[0]
    buf = first_rest[1:]
    idx = next_idx
    chunks: list[str] = []

    while True:
        pos = 0
        while pos < len(buf):
            if quote == '"' and buf[pos] == "\\":
                pos += 2
                continue
            if quote == "'" and buf[pos : pos + 2] == "''":
                pos += 2
                continue
            if buf[pos] == quote:
                inner = buf[:pos]
                chunks.append(inner)
                joined = " ".join(part.strip() if i > 0 else part.rstrip() for i, part in enumerate(chunks))
                if quote == '"':
                    return _unescape_double_quoted(joined), idx
                return joined.replace("''", "'"), idx
            pos += 1

        chunks.append(buf)
        if idx >= len(lines):
            # Unclosed quote: best-effort value
            joined = " ".join(part.strip() for part in chunks)
            return (_unescape_double_quoted(joined) if quote == '"' else joined.replace("''", "'")), idx
        nxt = lines[idx]
        if nxt.strip() != "" and nxt[0] not in (" ", "\t"):
            joined = " ".join(part.strip() for part in chunks)
            return (_unescape_double_quoted(joined) if quote == '"' else joined.replace("''", "'")), idx
        buf = nxt.strip()
        idx += 1


def _parse_yaml_frontmatter(fm_lines: list[str]) -> dict[str, str]:
    result: dict[str, str] = {}
    idx = 0
    total = len(fm_lines)

    while idx < total:
        line = fm_lines[idx]
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            idx += 1
            continue

        match = _TOP_KEY_RE.match(line)
        if not match:
            idx += 1
            continue

        key = match.group(1)
        rest = (match.group(2) or "").strip()

        if rest:
            block_match = _BLOCK_SCALAR_RE.match(rest)
            if block_match:
                val, idx = _parse_block_scalar(fm_lines, idx + 1, block_match.group(1), block_match.group(2) or "")
                result[key] = val
                continue

            if rest[0] in ('"', "'"):
                val, idx = _parse_quoted_scalar(rest, fm_lines, idx + 1)
                result[key] = val
                continue

            scalar = _strip_inline_comment(rest)
            idx += 1
            # Check plain multiline continuation lines
            parts = [scalar]
            while idx < total:
                cont = fm_lines[idx]
                cont_stripped = cont.strip()
                if not cont_stripped:
                    # Blank line in plain scalar only continues if next non-blank is indented plain text
                    look = idx + 1
                    while look < total and not fm_lines[look].strip():
                        look += 1
                    if look < total and fm_lines[look][0] in (" ", "\t") and not fm_lines[look].lstrip().startswith(("-", "#")):
                        parts.append("")
                        idx = look
                        continue
                    break
                if cont[0] not in (" ", "\t") or cont_stripped.startswith("#"):
                    break
                if cont_stripped.startswith("- ") or _TOP_KEY_RE.match(cont_stripped):
                    break
                parts.append(_strip_inline_comment(cont_stripped))
                idx += 1

            joined = "\n".join(
                p for p in (" ".join(seg for seg in chunk.split("\x00") if seg) for chunk in "\x00".join(parts).split("\x00\x00"))
            )
            if joined.lower() in ("null", "~"):
                joined = ""
            result[key] = joined
            continue

        # Empty inline value: either plain indented scalar or nested mapping/list
        idx += 1
        indented_plain: list[str] = []
        structured = False
        while idx < total:
            cont = fm_lines[idx]
            cont_stripped = cont.strip()
            if not cont_stripped:
                idx += 1
                continue
            if cont[0] not in (" ", "\t"):
                break
            if cont_stripped.startswith("#"):
                idx += 1
                continue
            if cont_stripped.startswith("-") or _TOP_KEY_RE.match(cont_stripped):
                structured = True
            elif not structured:
                indented_plain.append(_strip_inline_comment(cont_stripped))
            idx += 1

        if indented_plain and not structured:
            val = " ".join(indented_plain)
            result[key] = "" if val.lower() in ("null", "~") else val
        else:
            result.setdefault(key, "")

    return result


def parse_skill_md(path: str | Path) -> ParsedSkill:
    """Read and parse ``SKILL.md`` into YAML frontmatter and markdown body."""
    try:
        raw = Path(path).read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        return ParsedSkill({}, "", "", 0, has_frontmatter=False, readable=False)

    lines = raw.splitlines()
    line_count = len(lines)
    if not lines or not re.match(r"^---[ \t]*$", lines[0]):
        return ParsedSkill({}, raw, raw, line_count, has_frontmatter=False, readable=True)

    end_idx: int | None = None
    for i in range(1, len(lines)):
        if re.match(r"^(?:---|\.\.\.)[ \t]*$", lines[i]):
            end_idx = i
            break

    if end_idx is None:
        return ParsedSkill({}, raw, raw, line_count, has_frontmatter=False, readable=True)

    fm = _parse_yaml_frontmatter(lines[1:end_idx])
    body = "\n".join(lines[end_idx + 1 :])
    return ParsedSkill(fm, body, raw, line_count, has_frontmatter=True, readable=True)


# ---------------------------------------------------------------------------
# Link extraction & support-file checks


def _strip_code_from_markdown(markdown: str) -> str:
    """Remove fenced code blocks and replace inline code spans so code examples aren't parsed as links."""
    out_lines: list[str] = []
    in_fence = False
    fence_char = ""
    fence_len = 0

    for line in markdown.splitlines():
        if not in_fence:
            m = _FENCE_OPEN_RE.match(line)
            if m:
                in_fence = True
                fence_char = m.group(1)[0]
                fence_len = len(m.group(1))
                continue
            out_lines.append(_INLINE_CODE_RE.sub("code", line))
        else:
            stripped = line.strip()
            if stripped and all(ch == fence_char for ch in stripped) and len(stripped) >= fence_len:
                in_fence = False

    return "\n".join(out_lines)


def _iter_markdown_link_targets(markdown: str):
    """Yield raw link destinations from `[label](destination)` outside code spans/fences."""
    cleaned = _strip_code_from_markdown(markdown)
    n = len(cleaned)
    i = 0
    while i < n:
        if cleaned[i] == "[":
            # Find matching ']' followed immediately by '('
            j = i + 1
            bracket_depth = 1
            while j < n and bracket_depth > 0:
                if cleaned[j] == "\n":
                    break
                if cleaned[j] == "[":
                    bracket_depth += 1
                elif cleaned[j] == "]":
                    bracket_depth -= 1
                j += 1
            if bracket_depth == 0 and j < n and cleaned[j] == "(":
                k = j + 1
                while k < n and cleaned[k] in (" ", "\t"):
                    k += 1
                if k < n and cleaned[k] == "<":
                    end_angle = cleaned.find(">", k + 1)
                    close_paren = cleaned.find(")", end_angle + 1) if end_angle != -1 else -1
                    if end_angle != -1 and close_paren != -1 and "\n" not in cleaned[k:close_paren]:
                        yield cleaned[k : end_angle + 1].strip()
                        i = close_paren + 1
                        continue
                paren_depth = 1
                p = k
                while p < n and paren_depth > 0:
                    if cleaned[p] == "\n":
                        break
                    if cleaned[p] == "(":
                        paren_depth += 1
                    elif cleaned[p] == ")":
                        paren_depth -= 1
                    p += 1
                if paren_depth == 0:
                    raw_dest = cleaned[k : p - 1].strip()
                    # Strip optional trailing title `"..."` or `'...'`
                    raw_dest = re.sub(r"""[ \t]+(?:"[^"]*"|'[^']*')\s*$""", "", raw_dest).strip()
                    if raw_dest:
                        yield raw_dest
                    i = p
                    continue
        i += 1


def _safe_broken_link_label(raw_no_anchor: str, candidate: Path, skill_dir: Path) -> str:
    """Format a broken link target without ever exposing host filesystem paths."""
    is_file_uri = raw_no_anchor.lower().startswith("file:")
    is_abs_like = (
        is_file_uri
        or raw_no_anchor.startswith(("/", "\\", "~"))
        or bool(_WINDOWS_DRIVE_RE.match(raw_no_anchor))
    )
    if not is_abs_like:
        return raw_no_anchor.replace("\\", "/")

    try:
        rel = os.path.relpath(os.path.normpath(str(candidate)), os.path.normpath(str(skill_dir)))
        rel_posix = rel.replace("\\", "/")
        if rel_posix != "." and not rel_posix.startswith("../") and rel_posix != "..":
            return rel_posix
    except (OSError, ValueError):
        pass

    return candidate.name or "<missing>"


def _check_links(markdown: str, skill_dir: Path) -> tuple[list[QualityIssue], set[str]]:
    """Return `(broken_link_issues, existing_resolved_paths)`."""
    issues: list[QualityIssue] = []
    seen_broken: set[str] = set()
    existing_paths: set[str] = set()

    for raw_target in _iter_markdown_link_targets(markdown):
        # Ignore pure anchors
        if raw_target.startswith("#"):
            continue

        # Handle `<...>` and `{...}` placeholders vs CommonMark `<path with spaces>`
        if _PLACEHOLDER_RE.search(raw_target):
            continue
        if raw_target.startswith("<") and raw_target.endswith(">"):
            inner = raw_target[1:-1].strip()
            # `<placeholder>` / `<id>` / `<url>` has no spaces; `<my doc.md>` has whitespace
            if not inner or " " not in inner or _PLACEHOLDER_RE.search(inner):
                continue
            target = inner
        elif _ANGLE_PLACEHOLDER_RE.search(raw_target):
            continue
        else:
            target = raw_target

        # Ignore non-file URI schemes (http, https, mailto, sidecar, conversation, etc.)
        if not _WINDOWS_DRIVE_RE.match(target):
            scheme_match = _SCHEME_RE.match(target)
            if scheme_match and scheme_match.group(1).lower() != "file":
                continue

        no_anchor = target.split("#", 1)[0].split("?", 1)[0].strip()
        if not no_anchor:
            continue

        if no_anchor.lower().startswith("file:"):
            parsed = urllib.parse.urlparse(no_anchor)
            local_str = urllib.request.url2pathname(parsed.path)
            candidate = Path(local_str)
        else:
            unquoted = urllib.parse.unquote(no_anchor)
            candidate = Path(os.path.expanduser(unquoted))
            if not candidate.is_absolute():
                candidate = skill_dir / candidate

        try:
            norm = os.path.normpath(str(candidate))
            exists = Path(norm).exists()
        except OSError:
            norm = str(candidate)
            exists = False

        if exists:
            existing_paths.add(os.path.normcase(norm))
        else:
            label = _safe_broken_link_label(no_anchor, candidate, skill_dir)
            if label not in seen_broken:
                seen_broken.add(label)
                issues.append(QualityIssue("broken-link", "warning", f"broken local link: {label}"))

    return issues, existing_paths


def _is_metadata_file(rel_dir: str, fname: str) -> bool:
    lower = fname.lower()
    if rel_dir == "." and lower == "skill.md":
        return True
    first_dir = rel_dir.replace("\\", "/").split("/")[0].lower()
    if first_dir in METADATA_DIRS:
        return True
    return lower.startswith(METADATA_PREFIXES) or lower in METADATA_FILES


def _is_file_referenced(
    file_path: Path,
    rel_posix: str,
    skill_dir: Path,
    raw_text: str,
    linked_paths: set[str],
) -> bool:
    norm_file = os.path.normcase(os.path.normpath(str(file_path)))
    if norm_file in linked_paths:
        return True

    # Check if any ancestor directory strictly within skill_dir was linked directly
    parent = file_path.parent
    norm_skill_dir = os.path.normcase(os.path.normpath(str(skill_dir)))
    while os.path.normcase(os.path.normpath(str(parent))) != norm_skill_dir:
        if os.path.normcase(os.path.normpath(str(parent))) in linked_paths:
            return True
        if parent.parent == parent:
            break
        parent = parent.parent

    # Mentioned by relative path (forward or backslash)
    rel_pattern = re.compile(
        rf"(?:^|[^A-Za-z0-9_.-])(?:\./)?{re.escape(rel_posix).replace('/', r'[/\\]')}(?:$|[^A-Za-z0-9_.-])",
        re.IGNORECASE,
    )
    if rel_pattern.search(raw_text):
        return True

    # Mentioned by filename (bounded so `a.py` doesn't match `data.py`)
    fname = file_path.name
    fname_pattern = re.compile(
        rf"(?:^|[^A-Za-z0-9_-]){re.escape(fname)}(?:$|[^A-Za-z0-9_-])",
        re.IGNORECASE,
    )
    if fname_pattern.search(raw_text):
        return True

    # Mentioned by parent directory inside the skill (e.g. `references/`, `` `references` ``, `references/*`)
    parts = rel_posix.split("/")
    if len(parts) > 1:
        for depth in range(1, len(parts)):
            dir_rel = "/".join(parts[:depth])
            dir_name = parts[depth - 1]
            for candidate_dir in dict.fromkeys((dir_rel, dir_name)):
                escaped_dir = re.escape(candidate_dir).replace("/", r"[/\\]")
                # Directory trailing slash followed by wildcard, whitespace, delimiter, or end (not another specific filename!)
                dir_slash_re = re.compile(
                    rf"(?:^|[^A-Za-z0-9_.-])(?:\.[\\/])?{escaped_dir}[\\/](?:\*|(?:$|[\s`'\"),.;:!?\]]))",
                    re.IGNORECASE,
                )
                if dir_slash_re.search(raw_text):
                    return True
                # Backticked directory name (`references` or `scripts`) or `<dir> directory/folder`
                dir_code_re = re.compile(
                    rf"`(?:\.[\\/])?{escaped_dir}[\\/]?`|(?:^|[^A-Za-z0-9_-]){escaped_dir}[ \t]+(?:directory|dir|folder)\b",
                    re.IGNORECASE,
                )
                if dir_code_re.search(raw_text):
                    return True

    return False


def _check_unreferenced_files(skill_dir: Path, raw_text: str, linked_paths: set[str]) -> list[QualityIssue]:
    issues: list[QualityIssue] = []
    seen_count = 0
    try:
        for dirpath, dirnames, filenames in os.walk(skill_dir):
            dirnames[:] = sorted(
                d
                for d in dirnames
                if d not in inventory.SKIP_DIRS and not d.startswith(".") and d.lower() not in METADATA_DIRS
            )
            cur_dir = Path(dirpath)
            if cur_dir != skill_dir and "SKILL.md" in filenames:
                dirnames[:] = []
                continue
            rel_dir = os.path.relpath(dirpath, skill_dir)
            for fname in sorted(filenames):
                seen_count += 1
                if seen_count > MAX_SUPPORT_SCAN:
                    return issues
                if fname.startswith(".") or _is_metadata_file(rel_dir, fname):
                    continue
                file_path = cur_dir / fname
                rel_posix = (fname if rel_dir == "." else f"{rel_dir}/{fname}").replace("\\", "/")
                if not _is_file_referenced(file_path, rel_posix, skill_dir, raw_text, linked_paths):
                    issues.append(QualityIssue("unreferenced-file", "info", f"unreferenced support file: {rel_posix}"))
    except OSError:
        return issues
    return issues


# ---------------------------------------------------------------------------
# SkillCopy evaluation & aggregation


def _is_weak_description(desc: str) -> tuple[bool, str]:
    length = len(desc)
    if length < MIN_DESCRIPTION_CHARS:
        return True, f"description is {length} chars (< {MIN_DESCRIPTION_CHARS} char minimum)"
    has_cue = bool(_TRIGGER_CUE_RE.search(desc))
    if not has_cue and (length < CUE_DESCRIPTION_CHARS or len(desc.split()) < 8):
        return True, "description lacks trigger or usage guidance"
    return False, ""


def inspect_copy(copy: inventory.SkillCopy) -> list[QualityIssue]:
    """Run all static quality checks on a single ``SkillCopy``."""
    parsed = parse_skill_md(copy.path)
    if not parsed.readable:
        return []

    issues: list[QualityIssue] = []
    if not parsed.has_frontmatter:
        issues.append(QualityIssue("missing-frontmatter", "warning", "missing or unclosed YAML frontmatter"))
    else:
        name = parsed.frontmatter.get("name", "").strip()
        desc = parsed.frontmatter.get("description", "").strip()
        if not name:
            issues.append(QualityIssue("missing-name", "warning", "frontmatter missing non-empty 'name'"))
        if not desc:
            issues.append(QualityIssue("missing-description", "warning", "frontmatter missing non-empty 'description'"))
        else:
            weak, reason = _is_weak_description(desc)
            if weak:
                issues.append(QualityIssue("weak-description", "warning", reason))
            if len(desc) > MAX_DESCRIPTION_CHARS:
                issues.append(
                    QualityIssue(
                        "overlong-description",
                        "warning",
                        f"description is {len(desc)} chars (> {MAX_DESCRIPTION_CHARS} char limit)",
                    )
                )

    if parsed.line_count > MAX_SKILL_MD_LINES:
        issues.append(
            QualityIssue(
                "oversized-skill-md",
                "warning",
                f"skill file has {parsed.line_count} lines (> {MAX_SKILL_MD_LINES} line limit)",
            )
        )

    skill_dir = Path(copy.path).parent
    link_issues, linked_paths = _check_links(parsed.body, skill_dir)
    issues.extend(link_issues)
    issues.extend(_check_unreferenced_files(skill_dir, parsed.raw, linked_paths))
    return issues


def audit(copies) -> QualitySummary:
    """Evaluate all ``SkillCopy`` entries and aggregate findings by canonical skill name."""
    grouped: dict[str, list[inventory.SkillCopy]] = {}
    canonical_map: dict[str, str] = {}
    for copy in copies:
        key = copy.name.lower()
        canonical = canonical_map.setdefault(key, copy.name)
        grouped.setdefault(canonical, []).append(copy)

    rows: list[SkillQualityRow] = []
    counts_by_code: dict[str, int] = {}

    for canonical_name, group_copies in grouped.items():
        seen_issues: dict[tuple[str, str], QualityIssue] = {}
        for copy in group_copies:
            for issue in inspect_copy(copy):
                seen_issues.setdefault((issue.code, issue.detail), issue)

        # Keep warnings ahead of info findings while preserving discovery order within each severity
        ordered_issues = [
            *(iss for iss in seen_issues.values() if iss.severity == "warning"),
            *(iss for iss in seen_issues.values() if iss.severity != "warning"),
        ]
        warning_count = sum(1 for iss in ordered_issues if iss.severity == "warning")
        info_count = sum(1 for iss in ordered_issues if iss.severity == "info")
        for iss in ordered_issues:
            counts_by_code[iss.code] = counts_by_code.get(iss.code, 0) + 1

        sources = sorted({c.source for c in group_copies})
        agents = sorted({ag for c in group_copies for ag in getattr(c, "agents", ("copilot",))})
        rows.append(
            SkillQualityRow(
                name=canonical_name,
                copies=list(group_copies),
                sources=sources,
                agents=agents,
                issues=ordered_issues,
                warning_count=warning_count,
                info_count=info_count,
            )
        )

    rows.sort(key=lambda r: (-r.warning_count, -r.info_count, r.name.lower()))
    total_skills = len(rows)
    warning_skills = sum(1 for r in rows if r.warning_count > 0)
    info_only_skills = sum(1 for r in rows if r.warning_count == 0 and r.info_count > 0)
    clean_skills = sum(1 for r in rows if r.warning_count == 0 and r.info_count == 0)

    return QualitySummary(
        rows=rows,
        total_skills=total_skills,
        clean_skills=clean_skills,
        warning_skills=warning_skills,
        info_only_skills=info_only_skills,
        counts_by_code=dict(sorted(counts_by_code.items())),
    )


def quality_map(copies) -> dict[str, dict]:
    """Return canonical skill name -> dashboard-safe quality dict (no filesystem paths)."""
    summary = audit(copies)
    data = {
        row.name: {
            "ok": row.ok,
            "status": row.status,
            "warningCount": row.warning_count,
            "infoCount": row.info_count,
            "issues": [issue.to_dict() for issue in row.issues],
        }
        for row in summary.rows
    }
    return _QualityLookupMap(data)


# ---------------------------------------------------------------------------
# Markdown & CSV output


def _md_cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")


def render(summary: QualitySummary, issues_only: bool = False) -> str:
    """Render ``QualitySummary`` as clean Markdown with zero host filesystem paths."""
    out = [
        "# Skill quality audit report",
        "",
        (
            f"- Audited skills: {summary.total_skills} "
            f"({summary.warning_skills} with warnings, {summary.info_only_skills} info-only, {summary.clean_skills} clean)"
        ),
        "- Checks: YAML frontmatter, trigger description cues/length, progressive-disclosure line budget (<= 500 lines), local markdown links, and unreferenced support files.",
        "- Static findings are review signals for skill authoring, not ground-truth task outcome evaluations.",
    ]
    if summary.counts_by_code:
        breakdown = ", ".join(f"{code}: {cnt}" for code, cnt in sorted(summary.counts_by_code.items()))
        out.append(f"- Findings by check: {breakdown}")

    if summary.total_skills == 0:
        out += ["", "No skills found in inventory."]
        return "\n".join(out) + "\n"

    flagged = [r for r in summary.rows if r.issues]
    out += ["", "## Skills with findings", ""]
    if flagged:
        out.append("| Skill | Copies | Sources | Status | Warnings | Infos | Findings |")
        out.append("|---|---|---|---|---|---|---|")
        for r in flagged:
            findings = "; ".join(f"{iss.code} ({iss.detail})" for iss in r.issues)
            out.append(
                f"| {_md_cell(r.name)} | {len(r.copies)} | {_md_cell(', '.join(r.sources))} "
                f"| {r.status} | {r.warning_count} | {r.info_count} | {_md_cell(findings)} |"
            )
    else:
        out.append("None.")

    if not issues_only:
        clean = sorted((r for r in summary.rows if not r.issues), key=lambda r: r.name.lower())
        out += ["", "## Clean skills", ""]
        out += [f"- {r.name} ({', '.join(r.sources)})" for r in clean] or ["None."]

    return "\n".join(out) + "\n"


def write_csv(summary: QualitySummary, path: str | Path) -> None:
    """Write per-skill static quality audit rows to CSV."""
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["skill", "copies", "sources", "paths", "warnings", "infos", "issues", "status"])
        for r in summary.rows:
            writer.writerow(
                [
                    r.name,
                    len(r.copies),
                    ";".join(r.sources),
                    ";".join(c.path for c in r.copies),
                    r.warning_count,
                    r.info_count,
                    ";".join(f"{iss.code}: {iss.detail}" for iss in r.issues),
                    r.status,
                ]
            )

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from tracker.inventory import SkillCopy

_FRONTMATTER_RE = re.compile(r"\A---\s*\r?\n(.*?)\r?\n---(?:\r?\n|\Z)", re.DOTALL)
_MD_LINK_RE = re.compile(r"\[[^\]]+\]\(([^)#\s]+)(?:#[^)]*)?\)")
_JSON_BLOCK_RE = re.compile(r"```(?:json)?\s*\r?\n(.*?)\r?\n```", re.DOTALL)

RunnerFn = Callable[[str, Path, str | None], str]
JudgeFn = Callable[[str, list[str], str], tuple[bool, str]]

DEFAULT_CLI_RUNNERS: dict[str, list[str]] = {
    "claude": ["claude", "-p", "{prompt}"],
    "codex": ["codex", "exec", "{prompt}"],
    "copilot": ["copilot", "-p", "{prompt}"],
    "antigravity": ["gemini", "-p", "{prompt}"],
    "gemini": ["gemini", "-p", "{prompt}"],
    "pi": ["pi", "-p", "{prompt}"],
    "hermes": ["hermes", "-p", "{prompt}"],
}


@dataclass
class LintResult:
    skill: str
    path: str
    ok: bool
    issues: list[str] = field(default_factory=list)
    has_provider_evals: bool = False


@dataclass
class EvalCase:
    case_id: str
    prompt: str
    expected_output: str = ""
    assertions: list[dict] = field(default_factory=list)
    expectations: list[str] = field(default_factory=list)
    files: dict[str, str] = field(default_factory=dict)


@dataclass
class ArmOutcome:
    passed: bool
    checks_passed: int
    checks_total: int
    details: list[str] = field(default_factory=list)


@dataclass
class CaseResult:
    case_id: str
    with_skill: ArmOutcome | None = None
    baseline: ArmOutcome | None = None


@dataclass
class EvalRunResult:
    skill: str
    eval_source: str
    eval_path: str
    agent: str
    runner: str
    ts: str
    cases_total: int
    with_skill_passed: int
    with_skill_rate: float | None
    baseline_passed: int | None
    baseline_rate: float | None
    benefit_delta: float | None
    lint: LintResult | None = None
    duration_ms: int = 0
    case_results: list[CaseResult] = field(default_factory=list)
    dry_run: bool = False


def _parse_frontmatter(text: str) -> dict[str, str]:
    match = _FRONTMATTER_RE.match(text)
    if not match:
        return {}
    meta: dict[str, str] = {}
    current_key: str | None = None
    for raw_line in match.group(1).splitlines():
        if not raw_line.strip() or raw_line.lstrip().startswith("#"):
            continue
        if raw_line[0].isspace() and current_key is not None:
            meta[current_key] = (meta[current_key] + " " + raw_line.strip()).strip()
            continue
        if ":" not in raw_line:
            continue
        key, val = raw_line.split(":", 1)
        current_key = key.strip()
        cleaned = val.strip().strip("\"'")
        if cleaned in (">", "|", ">-", "|-"):
            cleaned = ""
        meta[current_key] = cleaned
    return meta


def lint_skill(copy: SkillCopy) -> LintResult:
    """Run static health checks on a skill copy without invoking any LLM."""
    skill_md = Path(copy.path)
    issues: list[str] = []
    if not skill_md.is_file():
        return LintResult(copy.name, copy.path, False, ["SKILL.md file not found"], False)

    try:
        text = skill_md.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return LintResult(copy.name, copy.path, False, [f"unreadable SKILL.md: {exc}"], False)

    meta = _parse_frontmatter(text)
    if not meta:
        issues.append("missing YAML frontmatter")
    else:
        if not meta.get("name"):
            issues.append("frontmatter missing 'name'")
        desc = meta.get("description", "")
        if not desc:
            issues.append("frontmatter missing 'description'")
        elif len(desc) < 20:
            issues.append("frontmatter 'description' is very short (<20 chars); may under-trigger")

    skill_dir = skill_md.parent
    for target in _MD_LINK_RE.findall(text):
        if "://" in target or target.startswith(("mailto:", "/")):
            continue
        if not (skill_dir / target).exists():
            issues.append(f"broken relative link: {target}")

    has_evals = (skill_dir / "evals" / "evals.json").is_file() or (skill_dir / "evals.json").is_file()
    return LintResult(copy.name, copy.path, len(issues) == 0, issues, has_evals)


def discover_eval_file(
    skill_name: str,
    copies: list[SkillCopy],
    evals_dir: Path | None = None,
    eval_file: Path | None = None,
) -> tuple[Path | None, str | None]:
    """Find the eval suite for a skill: explicit file -> user evals dir -> provider bundle."""
    if eval_file is not None:
        return (eval_file, "user") if eval_file.is_file() else (None, None)

    if evals_dir is not None and evals_dir.is_dir():
        for candidate in (evals_dir / f"{skill_name}.json", evals_dir / skill_name / "evals.json"):
            if candidate.is_file():
                return candidate, "user"
        lower_target = f"{skill_name.lower()}.json"
        for entry in sorted(evals_dir.iterdir()):
            if entry.is_file() and entry.name.lower() == lower_target:
                return entry, "user"

    for copy in copies:
        if copy.name.lower() != skill_name.lower():
            continue
        skill_dir = Path(copy.path).parent
        for candidate in (skill_dir / "evals" / "evals.json", skill_dir / "evals.json"):
            if candidate.is_file():
                return candidate, "provider"

    return None, None


def _normalize_assertions(raw_assertions) -> list[dict]:
    normalized: list[dict] = []
    if not isinstance(raw_assertions, list):
        return normalized
    for item in raw_assertions:
        if isinstance(item, dict) and item.get("type"):
            normalized.append(dict(item))
        elif isinstance(item, str) and item.strip():
            normalized.append({"type": "contains", "value": item})
    return normalized


def load_eval_suite(path: Path) -> list[EvalCase]:
    """Load eval cases from a JSON file (supports {'evals': [...]} or [...])."""
    data = json.loads(path.read_text(encoding="utf-8"))
    raw_cases = data.get("evals", []) if isinstance(data, dict) else data
    if not isinstance(raw_cases, list):
        raise ValueError(f"Invalid eval suite format in {path}")
    cases: list[EvalCase] = []
    for idx, item in enumerate(raw_cases, start=1):
        if not isinstance(item, dict):
            continue
        prompt = item.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            continue
        case_id = str(item.get("id", idx))
        expected_output = str(item.get("expected_output", "") or "")
        assertions = _normalize_assertions(item.get("assertions"))
        raw_exp = item.get("expectations", [])
        expectations = [str(x) for x in raw_exp if isinstance(x, str) and x.strip()] if isinstance(raw_exp, list) else []
        raw_files = item.get("files", {})
        files = {str(k): str(v) for k, v in raw_files.items()} if isinstance(raw_files, dict) else {}
        cases.append(
            EvalCase(
                case_id=case_id,
                prompt=prompt,
                expected_output=expected_output,
                assertions=assertions,
                expectations=expectations,
                files=files,
            )
        )
    return cases


def _extract_json(text: str):
    try:
        return json.loads(text)
    except ValueError:
        pass
    match = _JSON_BLOCK_RE.search(text)
    if match:
        return json.loads(match.group(1))
    raise ValueError("not valid JSON")


def grade_assertions(output: str, workdir: Path, assertions: list[dict]) -> tuple[int, int, list[str]]:
    """Evaluate deterministic assertions against output and workspace."""
    passed = 0
    details: list[str] = []
    for idx, assertion in enumerate(assertions, start=1):
        atype = assertion.get("type")
        ok = False
        reason = ""
        if atype == "contains":
            val = str(assertion.get("value", ""))
            case_sensitive = assertion.get("case_sensitive", True)
            haystack = output if case_sensitive else output.lower()
            needle = val if case_sensitive else val.lower()
            ok = needle in haystack
            reason = f"contains({val!r})"
        elif atype == "not_contains":
            val = str(assertion.get("value", ""))
            case_sensitive = assertion.get("case_sensitive", True)
            haystack = output if case_sensitive else output.lower()
            needle = val if case_sensitive else val.lower()
            ok = needle not in haystack
            reason = f"not_contains({val!r})"
        elif atype == "regex":
            pattern = str(assertion.get("pattern", ""))
            ok = bool(re.search(pattern, output, re.MULTILINE))
            reason = f"regex({pattern!r})"
        elif atype == "file_exists":
            rel = str(assertion.get("path", ""))
            ok = bool(rel) and (workdir / rel).is_file()
            reason = f"file_exists({rel!r})"
        elif atype == "file_contains":
            rel = str(assertion.get("path", ""))
            val = str(assertion.get("value", ""))
            target = workdir / rel
            ok = target.is_file() and val in target.read_text(encoding="utf-8", errors="replace")
            reason = f"file_contains({rel!r})"
        elif atype == "json_valid":
            try:
                _extract_json(output)
                ok = True
            except ValueError:
                ok = False
            reason = "json_valid"
        elif atype == "command":
            cmd = str(assertion.get("cmd", ""))
            env = dict(os.environ)
            env["EVAL_OUTPUT"] = output
            proc = subprocess.run(cmd, shell=True, cwd=workdir, env=env, capture_output=True, text=True)
            ok = proc.returncode == 0
            reason = f"command({cmd!r}) -> exit {proc.returncode}"
        else:
            reason = f"unknown assertion type {atype!r}"
        if ok:
            passed += 1
        details.append(f"{'PASS' if ok else 'FAIL'} #{idx}: {reason}")
    return passed, len(assertions), details


def _format_cmd_template(template: list[str], prompt: str, workdir: Path, skill_dir: Path | None, mode: str) -> list[str]:
    replacements = {
        "{prompt}": prompt,
        "{workdir}": str(workdir),
        "{skill_dir}": str(skill_dir) if skill_dir else "",
        "{mode}": mode,
    }
    cmd: list[str] = []
    for token in template:
        val = token
        for key, rep in replacements.items():
            val = val.replace(key, rep)
        cmd.append(val)
    return cmd


def build_cli_runner(
    runner_name: str | None = None,
    runner_cmd: str | None = None,
    skill_dir: Path | None = None,
    timeout: int = 120,
) -> tuple[RunnerFn, str]:
    """Create a runner callable backed by a local agent CLI or custom command template."""
    if runner_cmd:
        template = shlex.split(runner_cmd)
        label = template[0] if template else "custom"
    else:
        chosen = runner_name
        if not chosen or chosen == "all":
            for candidate, tmpl in DEFAULT_CLI_RUNNERS.items():
                if shutil.which(tmpl[0]):
                    chosen = candidate
                    break
        if not chosen or chosen not in DEFAULT_CLI_RUNNERS:
            raise RuntimeError("No supported agent CLI found on PATH; pass --runner-cmd or --runner")
        template = DEFAULT_CLI_RUNNERS[chosen]
        if not shutil.which(template[0]):
            raise RuntimeError(f"Agent CLI {template[0]!r} not found on PATH")
        label = chosen

    def _run(prompt: str, workdir: Path, skill_text: str | None) -> str:
        mode = "with-skill" if skill_text is not None else "baseline"
        full_prompt = prompt
        if skill_text is not None:
            full_prompt = (
                "Follow these skill instructions while completing the task:\n\n"
                f"<skill_instructions>\n{skill_text}\n</skill_instructions>\n\n"
                f"Task:\n{prompt}"
            )
        cmd = _format_cmd_template(template, full_prompt, workdir, skill_dir, mode)
        proc = subprocess.run(cmd, cwd=workdir, capture_output=True, text=True, timeout=timeout)
        if proc.returncode != 0 and not proc.stdout.strip():
            raise RuntimeError(f"Runner {label!r} exited {proc.returncode}: {proc.stderr.strip()}")
        return proc.stdout

    return _run, label


def build_judge(judge_cmd: str, timeout: int = 90) -> JudgeFn:
    """Create an LLM-as-judge callable from a command template containing {prompt}."""
    template = shlex.split(judge_cmd)

    def _judge(output: str, expectations: list[str], expected_output: str) -> tuple[bool, str]:
        criteria = "\n".join(f"- {item}" for item in expectations) if expectations else f"- {expected_output}"
        prompt = (
            "Grade whether the following assistant output satisfies ALL criteria.\n"
            "Respond ONLY with JSON: {\"passed\": true/false, \"reason\": \"...\"}\n\n"
            f"Criteria:\n{criteria}\n\nOutput:\n{output}"
        )
        cmd = _format_cmd_template(template, prompt, Path.cwd(), None, "judge")
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        try:
            parsed = _extract_json(proc.stdout)
            return bool(parsed.get("passed")), str(parsed.get("reason", ""))
        except Exception:
            lowered = proc.stdout.strip().lower()
            return lowered.startswith("pass") or '"passed": true' in lowered, proc.stdout.strip()[:120]

    return _judge


def _evaluate_arm(
    case: EvalCase,
    runner: RunnerFn,
    skill_text: str | None,
    judge: JudgeFn | None,
) -> ArmOutcome:
    with tempfile.TemporaryDirectory() as tmp:
        workdir = Path(tmp)
        for rel, content in case.files.items():
            target = workdir / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")

        output = runner(case.prompt, workdir, skill_text)

        if case.assertions:
            passed_cnt, total_cnt, details = grade_assertions(output, workdir, case.assertions)
            return ArmOutcome(passed=(passed_cnt == total_cnt), checks_passed=passed_cnt, checks_total=total_cnt, details=details)

        if (case.expectations or case.expected_output) and judge is not None:
            ok, reason = judge(output, case.expectations, case.expected_output)
            return ArmOutcome(passed=ok, checks_passed=1 if ok else 0, checks_total=1, details=[reason or ("PASS" if ok else "FAIL")])

        # Fallback: non-empty output check when no assertions/judge are configured
        ok = bool(output and output.strip())
        return ArmOutcome(passed=ok, checks_passed=1 if ok else 0, checks_total=1, details=["non-empty output" if ok else "empty output"])


def run_skill_eval(
    skill_name: str,
    copies: list[SkillCopy],
    *,
    runner: RunnerFn | None = None,
    judge: JudgeFn | None = None,
    eval_file: Path | None = None,
    evals_dir: Path | None = None,
    run_baseline: bool = True,
    dry_run: bool = False,
    agent: str = "all",
    runner_label: str = "custom",
    now: datetime | None = None,
) -> EvalRunResult:
    """Run an A/B evaluation for a skill and return structured results."""
    matching = [c for c in copies if c.name.lower() == skill_name.lower()]
    canonical_name = matching[0].name if matching else skill_name
    lint = lint_skill(matching[0]) if matching else None
    skill_text = ""
    if matching and Path(matching[0].path).is_file():
        skill_text = Path(matching[0].path).read_text(encoding="utf-8", errors="replace")

    found_path, eval_source = discover_eval_file(canonical_name, copies, evals_dir=evals_dir, eval_file=eval_file)
    if found_path is None or eval_source is None:
        raise FileNotFoundError(f"No eval suite found for skill {canonical_name!r}")

    cases = load_eval_suite(found_path)
    stamp = (now or datetime.now(timezone.utc)).isoformat()
    if dry_run:
        return EvalRunResult(
            skill=canonical_name,
            eval_source=eval_source,
            eval_path=str(found_path),
            agent=agent,
            runner=runner_label,
            ts=stamp,
            cases_total=len(cases),
            with_skill_passed=0,
            with_skill_rate=None,
            baseline_passed=None,
            baseline_rate=None,
            benefit_delta=None,
            lint=lint,
            dry_run=True,
        )

    if runner is None:
        raise ValueError("A runner callable is required when dry_run is False")

    t0 = time.monotonic()
    case_results: list[CaseResult] = []
    with_passed = 0
    base_passed = 0
    for case in cases:
        w_out = _evaluate_arm(case, runner, skill_text, judge)
        if w_out.passed:
            with_passed += 1
        b_out = None
        if run_baseline:
            b_out = _evaluate_arm(case, runner, None, judge)
            if b_out.passed:
                base_passed += 1
        case_results.append(CaseResult(case_id=case.case_id, with_skill=w_out, baseline=b_out))

    elapsed_ms = int((time.monotonic() - t0) * 1000)
    total = len(cases)
    w_rate = round(with_passed / total, 4) if total else 0.0
    b_rate = round(base_passed / total, 4) if (total and run_baseline) else None
    delta = round(w_rate - b_rate, 4) if b_rate is not None else None

    return EvalRunResult(
        skill=canonical_name,
        eval_source=eval_source,
        eval_path=str(found_path),
        agent=agent,
        runner=runner_label,
        ts=stamp,
        cases_total=total,
        with_skill_passed=with_passed,
        with_skill_rate=w_rate,
        baseline_passed=base_passed if run_baseline else None,
        baseline_rate=b_rate,
        benefit_delta=delta,
        lint=lint,
        duration_ms=elapsed_ms,
        case_results=case_results,
        dry_run=False,
    )


def eval_record_from_run(result: EvalRunResult) -> dict:
    """Convert an EvalRunResult into a compact, privacy-safe JSONL record."""
    return {
        "type": "eval",
        "ts": result.ts,
        "skill": result.skill,
        "agent": result.agent,
        "runner": result.runner,
        "eval_source": result.eval_source,
        "cases_total": result.cases_total,
        "with_skill_passed": result.with_skill_passed,
        "with_skill_rate": result.with_skill_rate,
        "baseline_passed": result.baseline_passed,
        "baseline_rate": result.baseline_rate,
        "benefit_delta": result.benefit_delta,
        "lint_ok": result.lint.ok if result.lint else None,
        "lint_issues": len(result.lint.issues) if result.lint else 0,
        "duration_ms": result.duration_ms,
    }

#!/usr/bin/env python3
"""Tier-one submission checks for the Innowise AI Engineering course.

Static only: never runs student code, never needs an API key.
Detects the task from the PR branch name (convention: phaseN/taskM-slug),
then applies that task's manifest from tasks.yml.

Exit code 0 = pass (warnings allowed), 1 = fail.
Writes ci_result.json for the Notion write-back step.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, asdict, field
from glob import glob
from pathlib import Path

import yaml

# --------------------------------------------------------------------------- #
# Models that are dead or off-course. Fail on dead, warn on off-course.
# --------------------------------------------------------------------------- #
# NOTE: these are regexes, not substrings. `embedding-001` must NOT match inside
# `gemini-embedding-001`, which is the current course standard.
DEAD_MODELS = {
    "text-embedding-004": (r"text-embedding-004",
                           "retired by Google (deprecated 14 Jan 2026)"),
    "embedding-001": (r"(?<![\w-])embedding-001",
                      "retired by Google (14 Aug 2025)"),
    "text-embedding-ada-002": (r"text-embedding-ada-002",
                               "long superseded by OpenAI"),
    "claude-3-sonnet": (r"claude-3-sonnet", "retired model generation"),
    "claude-3-opus": (r"claude-3-opus", "retired model generation"),
    "gpt-3.5-turbo": (r"gpt-3\.5-turbo", "retired model generation"),
}

OFF_COURSE = {
    "text-embedding-3-small": (r"text-embedding-3-small",
                               "course standard is models/gemini-embedding-001"),
    "text-embedding-3-large": (r"text-embedding-3-large",
                               "course standard is models/gemini-embedding-001"),
    "OpenAIEmbeddings": (r"\bOpenAIEmbeddings\b",
                         "course standard is GoogleGenerativeAIEmbeddings"),
    "dated model alias": (r"claude-[a-z]+-\d(?:-\d)?-\d{8}",
                          "use the bare alias, e.g. claude-haiku-4-5"),
}

DEAD_RE = {k: (re.compile(p), why) for k, (p, why) in DEAD_MODELS.items()}
OFF_RE = {k: (re.compile(p), why) for k, (p, why) in OFF_COURSE.items()}

# Secret patterns. Deliberately length-bounded to avoid matching docs examples.
SECRET_PATTERNS = [
    (re.compile(r"sk-ant-api\d\d-[A-Za-z0-9_\-]{40,}"), "Anthropic API key"),
    (re.compile(r"AIza[A-Za-z0-9_\-]{33,}"), "Google API key"),
    (re.compile(r"sk-proj-[A-Za-z0-9_\-]{40,}"), "OpenAI project key"),
    (re.compile(r"\bghp_[A-Za-z0-9]{36}\b"), "GitHub personal access token"),
    (re.compile(r"lsv2_(pt|sk)_[A-Za-z0-9]{32,}"), "LangSmith API key"),
]

# A line containing any of these is treated as an illustrative placeholder.
PLACEHOLDER_HINTS = ("...", "xxx", "your-", "your_", "<", "example", "placeholder")

SKIP_DIRS = {".git", ".venv", "venv", "__pycache__", "node_modules", ".mypy_cache",
             ".ruff_cache", ".pytest_cache", "chroma_db", ".idea", ".vscode"}
TEXT_SUFFIXES = {".py", ".md", ".txt", ".json", ".yml", ".yaml", ".toml", ".cfg",
                 ".ini", ".env", ".sh", ".ipynb", ".html", ".js"}

GOLDEN_CATEGORIES = ("happy_path", "edge_case", "out_of_scope", "ambiguous")


# --------------------------------------------------------------------------- #
@dataclass
class Result:
    name: str
    status: str          # pass | warn | fail | skip
    detail: str = ""
    items: list[str] = field(default_factory=list)
    # Render items as one fenced code block instead of a bullet each. Needed for
    # compiler output, where leading indentation is meaningful: as bullets, the
    # caret alignment line turns into a code box that looks empty.
    block: bool = False


ICON = {"pass": "✅", "warn": "⚠️", "fail": "❌", "skip": "➖"}


def walk_files(root: Path):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for fn in filenames:
            p = Path(dirpath) / fn
            if p.suffix.lower() in TEXT_SUFFIXES or p.name.startswith("."):
                yield p


def read_text(p: Path) -> str:
    try:
        return p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


# --------------------------------------------------------------------------- #
# checks
# --------------------------------------------------------------------------- #
def check_secrets(root: Path) -> Result:
    hits: list[str] = []

    for p in walk_files(root):
        rel = p.relative_to(root)
        # A committed .env is a finding regardless of content.
        if p.name == ".env":
            hits.append(f"{rel} — committed .env file (add it to .gitignore and rotate any key inside)")
            continue
        if p.name in {".env.example", ".env.sample", ".env.template"}:
            continue
        for i, line in enumerate(read_text(p).splitlines(), 1):
            low = line.lower()
            if any(h in low for h in PLACEHOLDER_HINTS):
                continue
            for pat, label in SECRET_PATTERNS:
                if pat.search(line):
                    hits.append(f"{rel}:{i} — looks like a live {label}")
                    break

    if hits:
        return Result("Secret scan", "fail",
                      "Credentials must never be committed. Rotate anything found here "
                      "immediately — git history keeps it even after deletion.", hits)
    return Result("Secret scan", "pass", "No committed credentials found.")


def check_gitignore(root: Path) -> Result:
    gi = root / ".gitignore"
    if not gi.exists():
        return Result("gitignore", "warn", "No .gitignore at repository root.")
    body = read_text(gi)
    missing = [n for n in (".env",) if n not in body]
    if missing:
        return Result("gitignore", "warn",
                      ".gitignore does not list: " + ", ".join(missing))
    return Result("gitignore", "pass", ".env is ignored.")


def check_models(task_dir: Path) -> Result:
    dead: list[str] = []
    warn: list[str] = []
    for p in walk_files(task_dir):
        rel = p.relative_to(task_dir.parent) if task_dir.parent != task_dir else p.name
        for i, line in enumerate(read_text(p).splitlines(), 1):
            for token, (pat, why) in DEAD_RE.items():
                if pat.search(line):
                    dead.append(f"{rel}:{i} — '{token}': {why}")
            for token, (pat, why) in OFF_RE.items():
                if pat.search(line):
                    warn.append(f"{rel}:{i} — '{token}': {why}")
    if dead:
        return Result("Model references", "fail",
                      "This code references a model that no longer exists. It will fail "
                      "at runtime, not at import.", dead + warn)
    if warn:
        return Result("Model references", "warn",
                      "Works, but diverges from the course standard.", warn)
    return Result("Model references", "pass", "All model references are current.")


def check_structure(task_dir: Path, required: list[str]) -> Result:
    if not required:
        return Result("Required files", "skip", "No manifest entry for this task.")
    missing = []
    for pattern in required:
        if not glob(str(task_dir / pattern), recursive=True):
            missing.append(pattern)
    if missing:
        return Result("Required files", "fail",
                      f"Expected in {task_dir.name}/ per the task page.",
                      [f"missing: {m}" for m in missing])
    return Result("Required files", "pass", f"All {len(required)} expected files present.")


def check_compile(task_dir: Path) -> Result:
    py = [str(p) for p in task_dir.rglob("*.py")
          if not any(part in SKIP_DIRS for part in p.parts)]
    if not py:
        return Result("Python syntax", "skip", "No .py files found.")
    proc = subprocess.run([sys.executable, "-m", "py_compile", *py],
                          capture_output=True, text=True)
    if proc.returncode != 0:
        # Absolute runner paths (/home/runner/work/repo/repo/...) are noise to a
        # student reading this on the PR; show the path relative to the checkout.
        cwd = os.getcwd().rstrip("/") + "/"
        lines = [l.replace(cwd, "")
                 for l in (proc.stderr or "").splitlines() if l.strip()][:12]
        return Result("Python syntax", "fail",
                      "Code does not compile — this would fail before anything ran.",
                      lines, block=True)
    return Result("Python syntax", "pass", f"{len(py)} file(s) compile cleanly.")


def check_lint(task_dir: Path) -> Result:
    # Lint is a warn-level check, so a missing ruff must degrade to a warning.
    # Letting FileNotFoundError escape kills the run before the summary is
    # written, and then the PR gets no comment at all.
    try:
        proc = subprocess.run(
            ["ruff", "check", "--exit-zero", "--quiet",
             "--select", "F,E9",          # real errors only: undefined names, syntax
             "--output-format", "concise", str(task_dir)],
            capture_output=True, text=True)
    except FileNotFoundError:
        return Result("Lint (errors only)", "warn",
                      "ruff is not installed - lint skipped. "
                      "Check that requirements.txt was installed.")
    out = [l for l in (proc.stdout or "").splitlines() if l.strip()]
    if out:
        return Result("Lint (errors only)", "warn",
                      f"{len(out)} probable error(s) — undefined names, unused imports.",
                      out[:15])
    return Result("Lint (errors only)", "pass", "No probable errors found.")


def check_notes(task_dir: Path, spec: dict) -> Result:
    """Mechanical check that a written deliverable exists and is substantive."""
    fname = spec.get("file")
    if not fname:
        return Result("Written notes", "skip", "")
    matches = glob(str(task_dir / fname))
    if not matches:
        return Result("Written notes", "fail", f"{fname} not found.")
    body = read_text(Path(matches[0]))
    words = len(body.split())
    min_words = spec.get("min_words", 0)
    problems = []
    if words < min_words:
        problems.append(f"{fname} has {words} words; expected at least {min_words}")

    min_items = spec.get("min_items")
    if min_items:
        label = spec.get("item_label", "bug")
        found = len(re.findall(rf"(?im)^\s*#{{1,4}}.*\b{label}\b|^\s*\**\s*{label}\s*#?\d",
                               body))
        if found < min_items:
            problems.append(
                f"{fname} appears to document {found} {label}(s); this task expects "
                f"at least {min_items}. Use a heading per {label}.")
    if problems:
        return Result("Written notes", "fail", "", problems)
    return Result("Written notes", "pass", f"{fname}: {words} words.")


def check_golden_set(task_dir: Path, spec: dict) -> Result:
    fname = spec.get("file", "golden_set.json")
    matches = glob(str(task_dir / fname))
    if not matches:
        return Result("Golden set", "fail", f"{fname} not found.")
    try:
        data = json.loads(read_text(Path(matches[0])))
    except json.JSONDecodeError as e:
        return Result("Golden set", "fail", f"{fname} is not valid JSON: {e}")

    cases = data.get("cases", data) if isinstance(data, dict) else data
    if not isinstance(cases, list):
        return Result("Golden set", "fail",
                      f"{fname} must be a list of cases, or an object with a 'cases' list.")

    problems: list[str] = []
    min_cases = spec.get("min_cases", 15)
    if len(cases) < min_cases:
        problems.append(f"{len(cases)} cases; task requires at least {min_cases}")

    counts = {c: 0 for c in GOLDEN_CATEGORIES}
    seen_questions: dict[str, int] = {}
    for idx, case in enumerate(cases):
        if not isinstance(case, dict):
            problems.append(f"case {idx} is not an object")
            continue
        cid = case.get("id", f"index {idx}")
        q = str(case.get("question", "")).strip().lower()
        if not q:
            problems.append(f"{cid}: empty or missing 'question'")
        else:
            if q in seen_questions:
                problems.append(f"{cid}: duplicate question (also case {seen_questions[q]})")
            seen_questions[q] = idx

        cat = case.get("category")
        if cat not in GOLDEN_CATEGORIES:
            problems.append(f"{cid}: category must be one of {', '.join(GOLDEN_CATEGORIES)}")
        else:
            counts[cat] += 1

        declared = bool(case.get("should_refuse", False))
        if cat == "out_of_scope" and not declared:
            problems.append(f"{cid}: out_of_scope case must set should_refuse true")
        if declared and cat != "out_of_scope":
            problems.append(f"{cid}: should_refuse is true but category is '{cat}'")
        # An out_of_scope case is a refusal case even if the flag is missing,
        # so don't also demand expected facts from it.
        refuse = declared or cat == "out_of_scope"
        if not refuse and not case.get("expected_facts"):
            problems.append(f"{cid}: answerable case needs non-empty 'expected_facts'")
        if not refuse and not case.get("expected_source"):
            problems.append(f"{cid}: answerable case needs 'expected_source'")

    for cat, minimum in (spec.get("min_per_category") or {}).items():
        if counts.get(cat, 0) < minimum:
            problems.append(f"only {counts.get(cat, 0)} '{cat}' case(s); at least "
                            f"{minimum} required — see the coverage table in Module 6")

    summary = ", ".join(f"{c}={counts[c]}" for c in GOLDEN_CATEGORIES)
    if problems:
        return Result("Golden set", "fail", f"{len(cases)} cases ({summary})", problems[:20])
    return Result("Golden set", "pass", f"{len(cases)} cases ({summary})")


# --------------------------------------------------------------------------- #
def detect_task(branch: str) -> str | None:
    m = re.match(r"(phase[1-5])/(task\d+)", branch.strip().lower())
    return f"{m.group(1)}/{m.group(2)}" if m else None


def resolve_task_dir(root: Path, dir_glob: str | None, task_key: str) -> Path | None:
    patterns = [dir_glob] if dir_glob else []
    phase, task = task_key.split("/")
    patterns += [f"{phase}/{task}*", f"{phase}/*{task}*", f"{task}*"]
    for pat in patterns:
        for hit in sorted(glob(str(root / pat))):
            if Path(hit).is_dir():
                return Path(hit)
    return None


def render(results: list[Result], task_key: str | None, branch: str,
           task_name: str, task_dir: Path | None, root: Path) -> str:
    worst = "pass"
    if any(r.status == "warn" for r in results):
        worst = "warn"
    if any(r.status == "fail" for r in results):
        worst = "fail"

    head = {"pass": "### ✅ Tier-one checks passed",
            "warn": "### ⚠️ Passed with warnings",
            "fail": "### ❌ Tier-one checks failed"}[worst]

    lines = [head, ""]
    lines.append(f"**Branch** `{branch}`  ")
    lines.append(f"**Task** {task_name or 'unrecognised'}  ")
    if task_dir:
        lines.append(f"**Directory** `{task_dir.relative_to(root)}`")
    lines += ["", "| | Check | Result |", "|---|---|---|"]
    for r in results:
        lines.append(f"| {ICON[r.status]} | {r.name} | {r.detail or '—'} |")

    detailed = [r for r in results if r.items]
    if detailed:
        lines += ["", "#### Details", ""]
        for r in detailed:
            lines.append(f"**{r.name}**")
            if r.block:
                # One fenced block: indentation and caret alignment survive, and
                # a bullet's 4-space rule cannot swallow the lines.
                lines += ["", "```text", *r.items, "```"]
            else:
                for it in r.items:
                    lines.append(f"- `{it}`" if ":" in it else f"- {it}")
            lines.append("")

    lines += ["", "---",
              "*These are mechanical checks only. They do not assess whether your "
              "reasoning or design decisions are correct — a mentor reviews that "
              "separately. Passing is necessary, not sufficient.*"]
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--branch", default=os.environ.get("SUBMISSION_BRANCH", ""))
    ap.add_argument("--root", default=".")
    ap.add_argument("--manifest", default=str(Path(__file__).with_name("tasks.yml")))
    ap.add_argument("--author", default=os.environ.get("PR_AUTHOR", ""))
    ap.add_argument("--pr", default=os.environ.get("PR_NUMBER", ""))
    ap.add_argument("--out", default="ci_result.json")
    ap.add_argument("--summary-out", default="ci_summary.md")
    ap.add_argument("--fail-on-warn", action="store_true")
    args = ap.parse_args()

    root = Path(args.root).resolve()
    manifest = yaml.safe_load(read_text(Path(args.manifest))) or {}
    tasks = manifest.get("tasks", {})

    task_key = detect_task(args.branch)
    spec = tasks.get(task_key, {}) if task_key else {}
    task_name = spec.get("name", task_key or "")

    results: list[Result] = []

    # Repo-wide checks always run.
    results.append(check_secrets(root))
    results.append(check_gitignore(root))

    task_dir = resolve_task_dir(root, spec.get("dir_glob"), task_key) if task_key else None

    if task_key is None:
        results.append(Result(
            "Task detection", "warn",
            f"Branch '{args.branch}' does not match phaseN/taskM-slug, so task-specific "
            "checks were skipped. Rename the branch to match the submission convention."))
    elif task_dir is None:
        results.append(Result(
            "Task detection", "fail",
            f"Recognised {task_key} from the branch, but found no matching directory. "
            f"Expected something like {task_key.replace('/', '/')}_*/"))
    else:
        results.append(Result("Task detection", "pass",
                              f"{task_key} → {task_dir.relative_to(root)}"))
        results.append(check_structure(task_dir, spec.get("required", [])))
        results.append(check_models(task_dir))
        results.append(check_compile(task_dir))
        results.append(check_lint(task_dir))
        if spec.get("notes"):
            results.append(check_notes(task_dir, spec["notes"]))
        if spec.get("golden_set"):
            results.append(check_golden_set(task_dir, spec["golden_set"]))

    summary = render(results, task_key, args.branch or "(unknown)", task_name, task_dir, root)

    failed = any(r.status == "fail" for r in results)
    warned = any(r.status == "warn" for r in results)
    status = "fail" if failed else ("warn" if warned else "pass")

    Path(args.summary_out).write_text(summary, encoding="utf-8")
    Path(args.out).write_text(json.dumps({
        "status": status,
        "task_key": task_key,
        "task_name": task_name,
        "branch": args.branch,
        "author": args.author,
        "pr_number": args.pr,
        "repo": os.environ.get("GITHUB_REPOSITORY", ""),
        "run_url": (f"{os.environ.get('GITHUB_SERVER_URL', '')}/"
                    f"{os.environ.get('GITHUB_REPOSITORY', '')}/actions/runs/"
                    f"{os.environ.get('GITHUB_RUN_ID', '')}"),
        "checks": [asdict(r) for r in results],
        "summary_md": summary,
    }, indent=2), encoding="utf-8")

    step_summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if step_summary:
        with open(step_summary, "a", encoding="utf-8") as fh:
            fh.write(summary + "\n")

    print(summary)
    return 1 if (failed or (warned and args.fail_on_warn)) else 0


if __name__ == "__main__":
    sys.exit(main())

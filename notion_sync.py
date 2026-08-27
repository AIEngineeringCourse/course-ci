#!/usr/bin/env python3
"""Sync CI results from student PRs into the Notion Assignments board.

WHY THIS RUNS CENTRALLY, NOT IN STUDENT REPOS
---------------------------------------------
A Notion integration token in a student repo is a token a student can exfiltrate
by editing the workflow (masking in logs does not survive base64). So the Notion
credential never leaves a repository you control: this script reads *public
metadata* about student PRs through the GitHub API and does the Notion write from
your own repo. Students can break their own checks; they cannot touch the board.

USAGE
    export GITHUB_TOKEN=...            # read-only, repo scope, your account/org
    export NOTION_TOKEN=...            # Notion internal integration secret
    export NOTION_STUDENTS_DB=...      # 32-hex database id
    export NOTION_ASSIGNMENTS_DB=...   # 32-hex database id

    python notion_sync.py --verify                 # check config, write nothing
    python notion_sync.py --org AIEngineeringCourse --dry-run
    python notion_sync.py --org AIEngineeringCourse

Run it from a workflow_dispatch or a cron in your own repo, or locally before a
review session.
"""
from __future__ import annotations

import argparse
import os
import re
import sys
import json
import urllib.error
import urllib.parse
import urllib.request

NOTION_API = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"
GITHUB_API = "https://api.github.com"

# Property names on your boards. Change here if you rename anything.
PROP_STUDENT_NAME = "Student"
PROP_STUDENT_GITHUB = "Github"
PROP_ASSIGNMENT_STUDENT = "Student"
# Either the title/rich_text naming the task ("[Ph. 2] Task 5 — ...") or a
# relation to task pages; the lookup below handles both.
PROP_ASSIGNMENT_TASK = "Assignment"
PROP_CI = "CI"          # optional select/rich_text property; skipped if absent
PROP_ASSIGNMENT_STATUS = "Status"
# Students paste the pull-request link here. Matching on it is exact, and unlike
# the Github field on the Students board it is visible to the person who can fix
# it. Declared as a `files` property; url and rich_text are read too, so
# changing the column type later does not break matching.
PROP_ASSIGNMENT_PR_URL = "PR url"
# Status set on rows the pipeline creates, so a practical submission lands in
# the mentor's queue rather than looking untouched.
STATUS_ON_CREATE = "Review needed"
# Status set when the checks pass: the submission is ready for a human. Only
# moved from the states below, so a mentor's own decision is never overwritten -
# in particular 'Done' is never dragged back into the queue. A failing verdict
# leaves Status alone, because the student is still working.
STATUS_ON_PASS = "Review needed"
STATUS_MOVABLE_FROM = {"", "Not started", "In progress"}

STATUS_LABEL = {"pass": "✅ CI passed", "warn": "⚠️ CI warnings",
                "fail": "❌ CI failed", "pending": "⏳ CI running",
                "none": "— no CI result"}

# What the student should do about each verdict. The board comment carries the
# verdict only - the per-check detail lives on the GitHub PR - so every line
# that is not "all clear" points back at the PR.
STATUS_ADVICE = {
    "pass": "Mentor will check your PR soon.",
    "warn": "Fix the issues. If you are sure there are no issues, write your mentor.",
    "fail": "Fix the issues and push again. If you think a check is wrong, write your mentor.",
    "pending": "Checks are still running. This will update on the next sync.",
    "none": "No checks have run for the latest commit yet.",
}

# 'pass' is the only verdict with nothing to act on, so it gets a plain link.
LINK_LABEL = {"pass": "PR"}
DEFAULT_LINK_LABEL = "Open the PR for more details"


def comment_body(status: str, pr: dict) -> str:
    link = LINK_LABEL.get(status, DEFAULT_LINK_LABEL)
    return (f"[ci-sync] {STATUS_LABEL[status]}\n"
            f"Branch: {pr['branch']}\n"
            f"{STATUS_ADVICE[status]}\n"
            f"{link}: {pr['url']}")


# --------------------------------------------------------------------------- #
def http(url: str, token: str, *, extra_headers: dict | None = None,
         payload: dict | None = None, method: str | None = None) -> dict:
    headers = {"Authorization": f"Bearer {token}",
               "Content-Type": "application/json"}
    headers.update(extra_headers or {})
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, headers=headers,
                                 method=method or ("POST" if data else "GET"))
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read().decode()
            return json.loads(body) if body else {}
    except urllib.error.HTTPError as e:
        detail = e.read().decode()[:400]
        raise RuntimeError(f"{e.code} {url}\n{detail}") from None


def notion(path: str, token: str, payload: dict | None = None,
           method: str | None = None) -> dict:
    return http(f"{NOTION_API}{path}", token,
                extra_headers={"Notion-Version": NOTION_VERSION},
                payload=payload, method=method)


DB_ID_RE = re.compile(r"^[0-9a-f]{32}$")


def normalise_db_id(value: str) -> str:
    """Accept a raw 32-hex id, a dashed uuid, or a full database URL.

    Notion answers 404 for a wrong id and for a database the integration cannot
    see, so a malformed id is indistinguishable from a permissions problem in
    the response. Catching the shape here keeps that ambiguity out of the way -
    and stops a mistyped value being sent upstream and echoed back in an error.
    """
    v = value.strip()
    if v.startswith("http"):                       # full URL: take the last path
        v = urllib.parse.urlparse(v).path.rsplit("/", 1)[-1]
    v = v.split("?", 1)[0]                         # drop ?v=<view-id>
    v = v.replace("-", "").lower()
    return v


def check_db_id(name: str, value: str) -> str | None:
    """Return an error string if the value cannot be a database id."""
    v = normalise_db_id(value)
    if DB_ID_RE.match(v):
        return None
    if value.strip().startswith(("ntn_", "secret_")):
        return (f"{name} looks like an integration TOKEN, not a database id. "
                "The id is the 32-hex string in the database's own URL.")
    return (f"{name} is not a 32-hex database id (got {len(v)} chars after "
            "normalising). Copy the part of the database URL before '?v='.")


def list_visible_databases(token: str) -> list[tuple[str, str]]:
    """Every database this integration can actually see, as (title, id)."""
    out, cursor = [], None
    while True:
        payload = {"filter": {"property": "object", "value": "database"},
                   "page_size": 100}
        if cursor:
            payload["start_cursor"] = cursor
        res = notion("/search", token, payload=payload)
        for r in res.get("results", []):
            title = "".join(t.get("plain_text", "")
                            for t in r.get("title", [])) or "(untitled)"
            out.append((title, r.get("id", "").replace("-", "")))
        if not res.get("has_more"):
            return out
        cursor = res.get("next_cursor")


def github(path: str, token: str) -> dict | list:
    return http(f"{GITHUB_API}{path}", token,
                extra_headers={"Accept": "application/vnd.github+json",
                               "X-GitHub-Api-Version": "2022-11-28"})


# --------------------------------------------------------------------------- #
def plain(prop: dict) -> str:
    """Flatten a Notion property to text."""
    t = prop.get("type")
    if t == "title":
        return "".join(x["plain_text"] for x in prop["title"])
    if t == "rich_text":
        return "".join(x["plain_text"] for x in prop["rich_text"])
    if t == "select":
        return (prop.get("select") or {}).get("name", "")
    if t == "url":
        return prop.get("url") or ""
    if t == "relation":
        return ",".join(r["id"] for r in prop.get("relation", []))
    if t == "files":
        return ",".join(file_urls(prop))
    return ""


def file_urls(prop: dict) -> list[str]:
    """URLs out of a Notion `files` property.

    An entry is either `external` (what a pasted link becomes) or `file` (an
    upload, whose url is signed and expires). Notion also stores the pasted
    text as `name`, which is the fallback when neither shape is present.
    """
    out = []
    for f in prop.get("files") or []:
        if f.get("type") == "external":
            url = (f.get("external") or {}).get("url") or ""
        else:
            url = (f.get("file") or {}).get("url") or ""
        url = url or f.get("name") or ""
        if url:
            out.append(url)
    return out


# owner/repo/pull/N - anything after the number (/files, /commits) is noise.
PR_PATH_RE = re.compile(r"^/([^/]+)/([^/]+)/pull/(\d+)")


def normalize_pr_url(value: str) -> str | None:
    """Canonical https://github.com/owner/repo/pull/N, or None if not a PR link.

    Students paste `/files` views, `#issuecomment-` anchors, trailing slashes,
    mixed case and occasionally a branch or commit url. Everything that is not
    unambiguously a pull request returns None so the caller can say so, rather
    than silently failing to match.
    """
    v = (value or "").strip()
    if not v:
        return None
    if not v.startswith(("http://", "https://")):
        v = "https://" + v
    try:
        parsed = urllib.parse.urlparse(v)
    except ValueError:
        return None
    host = (parsed.netloc or "").lower()
    if host.startswith("www."):
        host = host[len("www."):]
    if host != "github.com":
        return None
    m = PR_PATH_RE.match(parsed.path)
    if not m:
        return None
    owner, repo, number = m.group(1).lower(), m.group(2).lower(), int(m.group(3))
    return f"https://github.com/{owner}/{repo}/pull/{number}"


def row_pr_urls(row: dict) -> list[str]:
    """Every canonical PR url on a row, whatever type the column is."""
    prop = row.get("properties", {}).get(PROP_ASSIGNMENT_PR_URL) or {}
    ptype = prop.get("type")
    if ptype == "files":
        raw = file_urls(prop)
    elif ptype == "url":
        raw = [prop.get("url") or ""]
    elif ptype == "rich_text":
        raw = [plain(prop)]
    else:
        raw = []
    seen, out = set(), []
    for candidate in raw:
        norm = normalize_pr_url(candidate)
        if norm and norm not in seen:
            seen.add(norm)
            out.append(norm)
    return out


def move_status_to_review(page_id: str, props: dict, token: str,
                          who: str) -> int:
    """Queue a passing submission for review. Returns 1 if it moved.

    Idempotent, and deliberately conservative: a row already queued is left
    alone, and any state outside STATUS_MOVABLE_FROM belongs to the mentor -
    'Done' in particular is never dragged back into the queue.
    """
    current = (props.get(PROP_ASSIGNMENT_STATUS, {}).get("status")
               or {}).get("name", "")
    if current == STATUS_ON_PASS:
        return 0                                    # already queued
    if current not in STATUS_MOVABLE_FROM:
        print(f"    {who}: Status left at '{current}' — not the pipeline's to change")
        return 0
    try:
        notion(f"/pages/{page_id}", token,
               {"properties": {PROP_ASSIGNMENT_STATUS: {
                   "status": {"name": STATUS_ON_PASS}}}},
               method="PATCH")
    except RuntimeError as exc:
        print(f"    ! {who}: could not set Status: "
              f"{str(exc.args[0]).splitlines()[0]} — the Notion integration "
              "needs the 'Update content' capability")
        return 0
    print(f"    {who}: Status '{current or 'empty'}' → '{STATUS_ON_PASS}'")
    return 1


def find_row_by_task(assignments: list[dict], student: dict, key: str,
                     token: str, title_cache: dict[str, str]) -> dict | None:
    """Fallback match: the student's row whose task resolves to `key`.

    Kept so a student who has not pasted a link still gets feedback, provided
    the task is identifiable from the row.
    """
    for row in assignments:
        props = row.get("properties", {})
        if student["page_id"] not in plain(props.get(PROP_ASSIGNMENT_STUDENT, {})):
            continue
        task_prop = props.get(PROP_ASSIGNMENT_TASK, {})
        if task_prop.get("type") == "relation":
            # Relation: follow each linked page and read its title.
            for tid in filter(None, plain(task_prop).split(",")):
                if tid not in title_cache:
                    title_cache[tid] = page_title(tid, token)
                if task_key_from_title(title_cache[tid]) == key:
                    return row
        elif task_key_from_title(plain(task_prop)) == key:
            # title / rich_text / select: the property names the task itself.
            return row
    return None


def audit_pr_urls(assignments: list[dict]) -> list[str]:
    """Problems in the PR url column: unusable values and duplicates.

    A value that is not a pull-request link matches nothing and reports nothing,
    so without this it looks identical to a student who pasted correctly.
    """
    problems: list[str] = []
    for row in assignments:
        prop = row.get("properties", {}).get(PROP_ASSIGNMENT_PR_URL) or {}
        title = plain(row.get("properties", {}).get(PROP_ASSIGNMENT_TASK, {})) \
            or "(untitled row)"
        ptype = prop.get("type")
        if ptype == "files":
            raw = file_urls(prop)
        elif ptype == "url":
            raw = [prop.get("url") or ""]
        elif ptype == "rich_text":
            raw = [plain(prop)]
        else:
            continue
        for value in [r for r in raw if r.strip()]:
            if not normalize_pr_url(value):
                problems.append(f"{title}: not a pull-request link — {value[:80]}")

    for url, rows in build_pr_index(assignments).items():
        if len(rows) > 1:
            titles = [plain(r.get("properties", {}).get(PROP_ASSIGNMENT_TASK, {}))
                      or "(untitled)" for r in rows]
            problems.append(f"{url} is on {len(rows)} rows ({', '.join(titles)}) "
                            "— it will be skipped, not guessed")
    return problems


def build_pr_index(assignments: list[dict]) -> dict[str, list[dict]]:
    """canonical PR url -> the row(s) claiming it. More than one is a conflict."""
    index: dict[str, list[dict]] = {}
    for row in assignments:
        for url in row_pr_urls(row):
            index.setdefault(url, []).append(row)
    return index


def query_all(db_id: str, token: str, payload: dict | None = None) -> list[dict]:
    out, cursor = [], None
    while True:
        body = dict(payload or {})
        if cursor:
            body["start_cursor"] = cursor
        res = notion(f"/databases/{db_id}/query", token, body)
        out.extend(res.get("results", []))
        if not res.get("has_more"):
            return out
        cursor = res["next_cursor"]


def student_login(field_value: str) -> str:
    """The bare login from a Github field: a handle or a full profile URL."""
    return field_value.strip().rstrip("/").split("/")[-1].lower()


def load_students(token: str, db: str) -> dict[str, dict]:
    """Map lowercased GitHub login -> {page_id, name}."""
    out = {}
    for row in query_all(db, token):
        props = row.get("properties", {})
        gh = plain(props.get(PROP_STUDENT_GITHUB, {})).strip()
        name = plain(props.get(PROP_STUDENT_NAME, {})).strip()
        if gh:
            login = student_login(gh)
            out[login] = {"page_id": row["id"], "name": name or login}
    return out


def audit_students(token: str, db: str, gh_token: str = "") -> list[str]:
    """Problems that would silently misroute a student's PRs.

    load_students keys by login, so a handle on two rows collapses to whichever
    row is read last - the mapping looks fine and quietly points at the wrong
    person. That is invisible until someone asks why their board is empty.
    """
    problems: list[str] = []
    by_login: dict[str, list[str]] = {}

    for row in query_all(db, token):
        props = row.get("properties", {})
        raw = plain(props.get(PROP_STUDENT_GITHUB, {})).strip()
        name = plain(props.get(PROP_STUDENT_NAME, {})).strip() or "(unnamed row)"
        if not raw:
            problems.append(f"{name}: empty '{PROP_STUDENT_GITHUB}' — their PRs can never match")
            continue
        by_login.setdefault(student_login(raw), []).append(name)

    for login, names in sorted(by_login.items()):
        if len(names) > 1:
            problems.append(
                f"'{login}' is on {len(names)} rows ({', '.join(names)}) — "
                "only one of them will ever receive comments")
        if gh_token:
            try:
                github(f"/users/{login}", gh_token)
            except RuntimeError as exc:
                # Only a definite 404 is a finding; a rate limit is not.
                if str(exc.args[0]).startswith("404"):
                    problems.append(f"'{login}' ({names[0]}) is not a GitHub user")
    return problems


def canonical_assignment_title(key: str) -> str:
    """phase2/task5 -> '[Ph. 2] Task 5', a title task_key_from_title can read back."""
    phase, task = key.split("/")
    return f"[Ph. {phase[len('phase'):]}] Task {task[len('task'):]}"


def create_assignment(db_id: str, token: str, student: dict, title: str,
                      status: str | None = None) -> dict:
    """Title, student, and an opening Status. Nothing else is the pipeline's."""
    props = {
        PROP_ASSIGNMENT_TASK: {
            "title": [{"type": "text", "text": {"content": title}}]},
        PROP_ASSIGNMENT_STUDENT: {"relation": [{"id": student["page_id"]}]},
    }
    if status:
        props[PROP_ASSIGNMENT_STATUS] = {"status": {"name": status}}
    return notion("/pages", token,
                  {"parent": {"database_id": db_id}, "properties": props})


def status_option_available(schema: dict, wanted: str) -> bool:
    """Is `wanted` already defined on the Status property?

    The API can invent a `select` option on write but never a `status` one, so
    an undefined value fails the whole create. Check instead of discovering it
    one failed row at a time.
    """
    pdef = schema.get(PROP_ASSIGNMENT_STATUS) or {}
    if pdef.get("type") != "status":
        return False
    return wanted in {o["name"] for o in (pdef.get("status") or {}).get("options", [])}


def page_title(page_id: str, token: str) -> str:
    page = notion(f"/pages/{page_id}", token)
    for prop in page.get("properties", {}).values():
        if prop.get("type") == "title":
            return plain(prop)
    return ""


TASK_RE = re.compile(r"\[?ph\.?\s*(\d)\]?.*?task\s*(\d+)", re.I)


def task_key_from_title(title: str) -> str | None:
    m = TASK_RE.search(title)
    return f"phase{m.group(1)}/task{m.group(2)}" if m else None


def branch_task_key(branch: str) -> str | None:
    m = re.match(r"(phase[1-5])/(task\d+)", branch.strip().lower())
    return f"{m.group(1)}/{m.group(2)}" if m else None


# --------------------------------------------------------------------------- #
def classify_conclusions(conclusions: list) -> str:
    """A list of check/run conclusions -> one of our status keys."""
    if not conclusions:
        return "none"
    if None in conclusions:                 # still running
        return "pending"
    if all(c == "success" for c in conclusions):
        return "pass"
    if any(c == "failure" for c in conclusions):
        return "fail"
    return "warn"                           # neutral, skipped, cancelled, ...


def pr_status(gh_token: str, full: str, sha: str) -> tuple[str, str | None]:
    """CI verdict for a commit, via whichever endpoint the token can read.

    Two sources, because they need different fine-grained permissions and a
    given token often has only one:
      - commits/{sha}/check-runs  needs **Checks: Read**
      - actions/runs?head_sha=    needs **Actions: Read**
    Returns (status, error) where error is set only when neither could be read,
    so the caller can say so instead of silently reporting "no CI result".
    """
    errors = []
    try:
        runs = github(f"/repos/{full}/commits/{sha}/check-runs", gh_token)
        return classify_conclusions(
            [c.get("conclusion") for c in runs.get("check_runs", [])]), None
    except RuntimeError as exc:
        errors.append(f"check-runs: {str(exc.args[0]).splitlines()[0]}")

    try:
        res = github(f"/repos/{full}/actions/runs?head_sha={sha}&per_page=100",
                     gh_token)
        # Keep only the newest run per workflow, so a superseded failure does
        # not outvote the rerun that fixed it.
        newest: dict[str, dict] = {}
        for run in res.get("workflow_runs", []):
            name = run.get("name") or str(run.get("workflow_id"))
            if name not in newest or run.get("created_at", "") > newest[name].get("created_at", ""):
                newest[name] = run
        return classify_conclusions(
            [r.get("conclusion") for r in newest.values()]), None
    except RuntimeError as exc:
        errors.append(f"actions/runs: {str(exc.args[0]).splitlines()[0]}")

    return "none", "; ".join(errors)


COMMENT_VERDICT = {"### ✅": "pass", "### ⚠️": "warn", "### ❌": "fail"}


def refine_status_from_comment(gh_token: str, full: str, number: int,
                               status: str) -> str:
    """Recover check.py's three-level verdict from the action's PR comment.

    check.py exits 0 for "passed with warnings", so the workflow conclusion is
    `success` and the board would report a clean pass - a student who introduces
    lint errors would see no board change at all. Only the action's own comment
    distinguishes the two. Bot comments only: a student can comment on their own
    PR and could otherwise fake a verdict.
    """
    if status != "pass":
        return status
    try:
        comments = github(f"/repos/{full}/issues/{number}/comments?per_page=100",
                          gh_token)
    except RuntimeError:
        return status                       # fall back to the conclusion
    for comment in reversed(comments if isinstance(comments, list) else []):
        if (comment.get("user") or {}).get("login") != "github-actions[bot]":
            continue
        body = (comment.get("body") or "").lstrip()
        for prefix, verdict in COMMENT_VERDICT.items():
            if body.startswith(prefix):
                return verdict
    return status


def collect_prs(gh_token: str, org: str | None, repos: list[str]) -> list[dict]:
    if not repos:
        # Report the count. A token that cannot see the org returns an empty
        # listing, which then reports "Found 0 open PR(s)" - indistinguishable
        # from a quiet day, and the run still goes green.
        try:
            listing = github(f"/orgs/{org}/repos?per_page=100&type=all", gh_token) \
                if org else []
        except RuntimeError as exc:
            print(f"  ! cannot list repos of org '{org}': "
                  f"{str(exc.args[0]).splitlines()[0]}")
            listing = []
        repos = [r["full_name"] for r in listing]
        print(f"  {len(repos)} repo(s) visible to this token")
        if not repos:
            print("  ! nothing to scan. Check COURSE_READ_TOKEN has this org as "
                  "its resource owner and 'All repositories' access.")

    prs = []
    for full in repos:
        try:
            open_prs = github(f"/repos/{full}/pulls?state=open&per_page=50", gh_token)
        except RuntimeError as e:
            print(f"  ! skipping {full}: {e.args[0].splitlines()[0]}")
            continue
        for pr in open_prs:
            sha = pr["head"]["sha"]
            status, status_error = pr_status(gh_token, full, sha)
            status = refine_status_from_comment(gh_token, full, pr["number"],
                                                status)
            if status_error:
                # Never silent: without a readable verdict every board comment
                # says "no CI result" regardless of what CI did, and the run
                # still goes green.
                print(f"  ! {full}#{pr['number']}: cannot read the CI verdict "
                      f"— status will show as 'no CI result'. COURSE_READ_TOKEN "
                      f"needs **Checks: Read** or **Actions: Read** on this "
                      f"repo. ({status_error})")
            prs.append({
                "repo": full,
                "author": (pr.get("user") or {}).get("login", "").lower(),
                "branch": pr["head"]["ref"],
                "number": pr["number"],
                "url": pr["html_url"],
                "title": pr["title"],
                "status": status,
            })
    return prs


def previous_ci_label(page_id: str, token: str) -> str | None:
    """The verdict from the most recent [ci-sync] comment on this page.

    Notion comments cannot be edited or deleted through the API, so the only
    way to keep a board readable is to not post in the first place when nothing
    has changed. Comparing the verdict (rather than the whole body) means a
    re-push that keeps the same result stays quiet, while pass -> fail speaks up.
    """
    try:
        res = notion(f"/comments?block_id={page_id}", token)
    except RuntimeError:
        # Never let a read failure suppress a comment: fall through and post.
        return None
    found = None
    for c in res.get("results", []):          # ascending: last match is newest
        text = "".join(x["plain_text"] for x in c.get("rich_text", []))
        if text.startswith("[ci-sync]"):
            found = text.splitlines()[0][len("[ci-sync]"):].strip()
    return found


def post_comment(page_id: str, token: str, body: str) -> None:
    notion("/comments", token, {
        "parent": {"page_id": page_id},
        "rich_text": [{"type": "text", "text": {"content": body[:1900]}}],
    })


# --------------------------------------------------------------------------- #
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--org", default=os.environ.get("GITHUB_ORG"))
    ap.add_argument("--repo", action="append", default=[],
                    help="owner/name; repeatable. Use instead of --org for personal accounts.")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--verify", action="store_true",
                    help="Validate tokens and print board schema, then exit.")
    ap.add_argument("--create-missing", action="store_true",
                    help="Create an Assignment row when a matched student has a "
                         "valid phaseN/taskM branch and no row for it. Requires "
                         "the integration to have Insert content. Pair with "
                         "--dry-run first to see what it would create.")
    args = ap.parse_args()

    nt = os.environ.get("NOTION_TOKEN", "")
    gt = os.environ.get("GITHUB_TOKEN", "")
    students_db = os.environ.get("NOTION_STUDENTS_DB", "")
    assign_db = os.environ.get("NOTION_ASSIGNMENTS_DB", "")

    missing = [n for n, v in (("NOTION_TOKEN", nt), ("GITHUB_TOKEN", gt),
                              ("NOTION_STUDENTS_DB", students_db),
                              ("NOTION_ASSIGNMENTS_DB", assign_db)) if not v]
    if missing:
        print("Missing environment variables: " + ", ".join(missing))
        return 2

    # Accept a raw id, a dashed uuid, or a pasted database URL.
    students_db = normalise_db_id(students_db)
    assign_db = normalise_db_id(assign_db)

    if args.verify:
        # Shape-check the ids before spending a request on them.
        shape_errors = [e for e in (check_db_id("NOTION_STUDENTS_DB", students_db),
                                    check_db_id("NOTION_ASSIGNMENTS_DB", assign_db))
                        if e]
        for e in shape_errors:
            print(f"  ! {e}")

        try:
            visible = list_visible_databases(nt)
        except RuntimeError as exc:
            print(f"  ! /search failed: {exc.args[0].splitlines()[0]}")
            visible = []

        print(f"Databases shared with this integration: {len(visible)}")
        for title, db_id in visible:
            print(f"  {db_id}  {title}")
        if not visible:
            print("  (none — open each database as a full page, then "
                  "••• → Connections → connect your integration. Nothing else "
                  "below can work until this list is non-empty.)")
        for label, value in (("NOTION_STUDENTS_DB", students_db),
                             ("NOTION_ASSIGNMENTS_DB", assign_db)):
            norm = normalise_db_id(value)
            if visible and norm not in {i for _, i in visible}:
                print(f"  ! {label} is not in the list above — wrong id, or "
                      "that database is not shared with this integration.")
        if shape_errors:
            return 2

        def show_props(props: dict) -> None:
            # Types matter: the task lookup expects a relation it can follow to
            # a page, so a same-named title property would silently match zero.
            for pname, pdef in sorted(props.items()):
                ptype = pdef.get("type", "?")
                line = f"    {ptype:<14} {pname}"
                # The API can invent a new `select` option but never a `status`
                # one, so the writable values have to be listed to be usable.
                if ptype in ("select", "status", "multi_select"):
                    opts = [o["name"] for o in (pdef.get(ptype) or {}).get("options", [])]
                    if opts:
                        line += "   options: " + ", ".join(opts)
                print(line)

        print("Notion — Students database")
        db = notion(f"/databases/{students_db}", nt)
        show_props(db.get("properties", {}))
        for const, label in ((PROP_STUDENT_NAME, "PROP_STUDENT_NAME"),
                             (PROP_STUDENT_GITHUB, "PROP_STUDENT_GITHUB")):
            if const not in db.get("properties", {}):
                print(f"  ! {label} = '{const}' does not exist on this database")
        print("Notion — Assignments database")
        db2 = notion(f"/databases/{assign_db}", nt)
        props = db2.get("properties", {})
        show_props(props)
        for const, label in ((PROP_ASSIGNMENT_STUDENT, "PROP_ASSIGNMENT_STUDENT"),
                             (PROP_ASSIGNMENT_TASK, "PROP_ASSIGNMENT_TASK")):
            if const not in props:
                print(f"  ! {label} = '{const}' does not exist — the sync will "
                      "match nothing until this constant is corrected")
        print("  properties:", ", ".join(props))
        print(f"  '{PROP_CI}' property present:", PROP_CI in props,
              "(optional — comments are used regardless)")
        pr_prop = props.get(PROP_ASSIGNMENT_PR_URL)
        if not pr_prop:
            print(f"  ! no '{PROP_ASSIGNMENT_PR_URL}' property — PR-link "
                  "matching is off; falling back to branch/task matching only")
        else:
            rows = query_all(assign_db, nt)
            index = build_pr_index(rows)
            print(f"'{PROP_ASSIGNMENT_PR_URL}' is a {pr_prop.get('type')} "
                  f"property: {len(index)} usable PR url(s) across "
                  f"{len(rows)} row(s)")
            for problem in audit_pr_urls(rows):
                print(f"  ! {problem}")

        issues = audit_students(nt, students_db, gt)
        if issues:
            print("Students board problems:")
            for i in issues:
                print(f"  ! {i}")
        else:
            print("Students board: no duplicate or unknown handles.")

        students = load_students(nt, students_db)
        print(f"Resolved {len(students)} student(s) with a GitHub handle:")
        for login, s in students.items():
            print(f"  {login} -> {s['name']}")
        me = github("/user", gt)
        print("GitHub authenticated as:", me.get("login"))
        return 0

    students = load_students(nt, students_db)
    if not students:
        print("No students with a GitHub handle found — populate the Github column.")
        return 1

    print(f"Collecting open PRs ({'org ' + args.org if args.org else str(len(args.repo)) + ' repo(s)'})")
    prs = collect_prs(gt, args.org, args.repo)
    print(f"Found {len(prs)} open PR(s)")

    assignments = query_all(assign_db, nt)
    title_cache: dict[str, str] = {}

    if args.create_missing:
        # Creating a row means writing the task name into the title property.
        # On a board where PROP_ASSIGNMENT_TASK is a relation, that shape is
        # wrong, so refuse up front rather than fail once per PR.
        schema = notion(f"/databases/{assign_db}", nt).get("properties", {})
        ptype = schema.get(PROP_ASSIGNMENT_TASK, {}).get("type")
        if ptype != "title":
            print(f"--create-missing needs '{PROP_ASSIGNMENT_TASK}' to be the "
                  f"title property, but it is '{ptype or 'absent'}'. "
                  "Create the rows by hand, or point PROP_ASSIGNMENT_TASK at "
                  "the title property.")
            return 2

        create_status = STATUS_ON_CREATE if status_option_available(
            schema, STATUS_ON_CREATE) else None
        if STATUS_ON_CREATE and not create_status:
            print(f"  ! '{PROP_ASSIGNMENT_STATUS}' has no '{STATUS_ON_CREATE}' "
                  "option — creating rows without a status. Add the option in "
                  "Notion; the API cannot create one.")

    matched = 0
    created = 0
    status_moved = 0
    pr_index = build_pr_index(assignments)
    print(f"{len(pr_index)} assignment row(s) carry a usable PR url")

    for pr in prs:
        where = f"{pr['repo']}#{pr['number']}"
        student = students.get(pr["author"])       # may be None; not required
        key = branch_task_key(pr["branch"])
        target, matched_by = None, ""

        # ---- primary: the student pasted this PR's link on their page --------
        claimed = pr_index.get(normalize_pr_url(pr["url"]) or "", [])
        if len(claimed) > 1:
            print(f"  ! {where}: this PR url is on {len(claimed)} rows — "
                  "skipping rather than guessing which one is meant")
            continue
        if claimed:
            target, matched_by = claimed[0], "PR url"
            # A pasted link can be someone else's. Only cross-check when both
            # sides are known, so an unmaintained Students board blocks nothing.
            relation = plain(target.get("properties", {})
                             .get(PROP_ASSIGNMENT_STUDENT, {}))
            if student and relation and student["page_id"] not in relation:
                print(f"  ! {where}: opened by '{pr['author']}' but that row "
                      "belongs to another student — skipping")
                continue

        # ---- fallback: student + phaseN/taskM from the branch ---------------
        if not target and not student:
            print(f"  ? {where}: no row carries this PR url, and "
                  f"'{pr['author']}' is not on the Students board — skipping")
            continue
        if not target and not key:
            print(f"  ? {where}: no row carries this PR url, and branch "
                  f"'{pr['branch']}' does not match phaseN/taskM- — skipping")
            continue

        if not target:
            matched_by = "branch"
        if not target:
            target = find_row_by_task(assignments, student, key, nt, title_cache)

        if not target:
            if not args.create_missing:
                print(f"  ? {where}: no row carries this PR url and no "
                      f"{key} row for {student['name']} — paste the PR link "
                      "on the assignment page")
                continue

            title = canonical_assignment_title(key)
            # Guard against a runaway: if the title we write does not parse back
            # to the same key, the next run would not match it and would create
            # another row - twice a weekday, forever.
            if task_key_from_title(title) != key:
                print(f"  ! refusing to create '{title}': it does not read back "
                      f"as {key}")
                continue
            shown = f"'{title}'" + (f" [{create_status}]" if create_status else "")
            if args.dry_run:
                print(f"  + would create Assignment {shown} for {student['name']}")
                created += 1
                continue
            try:
                target = create_assignment(assign_db, nt, student, title,
                                           create_status)
            except RuntimeError as exc:
                print(f"  ! could not create '{title}': "
                      f"{str(exc.args[0]).splitlines()[0]}")
                continue
            created += 1
            print(f"  + created Assignment {shown} for {student['name']}")

        who = student["name"] if student else pr["author"]
        what = key or pr["branch"]
        label = STATUS_LABEL[pr["status"]]
        body = comment_body(pr["status"], pr)

        props = target.get("properties", {})

        # Status first, and independent of whether a comment is posted. It is
        # idempotent - already-queued rows and mentor-owned states are skipped -
        # so running it every time makes a failed write self-healing. Behind the
        # comment dedup it would never be retried: the verdict is unchanged on
        # the next run, so the whole block would be skipped forever.
        if pr["status"] == "pass" and not args.dry_run:
            status_moved += move_status_to_review(target["id"], props, nt, who)

        # Post only when the verdict changed. Checked before the dry-run bail
        # so a dry run reports exactly what a live run would do.
        if previous_ci_label(target["id"], nt) == label:
            print(f"  = {who} / {what}: unchanged ({label}) — not re-posting")
            continue

        matched += 1
        print(f"  → {who} / {what}: {label}  (matched by {matched_by})")

        if args.dry_run:
            continue

        post_comment(target["id"], nt, body)

        if PROP_CI in props:
            ptype = props[PROP_CI]["type"]
            if ptype == "select":
                payload = {"properties": {PROP_CI: {"select": {"name": label}}}}
            elif ptype == "rich_text":
                payload = {"properties": {PROP_CI: {
                    "rich_text": [{"type": "text", "text": {"content": label}}]}}}
            else:
                payload = None
            if payload:
                notion(f"/pages/{target['id']}", nt, payload, method="PATCH")

    tense = "would be" if args.dry_run else ""
    print(f"\n{matched} assignment(s) {tense} updated.")
    if args.create_missing:
        print(f"{created} assignment row(s) {tense} created.")
    print(f"{status_moved} Status value(s) {tense} moved to '{STATUS_ON_PASS}' "
          "(only from an un-reviewed state; 'Done' is never touched).")
    return 0


if __name__ == "__main__":
    sys.exit(main())

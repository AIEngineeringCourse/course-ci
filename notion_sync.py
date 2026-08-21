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
    return ""


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


def create_assignment(db_id: str, token: str, student: dict, title: str) -> dict:
    """A minimal row: title and student only. Status stays the mentor's call."""
    return notion("/pages", token, {
        "parent": {"database_id": db_id},
        "properties": {
            PROP_ASSIGNMENT_TASK: {
                "title": [{"type": "text", "text": {"content": title}}]},
            PROP_ASSIGNMENT_STUDENT: {"relation": [{"id": student["page_id"]}]},
        },
    })


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
def collect_prs(gh_token: str, org: str | None, repos: list[str]) -> list[dict]:
    if not repos:
        listing = github(f"/orgs/{org}/repos?per_page=100&type=all", gh_token) \
            if org else []
        repos = [r["full_name"] for r in listing]

    prs = []
    for full in repos:
        try:
            open_prs = github(f"/repos/{full}/pulls?state=open&per_page=50", gh_token)
        except RuntimeError as e:
            print(f"  ! skipping {full}: {e.args[0].splitlines()[0]}")
            continue
        for pr in open_prs:
            sha = pr["head"]["sha"]
            status = "none"
            try:
                runs = github(f"/repos/{full}/commits/{sha}/check-runs", gh_token)
                concl = [c.get("conclusion") for c in runs.get("check_runs", [])]
                if not concl:
                    status = "none"
                elif None in concl:
                    status = "pending"
                elif all(c == "success" for c in concl):
                    status = "pass"
                elif any(c == "failure" for c in concl):
                    status = "fail"
                else:
                    status = "warn"
            except RuntimeError:
                pass
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
                print(f"    {pdef.get('type', '?'):<14} {pname}")

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

    matched = 0
    created = 0
    for pr in prs:
        student = students.get(pr["author"])
        if not student:
            print(f"  ? {pr['repo']}#{pr['number']}: GitHub user '{pr['author']}' "
                  "not in the Students board — skipping")
            continue
        key = branch_task_key(pr["branch"])
        if not key:
            print(f"  ? {pr['repo']}#{pr['number']}: branch '{pr['branch']}' does not "
                  "match phaseN/taskM- — skipping")
            continue

        target = None
        for row in assignments:
            props = row.get("properties", {})
            rel = props.get(PROP_ASSIGNMENT_STUDENT, {})
            if student["page_id"] not in plain(rel):
                continue
            task_prop = props.get(PROP_ASSIGNMENT_TASK, {})
            if task_prop.get("type") == "relation":
                # Relation: follow each linked page and read its title.
                for tid in filter(None, plain(task_prop).split(",")):
                    if tid not in title_cache:
                        title_cache[tid] = page_title(tid, nt)
                    if task_key_from_title(title_cache[tid]) == key:
                        target = row
                        break
            elif task_key_from_title(plain(task_prop)) == key:
                # title / rich_text / select: the property names the task itself.
                target = row
            if target:
                break

        if not target:
            if not args.create_missing:
                print(f"  ? no Assignment row for {student['name']} + {key}")
                continue

            title = canonical_assignment_title(key)
            # Guard against a runaway: if the title we write does not parse back
            # to the same key, the next run would not match it and would create
            # another row - twice a weekday, forever.
            if task_key_from_title(title) != key:
                print(f"  ! refusing to create '{title}': it does not read back "
                      f"as {key}")
                continue
            if args.dry_run:
                print(f"  + would create Assignment '{title}' for {student['name']}")
                created += 1
                continue
            try:
                target = create_assignment(assign_db, nt, student, title)
            except RuntimeError as exc:
                print(f"  ! could not create '{title}': "
                      f"{str(exc.args[0]).splitlines()[0]}")
                continue
            created += 1
            print(f"  + created Assignment '{title}' for {student['name']}")

        label = STATUS_LABEL[pr["status"]]
        body = comment_body(pr["status"], pr)

        # Post only when the verdict changed. Checked before the dry-run bail
        # so a dry run reports exactly what a live run would do.
        if previous_ci_label(target["id"], nt) == label:
            print(f"  = {student['name']} / {key}: unchanged ({label}) — not re-posting")
            continue

        matched += 1
        print(f"  → {student['name']} / {key}: {label}")

        if args.dry_run:
            continue

        post_comment(target["id"], nt, body)

        props = target.get("properties", {})
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
    print("Status was not modified — that stays a mentor decision.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

# Course CI — tier-one checks + Notion sync

Static submission checks for the Innowise AI Engineering course. Two moving parts:

1. **`course-ci`** (this repo) — a composite action students reference in five
   lines, plus the Notion sync that runs here and nowhere else.
2. **`student-template`** — the repo each student's submissions repo is created
   from.

Nothing here runs student code and nothing here needs an LLM API key.

---

## What gets checked

| Check | Fails the PR? | Notes |
|---|---|---|
| Secret scan | **yes** | Committed `.env` or a live-looking key. Placeholder lines (`...`, `your-key`, `<`) are ignored |
| `.gitignore` lists `.env` | warn | |
| Task detection | yes if unresolvable | Reads `phaseN/taskM` from the branch name |
| Required files | **yes** | From `tasks.yml`, per task |
| Model references | **yes** for retired models | `text-embedding-004`, `embedding-001`, `claude-3-*`. Warns on off-course choices and dated aliases |
| Python syntax | **yes** | `py_compile`, so nothing executes |
| Lint | warn | `ruff`, errors only (`F`, `E9`) — undefined names, unused imports |
| Written notes | **yes** where configured | Exists, meets a word floor, documents the expected number of bugs |
| Golden set | **yes** where configured | Case count, all four categories, `should_refuse` consistency, duplicate questions, required fields |

The PR comment ends with a line telling students these are mechanical checks and
that a mentor still reviews reasoning. Keep that line — without it, green ticks
start to feel like a grade.

---

## Setup, once

### 1. Create the org and two repos

```bash
gh repo create AIEngineeringCourse/course-ci --public   --source=./course-ci --push
gh repo create AIEngineeringCourse/student-template --private --source=./student-template --push
gh api -X PATCH repos/AIEngineeringCourse/student-template -F is_template=true
```

`course-ci` is **public** on purpose: a private action shared across repos runs
into access rules that differ between orgs and personal accounts, and public
sidesteps all of it. Students can read the checks, which is fine — transparent
criteria are good. **Never put seeded-bug answers or a mentor key in this repo.**

Then tag it so student workflows can pin a version:

```bash
git -C course-ci tag v1 && git -C course-ci push --tags
```

### 2. Point the template at your org

`student-template/.github/workflows/ci.yml` already points at
`AIEngineeringCourse/course-ci@v1`. If you fork this setup for another org,
change that reference **before** creating student repos.

### 3. Create student repos

```bash
./create_student_repos.sh AIEngineeringCourse students.txt
```

Idempotent — safe to re-run when someone joins.

### 4. Notion sync (optional, do it second)

Create an internal integration at <https://www.notion.so/my-integrations>, then
**share the Students and Assignments databases with it** (Notion integrations see
nothing until explicitly shared — this is the step everyone forgets).

Add to `course-ci` repo secrets: `NOTION_TOKEN`, `NOTION_STUDENTS_DB`,
`NOTION_ASSIGNMENTS_DB`, and `COURSE_READ_TOKEN` (a fine-grained PAT with
**read-only** Contents/Pull-requests on the course repos).

Verify before enabling the schedule:

```bash
export NOTION_TOKEN=... GITHUB_TOKEN=... \
       NOTION_STUDENTS_DB=... NOTION_ASSIGNMENTS_DB=...
python notion_sync.py --verify                          # prints schema + students
python notion_sync.py --org AIEngineeringCourse --dry-run # prints intended writes
```

`--verify` exists because the database IDs are the easiest thing to get wrong.
Use the 32-hex ID from the database's own URL. If `--verify` lists your students
and your property names, the rest will work.

---

## Two deliberate security decisions

**The Notion token never enters a student repo.** A student can edit their own
workflow, and log masking does not survive base64. So the sync runs here, reading
PR state through the GitHub API and writing to Notion from a repo students cannot
modify. The cost is that the board updates on a schedule rather than instantly;
`workflow_dispatch` gives you an on-demand run before a review session.

**`CODEOWNERS` protects `.github/`.** Otherwise a student can weaken the checks
that gate their own PR. Combined with branch protection, the checks are a real
gate rather than advice.

The sync **never writes Status** — it posts a comment and, if you add an optional
`CI` select property to Assignments, sets that. Status stays your decision.

---

## Maintenance

Almost everything lives in **`tasks.yml`** — one entry per task, and tasks without
an entry still get every automatic check. Phase 1 entries are deliberately loose
(`*.py`, `README.md`); tighten them from the Notion submission trees when you get
a chance. Phase 3 has no entries yet.

When a model is retired, add it to `DEAD_MODELS` in `check.py`. Patterns are
regexes with boundaries — `embedding-001` deliberately does not match inside
`gemini-embedding-001`.

## Costs

Linux runners only (1× multiplier; Windows is 2×, macOS 10×). A run is roughly
40–60 seconds. Ten students across six core tasks is on the order of 120–200
minutes a month against 2,000 free on the GitHub Free plan. If you ever outgrow
that, a self-hosted runner has no per-minute fee.

## Known limits

- CI verifies mechanics, never understanding. Reasoning quality, whether the
  right bugs were found, and whether a student gamed a check all still need you.
- The checks are public, so they can be read and targeted. That is an acceptable
  trade for transparency, and it is why the substantive review still exists.
- `notion_sync.py` has **not** been run against a live Notion workspace — no
  network access to `api.notion.com` from where it was written. Run `--verify`
  then `--dry-run` first; expect to adjust property names if yours differ from
  the constants at the top of the file.

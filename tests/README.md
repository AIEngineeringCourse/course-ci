# Fixture tests for the tier-one checks

```bash
./tests/run_tests.sh
```

Runs `check.py` against three committed fixtures and asserts the exit code of
each, plus the reason each verdict was reached. No network, no API keys, and
nothing here runs student code.

| Fixture | Branch | Expected |
|---|---|---|
| `fixtures/clean` | `phase2/task2-rag-qa-bot` | exit **0** |
| `fixtures/broken` | `phase2/task2-rag-qa-bot` | exit **1** |
| `fixtures/task1` | `phase1/task1-first-api-calls` | exit **0** |

## Why the exit code alone is not enough

A fixture can fail for the wrong reason and still "pass" a naive exit-code
assertion. So the harness also asserts that the broken fixture's summary names
each planted fault, and that the clean one does not.

## Planted faults in `fixtures/broken`

1. A committed `.env`. `check.py` flags this by **filename** and never reads the
   contents (`check_secrets`), so the values inside are inert placeholders with
   obvious `FAKE` markers. Do not replace them with anything key-shaped: this
   repo is public.
2. `models/text-embedding-004` — retired, must fail.
3. A missing colon in `chain.py`, so `py_compile` fails.
4. A golden set with 4 cases, a duplicate question, and an `out_of_scope` case
   that omits `should_refuse`.

## `fixtures/task1` is a regression test

Phase 1 task pages ask only for a script — Ph1 Task 1's sole deliverable is
`summarizer.py`. `tasks.yml` used to require `README.md` for Phase 1 too, which
would have failed the first submission of the cohort. This fixture contains
**only** `summarizer.py` and must pass. Run it against the previous `tasks.yml`
and it exits 1.

## Boundary-aware dead-model patterns

`DEAD_MODELS` entries are regexes with boundaries on purpose: `embedding-001`
must **not** match inside `gemini-embedding-001`, the current course standard.
The harness asserts the regexes directly, in both directions, rather than
inferring from the summary wording:

| Pattern | Text | Must match |
|---|---|---|
| `embedding-001` | `models/gemini-embedding-001` | no |
| `embedding-001` | `models/embedding-001` | yes |
| `text-embedding-004` | `models/text-embedding-004` | yes |
| `text-embedding-004` | `models/gemini-embedding-001` | no |

If you change that logic, keep this table passing.

## Note on `ruff`

Lint is a warn-level check, so exit codes here are unaffected when `ruff` is not
installed locally — you will just see a warning line. CI installs it from
`requirements.txt`.

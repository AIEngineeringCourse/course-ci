#!/usr/bin/env bash
# Run check.py against each committed fixture and assert its exit code.
#
#   ./tests/run_tests.sh
#
# Exit code contract (check.py): 1 if any check failed, else 0. Warnings alone
# do not fail a run unless --fail-on-warn is passed, so a missing ruff on a
# developer machine degrades to a warning and does not skew these assertions.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(dirname "$HERE")"
CHECK="$ROOT/check.py"
MANIFEST="$ROOT/tasks.yml"
PYTHON="${PYTHON:-python3}"

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

pass=0
fail=0

# name | fixture dir | branch | expected exit code
run_case() {
  local name="$1" fixture="$2" branch="$3" expected="$4"
  local out="$WORK/$name.md"

  "$PYTHON" "$CHECK" \
    --branch "$branch" \
    --root "$HERE/fixtures/$fixture" \
    --manifest "$MANIFEST" \
    --out "$WORK/$name.json" \
    --summary-out "$out" >"$WORK/$name.log" 2>&1
  local actual=$?

  if [ "$actual" -eq "$expected" ]; then
    echo "PASS  $name (exit $actual, expected $expected)"
    pass=$((pass + 1))
  else
    echo "FAIL  $name (exit $actual, expected $expected)"
    echo "----- output -----"
    sed 's/^/      /' "$WORK/$name.log"
    echo "------------------"
    fail=$((fail + 1))
  fi
}

# Asserts a fixture's summary mentions something, so a fixture that fails for
# the wrong reason cannot pass by accident.
assert_contains() {
  local name="$1" needle="$2"
  if grep -qF -- "$needle" "$WORK/$name.md" 2>/dev/null; then
    echo "PASS  $name mentions: $needle"
    pass=$((pass + 1))
  else
    echo "FAIL  $name does not mention: $needle"
    fail=$((fail + 1))
  fi
}

assert_absent() {
  local name="$1" needle="$2"
  if grep -qF -- "$needle" "$WORK/$name.md" 2>/dev/null; then
    echo "FAIL  $name unexpectedly mentions: $needle"
    fail=$((fail + 1))
  else
    echo "PASS  $name does not mention: $needle"
    pass=$((pass + 1))
  fi
}

echo "== exit codes =="
run_case clean  clean  phase2/task4-rag-qa-bot        0
run_case broken broken phase2/task4-rag-qa-bot        1
run_case task1  task1  phase1/task1-first-api-calls   0
run_case task5  task5  phase2/task5-rag-debug         0

echo
echo "== broken fixture fails for the right reasons =="
assert_contains broken "committed .env file"
assert_contains broken "text-embedding-004"
assert_contains broken "SyntaxError"
assert_contains broken "duplicate question"
assert_contains broken "out_of_scope case must set should_refuse true"
assert_contains broken "4 cases; task requires at least 15"

# The dead-model patterns are boundary-aware on purpose: `embedding-001` must
# not match inside `gemini-embedding-001`, the current course standard. Assert
# the regexes directly rather than inferring from summary prose, which could
# pass for the wrong reason if the wording changes.
echo
echo "== dead-model boundary awareness =="
if "$PYTHON" - "$ROOT" <<'PYEOF'
import importlib.util, sys, pathlib
root = pathlib.Path(sys.argv[1])
spec = importlib.util.spec_from_file_location("check", root / "check.py")
m = importlib.util.module_from_spec(spec)
# Register before exec: @dataclass resolves cls.__module__ through sys.modules,
# which is absent for a module loaded this way (raises on Python 3.9).
sys.modules["check"] = m
spec.loader.exec_module(m)

cases = [
    ("embedding-001",      "models/gemini-embedding-001", False),  # must NOT match
    ("embedding-001",      "models/embedding-001",        True),   # must match
    ("text-embedding-004", "models/text-embedding-004",   True),
    ("text-embedding-004", "models/gemini-embedding-001", False),
]
ok = True
# Every model id the Phase 3/4 task pages use must survive every pattern, now
# and after any future retirement is added. gemini-3.6-flash in particular: if
# it were ever matched, every phase4/task4 submission would fail on correct work.
for mid in m.CURRENT_MODELS:
    hits = ([k for k, (rx, _) in m.DEAD_RE.items() if rx.search(mid)]
            + [k for k, (rx, _) in m.OFF_RE.items() if rx.search(mid)])
    print(f"      {mid:<30} {'clean' if not hits else 'FLAGGED by ' + str(hits)}")
    if hits:
        ok = False
for key, text, expected in cases:
    rx, _why = m.DEAD_RE[key]
    got = bool(rx.search(text))
    verdict = "ok" if got == expected else "WRONG"
    if got != expected:
        ok = False
    print(f"      {key:<20} vs {text:<30} match={got!s:<5} {verdict}")
sys.exit(0 if ok else 1)
PYEOF
then
  echo "PASS  dead-model regexes are boundary-aware"
  pass=$((pass + 1))
else
  echo "FAIL  dead-model boundary behaviour changed"
  fail=$((fail + 1))
fi
assert_contains clean "All model references are current."

# Regression test for the Phase 1 manifest fix: task1 has no README.md, and
# must still pass.
echo
echo "== phase1 needs no README =="
assert_contains task1 "All 1 expected files present."

# Pins the phase2 renumbering: task4 is the RAG bot and requires all seven
# files, including ingestion.py (not ingest.py) and a per-task .gitignore.
# A silent revert of the manifest would drop this count and fail here.
echo
echo "== phase2 renumbering =="
assert_contains clean "All 7 expected files present."
assert_contains clean "Ph2 Task 4"

# The bugs.md rule moved from the old task3 to task5 in the renumbering, and
# had never been exercised. This pins both the rule and the directory
# convention task5 now relies on - without a matching directory, detection
# fails before any file is read.
echo
echo "== phase2/task5 debug task =="
assert_contains task5 "phase2/task5 → phase2/task5_rag_debug"
assert_contains task5 "bugs.md:"           # the notes check ran and passed

echo
echo "== phase 3 and 4: one passing and one failing fixture per task =="
run_case p3t1_pass  p3t1_pass  phase3/task1-raw-react-agent       0
run_case p3t1_fail  p3t1_fail  phase3/task1-raw-react-agent       1
run_case p3t2_pass  p3t2_pass  phase3/task2-tool-best-practices   0
run_case p3t2_fail  p3t2_fail  phase3/task2-tool-best-practices   1
run_case p3t3_pass  p3t3_pass  phase3/task3-research-agent        0
run_case p3t3_fail  p3t3_fail  phase3/task3-research-agent        1
run_case p3t4_pass  p3t4_pass  phase3/task4-code-review-agent     0
run_case p3t4_fail  p3t4_fail  phase3/task4-code-review-agent     1
run_case p3t5_pass  p3t5_pass  phase3/task5-multi-agent-pipeline  0
run_case p3t5_fail  p3t5_fail  phase3/task5-multi-agent-pipeline  1
run_case p3t6_pass  p3t6_pass  phase3/task6-hitl-pipeline         0
run_case p3t6_fail  p3t6_fail  phase3/task6-hitl-pipeline         1
run_case p4t1_pass  p4t1_pass  phase4/task1-sse-streaming         0
run_case p4t1_fail  p4t1_fail  phase4/task1-sse-streaming         1
run_case p4t2_pass  p4t2_pass  phase4/task2-trace-inefficiency    0
run_case p4t2_fail  p4t2_fail  phase4/task2-trace-inefficiency    1
run_case p4t3_pass  p4t3_pass  phase4/task3-rag-failure-handling  0
run_case p4t3_fail  p4t3_fail  phase4/task3-rag-failure-handling  1
run_case p4t4_pass  p4t4_pass  phase4/task4-eval-pipeline         0
run_case p4t4_fail  p4t4_fail  phase4/task4-eval-pipeline         1
run_case p4t6_pass  p4t6_pass  phase4/task6-production-service    0
run_case p4t6_fail  p4t6_fail  phase4/task6-production-service    1

echo
echo "== the behaviours those fixtures exist to pin =="
# ast parsing, not regex: a commented-out import and a string literal naming
# the package must both pass, while a real import must fail with file:line.
assert_contains p3t1_fail "imports 'langchain.agents'"
assert_contains p3t1_pass "No import of: langchain, langgraph."
# A prose-only task has no .py at all; that is a pass, not a skip or an error.
assert_contains p4t2_pass "No Python files in this task"
# Nested required paths resolve (sample_code/buggy_code.py).
assert_contains p3t4_pass "All 4 expected files present."
# Phase 4 nests expected_source as an object; the Phase 2 validator would
# have rejected it, so this dataset gets its own shape check.
assert_contains p4t4_pass "shape valid"
# Required-files names what is absent, in the row students actually read.
assert_contains p3t2_fail "Missing: README.md"
# The golden-set rejection names the shape it received, so a student who
# grouped cases by category can see which keys they actually produced.
run_case p2t4_badshape p2t4_badshape phase2/task4-rag-qa-bot 1
assert_contains p2t4_badshape "found object with keys: happy_path, edge_cases, out_of_scope, ambiguous"
echo
echo "-------- $pass passed, $fail failed --------"
[ "$fail" -eq 0 ] || exit 1

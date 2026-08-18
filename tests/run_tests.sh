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
run_case clean  clean  phase2/task2-rag-qa-bot        0
run_case broken broken phase2/task2-rag-qa-bot        1
run_case task1  task1  phase1/task1-first-api-calls   0

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

echo
echo "-------- $pass passed, $fail failed --------"
[ "$fail" -eq 0 ] || exit 1

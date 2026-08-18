"""Score the chain against the golden set."""
import json
from pathlib import Path


def load_cases(path: str = "golden_set.json") -> list[dict]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def score(answer: str, case: dict) -> bool:
    if case.get("should_refuse"):
        return "do not know" in answer.lower()
    return all(fact.lower() in answer.lower() for fact in case["expected_facts"])


def main() -> None:
    cases = load_cases()
    print(f"{len(cases)} cases loaded")


if __name__ == "__main__":
    main()

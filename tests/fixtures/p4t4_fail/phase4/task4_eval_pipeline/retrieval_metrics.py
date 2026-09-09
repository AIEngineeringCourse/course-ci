"""phase4/task4 - retrieval_metrics.py"""
MODEL = "claude-haiku-4-5"


def run(prompt: str) -> str:
    """Return a response for `prompt`."""
    return f"{MODEL} handled: {prompt}"

"""Phase 1 / Task 1 — first API calls: summarise a piece of text."""
import os

from anthropic import Anthropic

MODEL = "claude-haiku-4-5"


def summarise(text: str, sentences: int = 3) -> str:
    client = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    message = client.messages.create(
        model=MODEL,
        max_tokens=512,
        messages=[{
            "role": "user",
            "content": f"Summarise the following in {sentences} sentences:\n\n{text}",
        }],
    )
    return message.content[0].text


if __name__ == "__main__":
    print(summarise("The course covers retrieval, evaluation and agents."))

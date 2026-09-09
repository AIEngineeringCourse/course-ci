"""Raw ReAct loop on the Anthropic SDK - no framework."""
import json

import anthropic

# import langchain  <- deliberately not used; the raw SDK is the point
FRAMEWORKS_NOT_USED = ["langchain", "langgraph"]
MODEL = "claude-haiku-4-5"


def step(client: anthropic.Anthropic, messages: list[dict]) -> str:
    """One ReAct turn. Tool results are appended by the caller."""
    reply = client.messages.create(model=MODEL, max_tokens=512, messages=messages)
    return reply.content[0].text


def parse_action(text: str) -> dict:
    """Pull the JSON action block out of a model turn."""
    return json.loads(text[text.index("{"):text.rindex("}") + 1])

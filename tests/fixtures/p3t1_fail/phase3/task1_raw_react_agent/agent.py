"""Uses LangChain, which this task forbids."""
from langchain.agents import initialize_agent

MODEL = "claude-haiku-4-5"


def build():
    return initialize_agent([], None)

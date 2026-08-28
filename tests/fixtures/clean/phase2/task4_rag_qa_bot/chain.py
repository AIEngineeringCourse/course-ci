"""Retrieval chain: retrieve, then answer strictly from context."""
from langchain_anthropic import ChatAnthropic
from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.runnables import RunnablePassthrough

CHAT_MODEL = "claude-haiku-4-5"

PROMPT = ChatPromptTemplate.from_template(
    "Answer using only the context below. If the context does not contain the "
    "answer, say you do not know.\n\nContext:\n{context}\n\nQuestion: {question}"
)


def format_docs(docs) -> str:
    return "\n\n".join(d.page_content for d in docs)


def build_chain(retriever):
    llm = ChatAnthropic(model=CHAT_MODEL, temperature=0)
    return (
        {"context": retriever | format_docs, "question": RunnablePassthrough()}
        | PROMPT
        | llm
        | StrOutputParser()
    )

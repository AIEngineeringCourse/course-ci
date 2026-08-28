"""Phase 2 / Task 4 — the chat entry point for the RAG bot."""
from chain import build_chain
from ingestion import build_index


def main() -> None:
    retriever = build_index().as_retriever(search_kwargs={"k": 4})
    chain = build_chain(retriever)
    while True:
        question = input("> ").strip()
        if not question:
            break
        print(chain.invoke(question))


if __name__ == "__main__":
    main()

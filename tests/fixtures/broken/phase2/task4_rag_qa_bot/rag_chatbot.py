from chain import build_chain
from ingestion import build_index


def main():
    retriever = build_index([]).as_retriever()
    return build_chain(retriever, None)

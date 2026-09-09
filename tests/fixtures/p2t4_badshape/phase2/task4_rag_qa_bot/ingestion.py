"""Ingest course documents into a persistent vector store."""
from pathlib import Path

from langchain_chroma import Chroma
from langchain_google_genai import GoogleGenerativeAIEmbeddings
from langchain_text_splitters import RecursiveCharacterTextSplitter

EMBED_MODEL = "models/gemini-embedding-001"
PERSIST_DIR = "chroma_db"


def build_index(docs_dir: str = "docs") -> Chroma:
    splitter = RecursiveCharacterTextSplitter(chunk_size=1000, chunk_overlap=150)
    texts = []
    for path in Path(docs_dir).glob("*.md"):
        texts.extend(splitter.split_text(path.read_text(encoding="utf-8")))
    embeddings = GoogleGenerativeAIEmbeddings(model=EMBED_MODEL)
    return Chroma.from_texts(texts, embeddings, persist_directory=PERSIST_DIR)


if __name__ == "__main__":
    build_index()

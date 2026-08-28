"""Phase 2 / Task 5 — the repaired RAG pipeline."""
from langchain_chroma import Chroma
from langchain_google_genai import GoogleGenerativeAIEmbeddings
from langchain_text_splitters import RecursiveCharacterTextSplitter

EMBED_MODEL = "models/gemini-embedding-001"


def build_retriever(texts: list[str]):
    splitter = RecursiveCharacterTextSplitter(chunk_size=1000, chunk_overlap=150)
    chunks = [c for t in texts for c in splitter.split_text(t)]
    store = Chroma.from_texts(chunks, GoogleGenerativeAIEmbeddings(model=EMBED_MODEL))
    return store.as_retriever(search_kwargs={"k": 4})

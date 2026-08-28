from langchain_chroma import Chroma
from langchain_google_genai import GoogleGenerativeAIEmbeddings

EMBED_MODEL = "models/text-embedding-004"


def build_index(texts):
    embeddings = GoogleGenerativeAIEmbeddings(model=EMBED_MODEL)
    return Chroma.from_texts(texts, embeddings, persist_directory="chroma_db")

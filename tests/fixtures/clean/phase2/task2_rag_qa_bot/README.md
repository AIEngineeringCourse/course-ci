# Phase 2 / Task 2 — RAG document Q&A bot

Answers questions over the course documents, refusing when the corpus does
not contain the answer.

## Design notes

Embeddings use `models/gemini-embedding-001` (the course standard) and the
chat model is `claude-haiku-4-5`. Chunking is 1000 characters with 150 of
overlap, so a definition split across a boundary is still retrievable.

The prompt constrains the model to the retrieved context, which is what makes
the `out_of_scope` golden-set cases refusable rather than hallucinated.

## Run

    python ingest.py
    python eval.py

# Bugs found in the broken RAG pipeline

Five defects, in the order they would bite at runtime.

## Bug 1 — retired embedding model

The pipeline requested a Google embedding model that was withdrawn, so every
call failed at request time rather than at import. Nothing in the traceback
pointed at the model name, which is what made it slow to find. Replaced with
the course standard, `models/gemini-embedding-001`.

## Bug 2 — chunk overlap set to zero

Splitting with no overlap cut definitions across boundaries, so a question
whose answer straddled two chunks retrieved neither half cleanly. The symptom
was a confident answer built from a fragment. Restored an overlap of 150
characters, which is enough to keep a sentence intact at typical chunk sizes.

## Bug 3 — retriever returned a single document

`k` defaulted to one, so any question needing corroboration from two passages
produced a partial answer with no indication that context was missing. Raised
to four, which covers the multi-passage questions in the golden set without
flooding the prompt.

## Bug 4 — the embedding function differed between ingest and query

Documents were indexed with one embedding configuration and queried with
another. Similarity scores were computed across mismatched vector spaces, so
retrieval looked plausible but ranked almost arbitrarily. Both paths now build
the embedding object from the same constant.

## Bug 5 — no guard for an empty retrieval

When retrieval returned nothing, the empty context string was still formatted
into the prompt, and the model answered from its own knowledge. That is the
failure mode the out_of_scope golden-set cases exist to catch. The chain now
refuses when no document clears the relevance threshold.

## How they were found

Bugs 1 and 5 surfaced from the golden set directly. Bugs 2 to 4 needed a
side-by-side comparison of retrieved chunks against the source documents,
because each produced a plausible answer rather than an obvious error.

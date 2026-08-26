# Alpha Detective — Financial Document Intelligence

A local-first RAG (Retrieval-Augmented Generation) web app: upload financial documents (PDF / DOCX / TXT / MD / CSV), ask questions in plain English, and get **grounded, citation-backed answers** — the exact source document, page, and snippet behind every claim. When the documents don't contain the answer, it says so instead of guessing.

**Stack:** Next.js 16 (JavaScript, App Router, Tailwind) · FastAPI + LlamaIndex · ChromaDB (dense, cosine) + BM25 (sparse) fused with reciprocal-rank fusion · local flashrank cross-encoder reranker · Google **Gemini free tier** for embeddings + answers, with a keyless "retrieval-only" fallback mode. $0 to run.

## Quickstart (≈3 minutes)

Requirements: Python 3.12+ (3.13 works), Node 20+, macOS/Linux.

```bash
make setup                 # venv + pip install, npm install, sample docs, creates backend/.env
# optional but recommended — free key, no card: https://aistudio.google.com/apikey
#   put it in backend/.env →  GOOGLE_API_KEY=your_key_here
make dev                   # boots backend :8000 + frontend :3000
```

Open **http://localhost:3000** → Documents → upload the three files in `backend/sample_data/` → Ask:

- *"What was Meridian's revenue in Q2 FY2026?"* → $48.2 million, cited to the PDF (a table value)
- *"What was Northwind's diluted EPS this quarter?"* → $1.12
- *"What is Apple's revenue?"* → refuses: not in your documents

Without a key the app runs in **retrieval-only mode** (amber pill): hybrid retrieval + reranking still work and answers are matched excerpts. With a key you get generative answers (blue "GENERATIVE" badge) — same citations, same refusal discipline.

**After adding a key**, verify the live path: `make test` (the auto-skipped `test_grounding_live.py` suite now runs, ≤4 LLM calls), then re-ask the three questions above.

## How it works

```
upload → parse (pypdf + pdfplumber tables / python-docx / txt / csv)
       → chunk (sentence splitter 512/64, page + provenance metadata)
       → embed (Gemini, sha256-cached) → ChromaDB (cosine)  ┐
       → BM25 over the same chunks                          ├─ hybrid
ask    → dense top-8 + BM25 top-8 → RRF fusion (top-12)     ┘
       → flashrank cross-encoder rerank → top 6
       → no-answer guardrail (BEFORE any LLM call)
       → ONE grounded Gemini call → citation-validated answer [n]
```

Free-tier discipline throughout: one LLM call per question, no LLM query expansion (`num_queries=1`), embedding cache, exponential backoff on 429 with a friendly banner in the UI.

## API (backend, :8000)

| Endpoint | What |
|---|---|
| `GET /api/health` | status, provider, models, rerank, doc/chunk counts |
| `POST /api/documents` | multipart upload (≤20 files, ≤25 MB each) → per-file indexed/duplicate/failed |
| `GET /api/documents` | list + totals |
| `DELETE /api/documents/{id}` | remove everywhere (Chroma + docstore + manifest) |
| `POST /api/query` | `{question, doc_ids?, top_k?}` → answer, mode, citations `{n, doc, page, snippet, score}`, timings |

Errors always arrive as `{error:{code,message,retry_after_s?}}`.

## Testing

```bash
make test        # 101 pytest cases + frontend production build
```

Includes the **accuracy gate**: a 30-case eval set (`backend/tests/eval_set.json`) — direct, table-only, paraphrase, cross-document-trap, and unanswerable questions — requiring 100% top-3 retrieval with the expected figure in a returned snippet, run with reranking on **and** off, entirely keyless. The end-to-end browser proof and review trail from the build live in `docs/build/`.

## Configuration (`backend/.env`)

```
GOOGLE_API_KEY=      # empty = retrieval-only mode
PROVIDER=auto        # auto | gemini | none
GEMINI_LLM_MODEL=auto    # resolves live: gemini-flash-latest → 2.5-flash → 2.0-flash
GEMINI_EMBED_MODEL=auto  # resolves live: gemini-embedding-001 → gemini-embedding-2-preview
RERANK=on            # local cross-encoder; auto-degrades to off if the model can't download
```

## Troubleshooting

- **Backend won't start after editing `.env`** — put each value alone on its line and every comment on its own line. `python-dotenv` only strips a trailing `# …` when a real value precedes it; for an empty value (`GOOGLE_API_KEY=   # note`) the comment *becomes* the value. Since r3 the backend detects that shape, ignores the bogus key with a warning, and boots retrieval-only instead of dying — but the key still won't be used. Copy a fresh template if in doubt: `cp backend/.env.example backend/.env`. A startup log line reading `provider=none` when you expected `gemini` means your key was rejected as malformed.
- **429 / "Free-tier rate limit hit"** — Gemini free tier is ~10 req/min with a daily cap; wait for the countdown. Check your exact quotas in AI Studio.
- **chromadb install fails on Python 3.13** — `brew install python@3.12`, delete `backend/.venv`, re-run `make setup` (it uses `python3`; adjust PATH so 3.12 wins).
- **Ports busy** — free 3000/8000 or export `PORT` for Next and change the uvicorn `--port`.
- **Reranker "off" in the Pipeline card** — flashrank's first-run model download failed (offline?); retrieval still works, rerun online.
- **No `node`?** — `brew install node`.

## Project notes

Rebuilt from the original Streamlit prototype (preserved in `legacy/`). The build was executed by an orchestrated agent team — architecture contracts, adversarial security/QA/design review rounds, and decisions are all in `docs/build/` (`CONTRACTS.md`, `DECISIONS.md`, `BUILD_LOG.md`, `reviews/`). The team definitions in `.claude/agents/` work with Claude Code for future iterations — see `CLAUDE.md`.

⚠️ One manual chore: the old `_env` file (moved to `_to_delete/`) contained a plaintext credential — **rotate it** wherever it came from, then delete `_to_delete/`.

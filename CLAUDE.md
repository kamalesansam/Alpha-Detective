# CLAUDE.md — Alpha Detective

Financial-document RAG app: FastAPI + LlamaIndex hybrid retrieval backend (`backend/`), Next.js 16 JavaScript frontend (`frontend/`). Free Gemini tier + keyless fallback. Full product spec: `CLAUDE_CODE_PROMPT.md`. Binding technical contract: `docs/build/CONTRACTS.md` (v1.2). Decision log: `docs/build/DECISIONS.md`.

## Commands

- `make setup` · `make dev` (both servers) · `make test` (pytest + frontend build) · `make samples`
- Backend only: `cd backend && .venv/bin/uvicorn app.main:app --port 8000`
- Accuracy gate: `cd backend && .venv/bin/python -m pytest tests/test_retrieval_accuracy.py -q`

## Architecture map

- `backend/app/`: `config.py` (pydantic-settings; six frozen path constants are a test seam — do not rename), `providers.py` (**the only file that talks to Gemini**; lazy factories, mockable), `ingest.py` (parse+table extraction, chunk, committed-flag cleanup), `stores.py` (Chroma cosine + docstore + manifest — must never disagree), `retrieval.py` (dense+BM25 → RRF `num_queries=1` → structural pre-LLM no-answer guardrail), `rerank.py` (flashrank, optional at runtime, injectable scorer), `synthesis.py` (ONE grounded LLM call, sources-are-data, citation post-validation, `[n]`→`⟦n⟧` neutralization), `api.py` (routing only, envelope errors).
- `frontend/`: App Router pages Overview `/`, `/documents`, `/ask`; components per `CONTRACTS.md` §4; `lib/api.js` is the only fetch layer; design tokens in `app/globals.css`.
- Tests: `backend/tests/` — 101 cases keyless incl. the 30-case eval gate (100% top-3, rerank on+off); `test_grounding_live.py` auto-skips without `GOOGLE_API_KEY`.

## Non-negotiable conventions

1. **JavaScript only** in `frontend/` — no TypeScript source files, ever.
2. **Design tokens are law** (`app/globals.css`, spec §8): flat fills, zero gradients, no emoji in UI, single sanctioned shadow, tabular numerals. The approved canvas is the visual truth.
3. **Free-tier frugality**: one LLM call per query, `num_queries=1`, embed cache, tenacity backoff. Never add an LLM call without counting its quota cost.
4. **Secrets**: `backend/.env` only; never logged, never client-side, never committed.
5. **Grounding**: no LLM answer without citations; unanswerable → the exact refusal sentence; the no-answer guardrail runs BEFORE the LLM.
6. **Tests stay green**: `make test` before and after any change; the eval gate is 100% or the change is wrong (or the gate needs an architect-ratified update).

## Agent-team workflow (keep using it)

Six specialists live in `.claude/agents/`: `architect` (owns CONTRACTS.md; contract changes go through them), `ai-engineer` (backend), `design-lead` (design canvas + visual QA; never writes code), `frontend-engineer`, `security-engineer` (review-only), `qa-engineer` (tests/eval; never fixes product code). Protocol: brief → build → adversarial review (fresh context, findings tagged BLOCKER/MAJOR/MINOR in `docs/build/reviews/`) → builders reply inline and fix → re-review; max 3 rounds, then escalate to the user. Log rounds in `docs/build/BUILD_LOG.md`; record decisions in `docs/build/DECISIONS.md`. Orchestrator (main session) writes no product code.

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

## Deploy (Render + Vercel, both free)

Two services: the FastAPI backend on **Render** (blueprint at repo root, `render.yaml`) and the Next.js frontend on **Vercel** (Root Directory `frontend`).

**Deploy Render first.** The frontend needs the backend's URL *at build time* (see the callout below), so there is nothing to point Vercel at until the API is live.

### 1 · Backend → Render

New → **Blueprint** → pick this repo. `render.yaml` sets plan, root dir, build/start commands, health check (`/api/health`) and every non-secret default. Render injects `PORT` — **do not set it yourself**; the app binds `0.0.0.0` precisely *because* `PORT` is present, and keeps `127.0.0.1` locally.

| Variable | Set to | If you get it wrong |
|---|---|---|
| `GOOGLE_API_KEY` | your key (`sync:false` — dashboard only, never in the repo) | Empty ⇒ the app boots retrieval-only. **Server-side only**: never logged, never sent to the browser, never a `NEXT_PUBLIC_*` var |
| `ACCESS_CODE` | any string, or leave unset | Unset ⇒ the gate is off and anyone can spend your quota. Set ⇒ every `/api` route except `/api/health` needs the `X-Access-Code` header |
| `CORS_ORIGINS` | the exact Vercel origin, e.g. `https://alpha-detective.vercel.app` | Wrong/left default ⇒ the deployed frontend's calls are blocked by the browser. `*` works but is discouraged |
| `TRUSTED_PROXY_HOPS` | **`1`** | **Wrong in both directions.** `0` ⇒ every visitor lands in Render's single proxy bucket, so one user's tenth question 429s everyone. Too high ⇒ the rate limiter reads a client-supplied header again, letting anyone pick their own bucket — or burn a specific victim's. Raise it in the *same* change that adds a CDN or WAF |
| `RERANK` | **`off`** on the 512 MB plan | `on` risks OOM (see below) |
| `PROVIDER` / `GEMINI_LLM_MODEL` / `GEMINI_EMBED_MODEL` | `auto` | `auto` degrades to retrieval-only if the key or model lookup fails; an explicit `gemini` fails startup loudly instead |
| `DAILY_LLM_BUDGET` | `200` | Lower = safer quota, more extractive answers |
| `RATE_LIMIT_PER_MIN` | `10` | See the demo warning below |
| `MAX_DOCUMENTS` | `50` | Shared ceiling across all visitors |
| `AUTO_SEED` | `on` | `off` ⇒ a redeployed demo comes back empty |

### 2 · Frontend → Vercel

Import the repo, set **Root Directory: `frontend`**, deploy.

| Variable | Set to | If you get it wrong |
|---|---|---|
| `BACKEND_ORIGIN` | the Render service URL, e.g. `https://alpha-detective-api.onrender.com` | Unset ⇒ the rewrite targets `127.0.0.1:8000` and every request fails in production |
| `NEXT_PUBLIC_ACCESS_CODE` | *(optional)* the same value as `ACCESS_CODE` | Skip it and visitors are prompted for the code instead. **`NEXT_PUBLIC_*` is compiled into the client bundle and is therefore public** — a convenience for a demo, never a place for a real secret |

> **`BACKEND_ORIGIN` is baked in at BUILD time.** Next compiles `rewrites()` into `routes-manifest.json` during the build, so editing the variable in the Vercel dashboard changes nothing on its own. **Change the Render URL ⇒ redeploy the frontend.** This is the single most common way to get a working backend and a dead frontend.

### Free-tier realities

- **Cold starts.** A free Render instance spins down when idle. The first request after a nap takes tens of seconds and the status pill reads *"Backend offline"* until it wakes. Expect it; don't debug it.
- **Ephemeral disk.** `backend/storage/` — Chroma, docstore, manifest, uploads, embed cache, budget counter — is **wiped on every redeploy**. That is exactly why `AUTO_SEED=on` exists: a redeployed demo returns with `backend/sample_data/` indexed instead of empty. This is not durable storage for anyone's documents.
- **`RERANK=off` is the documented posture on the 512 MB plan.** The cross-encoder plus ONNX runtime is the largest resident allocation in the process and the realistic OOM cause. Quality is safe: the 30-case accuracy gate is 100% with rerank **on and off**, and `/api/health` reports the effective state truthfully rather than the requested one.
- **Budget degradation, not failure.** Past `DAILY_LLM_BUDGET` (per UTC day), queries fall back to extractive answers with an amber note and `degraded_reason:"daily_budget"`. Retrieval, citations, uploads and search keep working, and it never returns a 429.
- **⚠ Rate limit vs. live demos.** `RATE_LIMIT_PER_MIN=10` throttles `POST /api/query` per visitor. **A live demo that asks more than ten questions in a minute will 429 its own audience.** Raise it in the Render dashboard before you present — it takes effect on restart, no redeploy or rebuild needed. (GET routes are exempt, so the health poll and document list never count against you.)

### Before you put this on a public URL

**There is no tenancy.** The corpus is single-tenant and shared: every visitor who passes the access code sees every document and **can delete any of them**. `MAX_DOCUMENTS` is a shared ceiling one person can fill. `AUTO_SEED` only re-seeds on a startup with an empty manifest, so a deleted demo stays deleted until you restart or redeploy.

The access code and the rate limit bound **spend and burst — nothing else**. They are not authentication, not authorization, not ownership, not audit, and not recovery. The code lives in the browser by design, so it is not secret from a determined visitor; its whole job is to stop a crawler from burning the free Gemini quota. Deploy accordingly: this is a disposable showcase, not a document store.

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

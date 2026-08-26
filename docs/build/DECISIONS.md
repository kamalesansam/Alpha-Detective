# Decisions (ADR log) — Alpha Detective

One line per decision. Date · decision · why. Details live in CONTRACTS.md (v1.1), review files, and BUILD_LOG.md.

- 2026-08-25 · Provider = Google Gemini free tier with keyless "none" fallback; no paid APIs anywhere · Sam's constraint; app must demo at $0.
- 2026-08-25 · Frontend = Next.js 16, JavaScript only, App Router + Tailwind; no UI kit · Sam's explicit stack choice.
- 2026-08-25 · Hybrid retrieval = Chroma dense (cosine) + BM25 → RRF (num_queries=1) → flashrank rerank (optional at runtime) · spec §6.5; num_queries=1 protects free-tier quota.
- 2026-08-25 · Structural pre-LLM no-answer guardrail (entity-presence, period-presence with quarter expansion, cross-doc exclusive-topic) — ratified r2 · literal BM25-floor sketch provably failed the unanswerable/scoping gates; frozen constants kept.
- 2026-08-25 · Citation snippets = question-relevant ≤300-char window (word-boundary, ellipsis), not chunk head — ratified r2 · required by the expect_substring-in-snippet accuracy gate.
- 2026-08-25 · Corruption fail-loud exit codes: 1 via `python -m app.main`, 3 under uvicorn CLI — ratified r2 · uvicorn owns its exit code; both documented.
- 2026-08-25 · Query `doc_ids` validation: non-string → 400 `bad_request`; malformed/unknown UUID → 404 `not_found` · uniform with DELETE semantics.
- 2026-08-25 · Health reports the reranker's *effective* state (`rerank:"off"` when flashrank init fails, config unchanged) · truthful ops signal.
- 2026-08-25 · Failed/duplicate uploads persist nothing — committed-flag cleanup rmtrees `uploads/{doc_id}/` on any non-committed exit · security M1 (disk-fill DoS + spec §1.3).
- 2026-08-25 · Anti-citation-forgery: bracketed `[n]` tokens inside source text are neutralized to `⟦n⟧` in the LLM context only (snippets untouched); deeper cited-claim↔source verification consciously deferred as accepted defense-in-depth · extractive default is immune; generative path keeps rule + range validation.
- 2026-08-25 · 405 responses use envelope code `not_found` (status 405) · contract's error-code table defines no `method_not_allowed`.
- 2026-08-25 · No file-download/static route exists at all; uploads are write-only storage · smaller attack surface.
- 2026-08-25 · Design: file-type chips stay neutral; red/green/amber reserved exclusively for status semantics · avoids "PDF=error" misreads (design round 2).
- 2026-08-25 · Canvas artifact URL is stable across design rounds (round-2-density republished in place) · Sam's review link must not rot.
- 2026-08-26 · `.env` values are de-commented before validation and `GOOGLE_API_KEY` is dropped when implausible (leading `#`, whitespace, `#`, non-ASCII/non-printable) with one value-free warning — ratified r3 · python-dotenv keeps the comment as the value when the value is empty, so the shipped `.env.example` produced a 76-char "key" that reached an HTTP auth header and killed startup with a misleading "check the API key and network" error.
- 2026-08-26 · `PROVIDER=auto` is best-effort: any provider-init or model-resolution failure logs its cause once and boots retrieval-only (`provider:"none"`); explicit `PROVIDER=gemini` keeps the fail-loud `SystemExit(1)` — ratified r3 · `auto` means "figure it out" and keyless mode is fully supported; an operator who names a provider explicitly wants to hear it fail.


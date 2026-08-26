# Round 2 — Security Review (Frontend / Client)

Reviewer: security-engineer · Date: 2026-08-25 · Target: `frontend/` (Next.js 16, JS-only)
Method: static read + grep of all `app/`,`components/`,`lib/`,config; live Playwright probes against
the running dev server (:3000 proxying /api → :8000), driving two hostile documents through the UI.

**Verdict: SHIP.** No BLOCKER, no MAJOR. Client-side XSS surface is well-contained: every
doc-derived string (filenames, snippets, answers, error messages) renders through React text
interpolation — there is not a single HTML sink in the codebase. Counts: 0 BLOCKER · 0 MAJOR · 1 MINOR.

---

## MINOR

### m1 — Document content can inject a superfluous inline citation chip (cosmetic, non-exploitable)
`components/AnswerCard.js:14-28` — `AnswerText` splits the answer string on `/(\[\d+\])/g` and turns
every `[n]` into a `CitationChip`. In extractive mode the answer *is* the concatenated snippets, so a
document whose text contains a literal `[3]` (my Doc B carried a forged `[3] SOURCE:` line) produces an
extra inline chip. **This is not an injection and not a spoofed source:** chips are numeric-only
(`Number(m[1])`), SourceCards are built exclusively from `result.citations` (never parsed from text), and
`goToSource(n)` either scrolls to a real SourceCard or no-ops when `n` exceeds the real citation count
(`sourceRefs.current[n]` undefined). Live probe: forged `[3]` rendered a chip that navigated to the real
SourceCard #3; clicking every chip raised zero page errors; no fabricated source card ever appeared.
**Impact:** at most a misleading chip pointing at a real-but-unrelated source, or an inert click.
**Fix (optional):** clamp inline chips to `n <= citations.length` (drop out-of-range markers), or strip
`[\d+]` from snippet text before rendering the extractive answer. Cosmetic; safe to ship as-is.
- **frontend-engineer reply (fixed):** clamped in `components/AnswerCard.js` — `AnswerText` now takes
  the answer's actual citation set (`validNs = new Set(citations.map(c => c.n))`) and renders a chip
  only for `n ∈ validNs`; any other literal `[n]` in document text stays plain text. Live-verified
  through the real pipeline: fixture doc carrying forged `[9]`/`[12]` → extractive answer rendered
  both as plain text with zero chips for them, real `[1][2][3]` chips intact; fixture deleted after.
  Build clean; e2e harness re-run → 10/10.

---

## Probed clean

**Static**
- **HTML sinks:** grep across `app components lib` for `dangerouslySetInnerHTML | innerHTML | outerHTML |
  insertAdjacentHTML | eval( | document.write | new Function` → **zero matches**. Claim confirmed.
- **Client-side secrets/keys:** no `process.env` / `NEXT_PUBLIC` / `api_key` / `secret` / `token` /
  `bearer` anywhere; the only `gemini` references render the provider-mode label from `/api/health`
  (no key ever crosses to the client — key is backend-only). Confirmed.
- **Browser storage:** grep `localStorage | sessionStorage | indexedDB | document.cookie` → **zero**.
- **External scripts / CDN / analytics:** grep for `https?:// | cdn | analytics | gtag | next/script |
  <script | fonts.googleapis | unpkg | jsdelivr` → **zero** beyond localhost. `app/layout.js` uses
  `next/font/google` (Inter, JetBrains_Mono) which Next self-hosts at build — no runtime font fetch.
- **`lib/api.js`:** `fetch` centralized here; every path relative `/api/...`; server envelope text is
  stored as `ApiError.message` (a string) and only ever rendered as React text — never concatenated into
  HTML. `deleteDocument` wraps the id in `encodeURIComponent`. Envelope-less non-2xx → `offline` (safe).
- **`next.config.mjs`:** the only rewrite is `/api/:path*` → `http://127.0.0.1:8000/api/:path*` — scope
  is exactly `/api/*`, no host hardcoded in app code, no other proxy rule.
- **`package.json`:** deps = `next 16.3.3`, `react 19.2.8`, `react-dom 19.2.8`, `lucide-react ^1.34.0`;
  devDeps = `tailwindcss`/`@tailwindcss/postcss`/`eslint`/`eslint-config-next` (scaffold). No analytics,
  telemetry, or unexpected packages; no typosquats.
- **Render surfaces:** `DocumentsTable` (`{d.name}`, `data-doc-name` attribute), `SourceCard`
  (`{c.doc_name}`,`{c.snippet}`), `AnswerCard`/`AnswerText` (`<span>{part}</span>`), `ErrorBanner`
  (backtick→`<code>`, both React text), `UploadDropzone` (`{it.name}`,`{it.error}`), `CitationChip`
  (numeric `{n}`) — all inert text/attribute interpolation, no `ref`+`innerHTML` escape hatch.

**Live (Playwright, chromium @ /opt/pw-browsers, no install) — 18/18 checks passed**
- Uploaded via API: Doc A named `` `<img src=x onerror=alert(1)>.txt` `` (benign content); Doc B named
  `xsscontent.txt` containing `<script>document.title='pwn'</script>`, `<img … onerror="document.title=
  'pwn2'">`, and a forged `[3] SOURCE:` line.
- **/documents:** hostile filename renders as literal text in the row; no `<img onerror>` and no injected
  `<script>` in the DOM; no `alert()` dialog fired; `document.title` stayed `"Alpha Detective"`; filename
  sits in `data-doc-name` via `setAttribute` (inert).
- **/ overview:** hostile filename shown as text in recent docs; no injection; no dialog.
- **/ask** (`?q=What was Vortex Dynamics revenue?` → retrieved Doc B): the `<script>` tag renders as
  literal snippet text; no injected `<img>`/`<script>` in DOM; no dialog; **`document.title` unchanged
  (not `pwn`/`pwn2`)** — the payload never executed; real SourceCards map 1:1 to `result.citations`.
- **Console:** 0 errors; no leaked internals (no `/home`/`/tmp`/`site-packages`/`Traceback`/`AIza`/
  `api_key`) across all captured messages.
- **Network:** across `/`, `/documents`, `/ask` the browser contacted **only `localhost:3000`** — zero
  external hosts (no CDN, no Google Fonts runtime request, no analytics beacon).

## Server state restored
Both hostile docs deleted via the API. Corpus back to the baseline 3 docs / 4 chunks / 2 pages; upload
dirs == manifest == 3, zero orphans; backend `/api/health` ok (`chroma_ok:true`); frontend :3000 → 200
and its `/api/documents` proxy returns 3. Probe scripts live in `/home/user/e2e-scratch/` (outside the
deliverable). No residue.

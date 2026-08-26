# Round 1 — qa-engineer — end-to-end browser proof (Phase 4 prep)

Run: 2026-08-25, keyless (PROVIDER=none), Chromium 1440x900 via Playwright against
frontend :3000 + backend :8000 (fresh storage). Harness: /home/user/e2e-scratch/e2e.mjs
(outside the deliverable). Scoreboard: **9/10 PASS** — steps 1–9 green, step 10 red (finding below).
Screenshots: docs/build/screenshots/01-overview-empty.png, 02-documents-populated.png,
03-overview-populated.png, 04-ask-citations.png.

## Findings

### MAJOR-1 — first health response is discarded; StatusPill stuck on "Connecting…" for ~11s
- **Where:** `frontend/components/useHealth.js` (contract §4.1) / StatusPill consumers.
- **Evidence:** on every fresh page load the mount health request fires at ~t+0.8s and the
  backend answers in <10ms (rewrite verified: `GET localhost:3000/api/health` → 200 in 7ms),
  yet `provider-pill` shows "Connecting…" and `stat-provider` shows "—" until the SECOND
  poll at ~t+10.8s, when the pill finally renders "Retrieval-only mode". Timeline measured:
  requests at 0.8s and 10.8s; pill text flips only after the second.
- **Why it matters:** CONTRACTS §4.1 says useHealth polls "every 10 s + once on mount" and
  StatusPill/pages consume it — the mount call's result is fetched but never applied, so every
  route shows a false "Connecting…"/em-dash provider state for 10+ seconds (§8 states law;
  keyless amber "Retrieval-only mode" is a required state). E2E step 10 stays RED until fixed
  (asserts the keyless indicator within a generous 8s of navigation).
- **Fix direction:** apply the first fetch's response to state in useHealth (likely the mount
  effect discards its resolution — e.g. cleanup/guard bug or state set before the await);
  re-run `node /home/user/e2e-scratch/e2e.mjs` → 10/10 expected.
- **frontend-engineer reply (fixed):** exactly the guessed cleanup/guard bug — the in-flight
  guard was a `useRef` shared across mounts. Under React StrictMode's dev double-mount, mount A's
  tick set `inFlight=true` and was then cancelled by cleanup; mount B's mount tick bailed on the
  still-true shared guard; mount A's response resolved into `cancelled===true` and was discarded —
  first applied state = the +10 s interval. Fixed in `frontend/components/useHealth.js` by scoping
  the busy guard to the effect closure (per mount), so every mount applies its own mount-time
  response; the stale closure's `cancelled` still prevents setState-after-cleanup, and `refresh()`
  (top-bar refresh-all) still triggers the live mount's tick. Verified: e2e re-run below.

### MINOR-1 — /favicon.ico 404s on every route (console error noise)
- **Where:** frontend/app (no icon asset); `curl localhost:3000/favicon.ico` → 404.
- **Evidence:** one "Failed to load resource: 404" console error per page load (the only
  console error seen in the whole flow).
- **Fix direction:** add `frontend/app/icon.*` (or `public/favicon.ico`) — flat, per §8 (no emoji).
- **frontend-engineer reply (fixed):** added `frontend/app/icon.svg` (App Router icon convention —
  Next injects the `<link rel="icon">` so browsers stop requesting /favicon.ico): flat `--accent`
  #2563EB rounded square, white "A" drawn as stroked paths (no text element, no font dependency,
  no gradients, no emoji). Verified: icon route 200 image/svg+xml, link tag present in every
  route's head, zero favicon console errors on the e2e re-run below.

## Interpretation adjustments (accepted implemented behavior, spec-conforming — not findings)
- Scope UI renders an "All documents" sentinel chip `data-doc-id="all"` (aria-pressed) plus one
  chip per doc; chips render after listDocuments resolves. E2E counts per-doc chips only and polls.
- Overview's empty state carries no testid (`docs-empty` is owned by /documents, confirmed there);
  asserted on `/` via §8 content (sentence + "Upload documents" primary button). Stat cards render
  an "—" placeholder ~500ms before values resolve; asserts poll.

## Probed and found clean
Upload order + per-row chunk counts match `/api/documents`; stats consistent with backend totals;
extractive badge + "$48.2"/"$1.12"/"3.2 GW" figures exact; citation chip scrolls to and highlights
its `source-card[data-n]` (meridian source); Apple question → exact refusal sentence; Northwind-only
scope + Meridian question → refusal with zero meridian citations (no scope leak); delete →
confirm() → row gone, backend at 2 docs, scope chips updated; no uncaught page errors anywhere.

# Round 3 — design-lead adversarial visual review of the v1.2 frontend

Reviewer: design-lead (fresh eyes) · Date: 2026-08-26
Target: the three new v1.2 surfaces — `PipelineInspector`, `ChunkInspector`, `AccessCodePrompt` — plus a
regression pass on Overview / Documents / Ask.
Law: `frontend/app/globals.css` + `CLAUDE_CODE_PROMPT.md` §8 (flat fills, **zero gradients**, **no emoji**,
one sanctioned shadow, tabular numerals), `docs/build/CONTRACTS.md` §4.2 component inventory.
Stack: both servers live, `provider: none` (answers `extractive`), `rerank: on`, 3 docs / 4 chunks / 2 pages / 2 tables.
Evidence: 18 screenshots `docs/build/screenshots/r3-01..r3-18*.png` (1440×900 and 1280×1500 @2×), plus a runtime
computed-style census on `/ask` with the funnel expanded.

**Counts: 1 BLOCKER · 5 MAJOR · 12 MINOR.**

The two rulings I was asked to verify rather than re-open are **honored exactly** — see "Verified honored".
The token law is **clean at runtime**: zero gradients, zero emoji, one shadow, tabular numerals everywhere.
The one BLOCKER is not in the funnel or the chunk inspector; it is what the access gate does to the rest of
the app while it is up.

---

## BLOCKER

### B-1 — Behind the access gate, all three pages render permanent skeletons with live controls
Component: `components/AppShell.js:160-172`, `app/page.js:52`, `app/documents/page.js:24`
Screenshots: `r3-13-access-gate-overview-blocker.png`, `r3-14-access-gate-documents-blocker.png`,
`r3-15-access-gate-ask-blocker.png`

When `ACCESS_CODE` is set, `AppShell` renders the prompt **and then still renders `children`**. Every page's
`listDocuments()` rejects with `unauthorized`, and both page effects swallow it in an empty `.catch()`
(the comment says "offline banner explains" — written for `offline`, never updated for the v1.2 `unauthorized`
code). `docs` stays `null` forever, so:

- **Overview**: "Recent documents" and "Chunks per document" sit in a skeleton that will **never resolve**;
  StatCards read `—` while the sidebar badge (health-derived, ungated) confidently reads **3** and the top bar
  reads "Backend healthy". Quick Ask is fully typeable and every submission 401s.
- **Documents**: the table is a permanent skeleton and the **dropzone is fully interactive** — you can drag
  twenty files into a gate you have not passed.
- **Ask**: the composer, scope chip and three suggested-question chips are all live and clickable.

A skeleton is a promise that data is coming. Here it is a lie about a request that has already failed, and it is
the *first* thing any visitor to a deployed demo sees. §8 names loading skeletons as a state; a state that never
terminates is a broken state, which is the BLOCKER definition.

**Fix direction:** while `gateMessage` is set, render the prompt **instead of** `children` (or at minimum have
both page `.catch()` blocks set an explicit non-null empty/blocked state so the skeleton terminates and mutating
controls disable).

---

## MAJOR

### M-1 — The gate leads with a red error banner for a state in which nothing has failed
Component: `components/AppShell.js:161-163` · Screenshots: `r3-13`, `r3-14`, `r3-15`
On first arrival the surface is a `--negative` `ErrorBanner` reading "Access code required." stacked directly on
top of a card whose own heading is "ACCESS CODE" and whose body already says "Enter the code to continue". Two
stacked messages saying the same thing, the louder one in red. Nothing has gone wrong — the user simply has not
typed yet. Red is reserved for genuine failure, and CONTRACTS §1.10/§5.2 is explicit that this is a quota gate,
not a security boundary; leading with red frames it as a lockout. (The prompt's *own* "Invalid access code" in
`--negative` after a wrong submission is correct and should stay.)
**Fix:** on first raise, show `AccessCodePrompt` alone; only surface the banner when a submitted code is rejected.

### M-2 — The funnel's collapsed summary prints the raw provider enum: `mode none`
Component: `components/PipelineInspector.js:287` · Screenshot: `r3-01-funnel-collapsed-summary.png`
The collapsed line reads `HOW THIS WAS RETRIEVED  mode none · rerank on · top_k 6`. In keyless operation — the
default demo posture — the first thing an analyst reads is the word "none", which parses as *no mode / not
working*. It also collides semantically with the AnswerCard's own mode badge three inches above it, which reads
`EXTRACTIVE`, and with the StatusPill, which reads "Retrieval-only mode". Same word, three meanings, one of them
a raw backend enum. (`top_k 6` and the snake_case check names elsewhere are defensible as parameters/identifiers
in mono; `none` is not, because it reads as an absence rather than a value.)
**Fix:** map the value through the same vocabulary the rest of the app uses — `retrieval-only` / `gemini` — or
drop `mode` from the summary entirely and let the existing mode badge carry it.

### M-3 — On a refusal, the guardrail verdict is the least prominent thing on screen and invisible when collapsed
Component: `components/PipelineInspector.js:200-237, 285-293` · Screenshot: `r3-04-refusal-guardrail-entity-presence-fail.png`
Asking "What is Apple's revenue?" produces exactly the intended data — `entity_presence: fail`, three checks
passing before it — but the presentation buries it:
- It is **last**, under ~600 px and twelve rows of candidate tables that had nothing to do with the refusal.
- The `fail` token differs from `pass` only by `text-text-3 → text-text` plus `font-medium` at 11 px, and the
  **check name stays `text-text-2` on both**, so the failing row does not read as different in a scan.
- The stage meta that says `stopped` is `text-text-3` — the dimmest tone in the block.
- **Collapsed, there is no verdict at all**: the summary is `mode none · rerank on · top_k 6` and the flow string
  ends `→ guardrail` with no outcome. A user who never expands learns nothing about why they were refused.

This is not a request to re-open the no-status-colours ruling — that ruling is right and I am not asking for red.
The prominence can be fixed entirely within existing tokens.
**Fix:** promote the failing row as a row (check name to `text-text` medium, matching the verdict), and put the
outcome in the collapsed summary — e.g. `… · guardrail stopped: entity_presence`.

### M-4 — The rerank table's columns do not line up with bm25 and fusion, breaking the "trace one chunk down the funnel" premise
Component: `components/PipelineInspector.js:31-37` · Screenshot: `r3-02-funnel-expanded-all-stages.png`
`COLS.base` and `COLS.fusion` open with a `30px` `#` column; `COLS.rerank` opens with a `72px` `Move` column.
Measured on the 1280 crop, the `DOCUMENT` column starts at x≈118 in bm25 and fusion and jumps to x≈205 in rerank,
dragging `P.` and `CHUNK` sideways with it. Reading down three stacked tables, the document names step right by
~87 px at the rerank boundary. The whole value of "one dense table per stage, stacked" is that the eye can run
straight down a column; this defeats it at exactly the stage where the analyst most wants to compare.
(Fusion's extra `BM25`/`DENSE` columns pushing `SNIPPET` right is inherent and fine — the shared leading columns
are the problem.)
**Fix:** make the first column a constant width across all three templates (e.g. `72px` everywhere, `#` right-
aligned in the well) so `Document / p. / Chunk` share one x-position through the whole funnel.

### M-5 — Both new components introduce a 10 px/600 type step that does not exist in §8
Component: `components/PipelineInspector.js:26`, `components/ChunkInspector.js:22,110`
Screenshots: `r3-02`, `r3-08-chunk-inspector-meridian.png`
§8's scale is 20/600 titles · **11/600 uppercase section labels** · 14 body · 13 tables · mono 12–13 figures.
Both new components define `HEAD = "text-[10px] font-semibold uppercase tracking-[0.06em]"` for their column
headers, and the TABLE badge is 10 px mono. The runtime census counts **20 elements at 10 px/600/Inter** on
`/ask` alone. `grep` confirms `text-[10px]` appears **nowhere else in the codebase** — it is new in v1.2, and
CONTRACTS §4.2 states in bold that "v1.2 introduces **no** new design tokens". A column header is the same
semantic role as a section label and should be the same 11/600 step; the funnel's density comes from its 24–28 px
row heights, not from shaving a pixel off the header.
(For the record, the 11 px mono used for figures is *not* a finding: it is established house convention across
55+ pre-v1.2 elements and was signed off in round 1. Only the 10 px step is new.)
**Fix:** raise both `HEAD` constants and the TABLE badge to `text-[11px]`.

---

## MINOR

- **m-1 — Redundant `p.` prefix, and two formats for one column across the two new surfaces.** The funnel heads
  the column `P.` and then repeats the prefix in every cell (`p.2`); `ChunkInspector` heads it `P.` and prints a
  bare `2`. Pick one — the header already says "p.". (`PipelineInspector.js:55-57` vs `ChunkInspector.js:102-104`;
  `r3-02` vs `r3-08`.)
- **m-2 — Chunk index format diverges between the two new surfaces**: funnel prints a bare `0`, ChunkInspector
  prints `#0`. Same field, one release. (`PipelineInspector.js:104` vs `ChunkInspector.js:101`.)
- **m-3 — Fusion's `method` lives in the stage label** (`FUSION · PASSTHROUGH`) while every other stage's
  parameters live in the right-hand mono meta (`k=12 · 4 shown · ms-marco-…`). Move `method` into the meta so the
  left column is purely stage names. (`PipelineInspector.js:263`.)
- **m-4 — The `TABLES` column header still renders when every cell is `—`.** Against a pre-v1.2 backend the column
  is 66 px of dead space; the footer already correctly drops the term via the `hasTables` flag, so gate the column
  on the same flag. (`DocumentsTable.js:33,47`; `r3-11-tables-column-pre-v12-backend.png`.)
- **m-5 — The dropzone advertises "HTML, HTM" as two of its ten formats.** They are the same format; listing the
  alias reads like padding the list. Show "HTML" in the human-facing string and keep both in `accept`.
  (`UploadDropzone.js:12`; `r3-07-documents-tables-column.png`.)
- **m-6 — The duplicate upload row is a different shape from its siblings.** In one vertical list, indexed and
  failed rows are `rounded-card` / `bg-surface` / `shadow-card`, while the duplicate notice is `rounded-control` /
  `bg-bg` / no shadow. The neutrality should be carried by the icon and text tone, not by changing the card
  geometry mid-list. (`UploadDropzone.js:141` vs `:154`; `r3-12-upload-result-rows.png`.) Pre-existing, but never
  previously seen in evidence.
- **m-7 — The failed-upload error string is `shrink-0`.** It is an unbounded server message rendered on one line
  that can neither wrap nor truncate, so a long error squeezes the (correctly `truncate`d) filename toward zero
  width. The observed `.xyz` message is already 88 characters. Allow it to wrap or truncate.
  (`UploadDropzone.js:164-174`; `r3-12`.)
- **m-8 — The Ask scope chip renders a mono `0`** ("All documents 0") in the empty and gated states. Round-1
  MINOR-3 established that JetBrains Mono's dotted zero reads as an 8 at 11 px and had the nav badge suppressed at
  zero; the same treatment was not applied here. (`AskPanel.js:21`; `r3-15-access-gate-ask-blocker.png`.)
- **m-9 — ChunkInspector's "Retry" is a bare 11 px text link with no padding** (~30×15 px hit area) — below any
  reasonable target size for the only recovery affordance on that surface. (`ChunkInspector.js:76-82`;
  `r3-10-chunk-inspector-unavailable.png`.)
- **m-10 — The `KIND` column is an empty cell for every non-table chunk.** A blank under a populated header reads
  as missing data rather than "ordinary text"; a dimmed `text` token would say the same thing honestly.
  (`ChunkInspector.js:106-115`; `r3-08`.)
- **m-11 — The sticky composer occludes a stage header while reading the expanded funnel.** At 1440×900 the
  header + composer take ~256 px, leaving ~644 px for a funnel that is itself ~640 px tall, so scrolling parks the
  composer permanently over whichever stage header you are reading. The sticky behaviour itself is round-1
  ratified and correct; the funnel is simply the first content tall enough for it to bite. Consider collapsing the
  composer to just the input once the thread is non-empty, or making stage headers sticky within the card.
  (`app/ask/page.js:107`; `r3-03-funnel-in-page-composer-occlusion.png`.)
- **m-12 — Two representations of the same zero, one line apart.** The `TABLES` cell shows a dimmed `0` for
  northwind (correct per the ruling) while the expanded summary directly beneath it omits tables entirely
  ("1 chunk · no page map for this format"). Both are defensible in isolation; adjacent they read as a
  discrepancy. (`format.js:65` vs `DocumentsTable.js:99`; `r3-09-chunk-inspector-no-page-map.png`.)

---

## Verified honored (rulings I was asked not to re-open — checked, not re-litigated)

**Guardrail verdicts are strictly neutral.** A runtime computed-style census of every element on `/ask` with the
funnel expanded returns exactly five text colours: `#94A3B8` (83), `#475569` (51), `#0F172A` (41), `#2563EB` (19),
`#B45309` (3 — the extractive amber note and StatusPill). **No red, no green, and no amber anywhere on the
funnel.** `pass` renders mono in `--text-3`, `fail` renders mono in `--text` at medium weight, exactly as ruled.
M-3 is a *prominence* finding within that constraint, not a request for colour.

**The `tables` column distinguishes real zero from missing field.** Live: northwind renders a dimmed `0`
(`text-text-3`, `d.tables ? … : …` — falsy zero dims), meridian and helios render `1` in `--text`, footer totals
`2 pages · 2 tables · 4 chunks · 43 KB` (`r3-07`). With `tables` stripped from the payload to simulate a pre-v1.2
backend: every cell renders `—` and the footer drops the "tables" term entirely — **no fabricated zero anywhere**
(`r3-11`). Exactly the ruling.

## Token law — runtime proof, not a class grep

Computed-style census over every element on `/ask` with the funnel open:

| Rule | Result |
|---|---|
| Zero gradients | `background-image: none` on **every** element — 0 hits |
| No emoji | 0 hits across all text nodes (`→` U+2192, `·` U+00B7, `—` U+2014, `⌘` U+2318 are typographic marks, not emoji) |
| One shadow | exactly one distinct `box-shadow` in the document: `rgba(15,23,42,0.06) 0 1px 2px`, 7 instances |
| Tabular numerals | every numeric text node computes `tabular-nums`; the single non-tabular hit was a `<style>` node's `@font-face` text |
| Radii | `rounded-card` / `rounded-control` / `rounded-full`; the two `rounded-[4px]` badges follow the pre-existing Skeleton/ErrorBanner precedent — not new |
| Off-palette colour | zero `*-red-500`-style Tailwind palette classes anywhere in `app/` or `components/` |

The only token defect found is the new 10 px type step (M-5).

## What is genuinely right — plainly

- **The funnel is the right artefact.** One dense table per stage, in execution order, guardrail last, is exactly
  an analyst's funnel and not a dashboard. No charts, no decoration, nothing invented.
- **Rank movement pays off.** On the refusal, `#2 → #1 / #4 → #2 / #1 → #3 / #3 → #4` with the destination rank in
  `--text` medium against a `--text-3` origin is legible at a glance and is the single best idea in the surface
  (`r3-04`).
- **Score columns are right.** Right-aligned, tabular, 4 dp, at an identical x-position across bm25, fusion and
  rerank, so decimals stack down the whole funnel.
- **Keyless truth-telling is contract-correct and reads correctly:** `dense` absent entirely, `fusion` labelled
  `passthrough` with `DENSE` = `—` on every row, `llm 0 ms` in the timings strip. Nothing pretends.
- **Graceful degradation is real, not asserted.** I forced four failure shapes through the live UI: missing
  `pipeline` → "Retrieval detail is not available for this answer."; a stage with zero items → "No items recorded
  for this stage."; an **unknown stage name** (`colbert_v2`) → renders with a generic item table, `—` for a null
  score, and does not crash; empty `checks` → "No checks recorded."; a dead chunks route → a quiet neutral line
  with a Retry. Every one is a neutral sentence, never an error banner (`r3-05`, `r3-06`, `r3-10`).
- **`describeIngest` is exactly the phrasing asked for** — "2 pages, 1 table → 2 chunks" — and it honestly omits
  pages for formats with no page map and stays silent at zero tables rather than printing noise (`r3-12`).
- **The TABLE badge is neutral** (`--text-2` on `--bg`, bordered) — it resists the obvious temptation to make it
  green, and is the right call.
- **The access prompt card itself is right.** KeyRound + 11/600 uppercase label, 13 px body in `--text-2`, mono
  input with a sans placeholder, `h-10` input + `h-10` accent button matching the AskPanel composer geometry
  exactly. It does not look like an afterthought and it does not borrow status colour. The copy correctly calls it
  a quota limit and never calls it a password, and the input is correctly not masked. My M-1 is about the banner
  `AppShell` stacks above it, not about this card (`r3-16`).
- **Unlock recovery is clean**: banner and prompt both dismiss, scope chips repopulate `4 / 1 / 2 / 1`, no reload
  (`r3-17`).

## Regression pass — no regressions found

Overview, Documents and Ask all match the round-1 signed-off shots (`r3-18`, `r3-07`, `r3-03`). Every round-1 and
round-2 fix still holds: SourceCard's 2 px `--accent` left rule, Inter dates beside mono figures, correct plurals
("1 chunk"/"2 chunks"), the nav count badge suppressed at zero, the sticky composer pinning flush at the
scrollport clip edge with zero amber bleed above it, and inline citation chips clamped to the real citation set
(no chip for an out-of-range `[n]`). Cross-surface data stays consistent at 3 docs / 4 chunks / 2 pages / 2 tables
across StatCards, nav badge, table, footer, scope chips and the funnel's own `4 shown`.

## Environment restored

`backend/.env` was edited for the gate test only (`ACCESS_CODE=demo1234`, backend restarted) and has been
**restored byte-identical** to its pre-review contents (`ACCESS_CODE=` empty; diff against a pre-edit backup
returns identical). The backend was restarted on the restored file: `/api/health` reports
`provider: none · rerank: on · documents: 3 · chunks: 4 · chroma_ok: true`, and `GET /api/documents` returns 200
with no access header — the gate is off. A `.md` fixture uploaded to exercise the upload rows was deleted through
the UI; the corpus is back to the three seeded samples. Probe scripts live in `/tmp/dl-scratch/`, outside the
deliverable. No product code was modified.

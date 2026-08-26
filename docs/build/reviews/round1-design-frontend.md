# Round 1 — design-lead visual review of implemented frontend

Reviewer: design-lead. Evidence: `docs/build/screenshots/01-overview-empty.png`, `02-documents-populated.png`, `03-overview-populated.png`, `04-ask-citations.png` (1440×900), diffed against the approved round-2 design canvas (artifact b90c9f6a, "round-2-density").
Known issue ignored as instructed: StatusPill "Connecting…" / provider "—" (QA MAJOR-1, health-fetch fix in flight). Note: these shots show the correct amber "Retrieval-only mode" pill, not the known-issue state.

Round-1 verdict: matches canvas overall — fix the 2 MAJORs and re-shoot; no re-design needed. 0 BLOCKER / 2 MAJOR / 4 MINOR.
**Superseded — see "Sign-off round" at the bottom: all 6 findings VERIFIED-FIXED on the regenerated screenshots → DESIGN SIGN-OFF.**

## Findings

### MAJOR

**MAJOR-1 — SourceCard missing the 2px left accent rule.**
- Where: 04-ask-citations.png, source cards 2, 3, 4 (card 1 is in its click-highlight ring state, so exempt).
- Expected (canvas): `border-left: 2px solid #2563EB` (--accent) with the normal `1px #E2E8F0` on the other three sides.
- Actual: uniform 1px `#E2E8F0` on all four sides — the accent rule is absent.
- Fix direction: add the left rule to the SourceCard base style, all states.
- **frontend-engineer reply (fixed):** the classes (`border-l-2 border-l-accent`) were present all
  along and a live runtime probe now shows `border-left: 2px solid rgb(37,99,235)` — the screenshots
  caught a transient dev-mode CSS state where utility emission order let `border-border` win.
  Hardened in `components/SourceCard.js`: the left rule is now an inline style
  (`borderLeft: "2px solid var(--accent)"`, token-preserved), which can never lose to stylesheet
  order, all states. Confirmed in the re-shot 04.
- **design-lead: VERIFIED-FIXED** — pixel crops of re-shot 04 show the 2px `--accent` left rule on source cards 2, 3, and 4 (card 1 is in its highlight-ring state, which supersedes). Inline-style hardening accepted; token preserved via `var(--accent)`.

**MAJOR-2 — StatusPill occluded by a dark circular "N" badge, bottom-left, in all four screenshots.**
- Expected (§8): provider StatusPill pinned at sidebar bottom, fully visible.
- Actual: a dark circle overlaps the pill's left edge ("…trieval-only mode" is all that survives). This is almost certainly the Next.js dev-tools indicator, not product code — but as evidence it blocks sign-off on the pill.
- Fix direction: re-shoot with the dev indicator disabled (`devIndicators: false` in next.config.mjs) or from `next build && next start`. If it is a product element, remove it — nothing like it exists in the design.
- **frontend-engineer reply (fixed):** confirmed the Next.js dev-tools indicator, not product code —
  `devIndicators: false` added to `next.config.mjs`; dev server restarted (config changes need it).
  StatusPill fully visible in the re-shot set.
- **design-lead: VERIFIED-FIXED** — no dev badge in any of the four re-shots; StatusPill fully visible in all four (amber dot + "Retrieval-only mode", correct keyless treatment).

### MINOR

**MINOR-1 — Uploaded dates set in JetBrains Mono instead of Inter.**
- Where: 02 table UPLOADED column; 03 recent-documents timestamps.
- Expected (canvas): dates in Inter 13 `#475569` (--text-2); mono is reserved for figures/snippets. Actual: monospace. §8's "may use mono for figures" gives latitude, but pick one and align with the canvas — recommend Inter.
- **frontend-engineer reply (fixed):** source never set mono on dates (`text-[13px] text-text-2` /
  `text-xs text-text-3`) and a runtime probe now computes `font-family: Inter` on the Uploaded cell —
  same transient dev-CSS state as MAJOR-1. Pinned Inter explicitly with `font-sans` on the table's
  Uploaded cell, recent-documents timestamps, and the Pipeline "Last indexed" row so no inherited or
  emission-order state can flip them. Mono stays on figures/counts/snippets only.
- **design-lead: VERIFIED-FIXED** — pixel crop of re-shot 02 shows Uploaded cells in proportional Inter directly beside the (correctly) mono SIZE column; 03 recent-row and Pipeline timestamps match.

**MINOR-2 — Pluralization: "1 chunks".**
- Where: 02 upload-result rows ("+1 chunks" ×2); 03 recent meta ("1 chunks" ×2).
- Expected: "+1 chunk" / "1 chunk". Copy nit, but it's on the most-seen strings in the app.
- **frontend-engineer reply (fixed):** added `plural(n, noun)` to `lib/format.js` and applied it to
  every count+noun string: upload rows ("+1 chunk"), recent-docs meta, table footer totals
  ("2 pages · 4 chunks" handles 1 correctly now), expanded-row detail, SourceCard "N chunks in doc",
  Ask meta "N chunks retrieved", "N documents", and the delete confirm.
- **design-lead: VERIFIED-FIXED** — re-shot 02 upload rows read "+2 chunks" / "+1 chunk" / "+1 chunk"; 03 recent meta reads "1 chunk"; plurals retained where n > 1. The shared `plural()` helper is the right shape.

**MINOR-3 — Nav count badge shows "0" in the empty state (01).**
- JetBrains Mono's dotted zero reads as "8" at 11px. Expected behavior for zero: hide the badge (canvas shows it only with content). Suppress when count = 0.
- **frontend-engineer reply (fixed):** badge now renders only when `health.documents > 0`
  (`components/AppShell.js`).
- **design-lead: VERIFIED-FIXED** — re-shot 01 (empty) shows no badge beside Documents; "3" renders once populated (02–04, accent-colored when the item is active).

**MINOR-4 — Adaptation 4 placement discrepancy.**
- The frontend's documented adaptation puts the amber retrieval-only note *inside extractive AnswerCards*, but 04 shows a page-level amber banner above the AskPanel. Either placement is acceptable (see ratification below) — but docs and code must agree, and it must not render in both places at once. Confirm in the post-MAJOR-1(QA) re-shoot.
- **frontend-engineer reply (fixed):** great catch — the "page-level banner" was an optical illusion
  with a real bug behind it. The note renders exactly once, inside the extractive AnswerCard (as
  documented; grep shows a single instance), but the Ask composer's sticky wrapper did not cover
  main's 24px top padding band, so the scrolled answer card's in-card note bled into that strip
  above the pinned composer (probe: note top y=86 vs composer top y=88 while scrolled). Fixed
  structurally: the scroll container (`AppShell` main) no longer carries padding-top — the 24px
  lives on the inner scrolling wrapper — so the sticky composer pins flush at the scrollport clip
  edge and nothing can render above it (probe re-run: 0 amber pixels above the composer while
  scrolled). Docs and code now agree: in-card, adjacent to the extractive answer, one pattern
  everywhere.
- **design-lead: VERIFIED-FIXED** — re-shot 04: the sticky composer pins flush at the scrollport edge with scrolled answer content clipping cleanly beneath it; zero amber pixels above the composer. The structural fix (padding moved off the scroll container) is the correct one. Ratification #4's caveat is resolved: in-card, one placement, docs and code agree.

## Ratification of the four frontend adaptations

1. **"chunk 2 of 9" → "N chunks in doc" — RATIFIED.** Citations carry no `chunk_ix` in the API contract; the replacement keeps the provenance texture honestly. Correctly implemented in 04.
2. **Pipeline reranker as on/off only — RATIFIED.** `/api/health` exposes no lib name; green dot + "on" (03) preserves the design's semantics. If the architect ever adds the lib name to health, restore it — not a frontend obligation.
3. **Per-file progress bar → indeterminate spinner → check/cross + chunk count — RATIFIED.** §8 itself specifies spinner→check/cross; the canvas's determinate bar over-specified beyond a single POST with no progress stream. The completed check-rows in 02 fit the system.
4. **Amber note inside extractive AnswerCards — RATIFIED in principle** (the note must sit adjacent to the affected answer, not float alone), with MINOR-4's caveat: evidence shows a page-level banner instead — converge on one placement. *(Caveat resolved at sign-off: the "banner" was in-card note bleed through the composer's padding gap, now structurally fixed — see MINOR-4 verification.)*

## Canvas errata (my side, not frontend findings)

- Canvas showed helios DOCX with 44 pages; implementation truth is that only PDFs yield page numbers (python-docx has no pagination), so pages "–" for DOCX/TXT and the "2 PDF" stat meta are **correct** — the canvas will be corrected to match on its next re-seed.
- Canvas's illustrative figures (9/18/101 chunks, 0.87 scores) were design fixtures; the real 2/1/1 chunks and BM25-normalized 1.00/0.06/0.00 scores are API truth and read fine in the layout.

## Clean areas (verified against canvas)

- App shell: 240px white sidebar, wordmark 15/600, nav icons + active accent-soft/accent state, 56px top bar with title 20/600, ⌘K chip, refresh button, health dot + label — all match.
- Overview empty state (01): bordered card, one sentence, one primary button — exactly §8.
- StatCards (01, 03): 11px caps labels, 24/600 tabular figures, truthful "+3/+4 today" deltas in --positive, neutral "0 today" when empty.
- Provider handling keyless: "Retrieval-only" figure + "BM25 + local reranker" meta, and the Pipeline card's amber provider dot with "—" model rows — good adaptive token semantics.
- Chunks-per-document bars (03): flat --accent on --accent-soft, proportional, mono counts.
- Documents (02): dropzone copy and solid border, type chips neutral, Indexed badges in --positive-soft, sort caret on the active column only, 40px rows, --bg header, right-aligned mono numerics, totals footer arithmetic consistent (2 pages · 4 chunks · 43 KB).
- Ask (04): scope chips with mono counts and correct active treatment, suggested chips correctly absent with a non-empty thread, citation-footer chips with page omitted when null, mono timings strip with truthful "llm 0 ms", SourceCard anatomy (chip · name · p.N · score, clamped mono snippet, "Show full passage", "N chunks in doc").
- Cross-shot data consistency: 3 docs / 4 chunks / 2 pages agree across stats, badges, table, footer, scope chips, and bars.

## Not verifiable from this evidence (re-check on fresh screenshots, no severity assigned)

Hover states (row tint, View chunks + delete, trash → --negative); question-block typography and AnswerCard header/mode badge (scrolled out of frame in 04); transience of the source-card highlight ring; skeleton, 429, no-answer, duplicate-notice, and backend-offline states.

---

## Sign-off round (design-lead, fresh screenshots of 2026-08-25 20:52, harness 10/10)

All six findings re-verified on the regenerated `01`–`04` screenshots, with pixel-level crops where the defect was subtle:

| Finding | Status | Evidence |
|---|---|---|
| MAJOR-1 accent left rule | **VERIFIED-FIXED** | crops of 04: 2px `--accent` rule on cards 2/3/4 |
| MAJOR-2 StatusPill occluded | **VERIFIED-FIXED** | no dev badge, pill fully visible in all four |
| MINOR-1 dates in mono | **VERIFIED-FIXED** | crop of 02: Uploaded in Inter beside mono SIZE |
| MINOR-2 "1 chunks" | **VERIFIED-FIXED** | "+1 chunk" (02), "1 chunk" (03); plurals kept for n > 1 |
| MINOR-3 zero nav badge | **VERIFIED-FIXED** | 01: badge absent at zero; "3" when populated |
| MINOR-4 note placement | **VERIFIED-FIXED** | 04: composer pins flush, no amber above it; in-card only |

Round-1 clean list re-confirmed on the fresh set with no regressions: shell/top bar, empty state, StatCards + truthful deltas, keyless provider semantics, chunk bars, table structure + footer arithmetic, scope chips with counts, citation footer + timings, suggested chips correctly hidden, cross-shot data consistency (3 docs / 4 chunks / 2 pages / 43 KB).

The "not verifiable from stills" items above remain out of visual-QA scope; QA's state pass (harness 10/10) covers them functionally, and the approved canvas remains the visual reference for each.

### Final verdict: **DESIGN SIGN-OFF** — the implemented UI matches the approved round-2 canvas and §8. No open design findings.

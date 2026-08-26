"""Retrieval inspector contract tests -- CONTRACTS.md v1.2 SS1.6 + SS1.9.

Written against the contract, not the implementation.

The load-bearing law here is SS1.9.1: explain mode is an OBSERVABILITY VIEW over
work the pipeline already did. Zero extra LLM calls, zero extra embedding calls,
no second retrieval pass, and byte-identical `answer`/`citations`/ordering
versus `explain:false`. Every purity test below runs with the providers.py
Gemini seams replaced by tripwires (conftest.no_provider_calls), so a stray
provider call fails loudly instead of silently costing quota.

Keyless by construction (conftest pops GOOGLE_API_KEY at collection). SS1.9.3
says the `dense` stage is OMITTED ENTIRELY in keyless mode -- that omission is
the single most testable consequence of the four-path retrieval matrix (SS5).
"""

import pytest
from types import SimpleNamespace

from conftest import (
    EXPLAIN_CAPS,
    GUARDRAIL_CHECKS,
    SAMPLE_FILENAMES,
    app_client,
    index_samples,
    no_provider_calls,
    post_query,
)

ANSWERABLE = "What was Meridian's Q2 FY2026 revenue?"
UNANSWERABLE = "What was Contoso Manufacturing's FY2031 dividend policy?"
ITEM_KEYS = {
    "bm25": {"doc_id", "doc_name", "page", "chunk_ix", "score", "snippet"},
    "dense": {"doc_id", "doc_name", "page", "chunk_ix", "score", "snippet"},
    "fusion": {
        "doc_id", "doc_name", "page", "chunk_ix", "score",
        "bm25_rank", "dense_rank", "snippet",
    },
    "rerank": {
        "doc_id", "doc_name", "page", "chunk_ix",
        "before_rank", "after_rank", "score", "snippet",
    },
}
COMPARED_FIELDS = ("answer", "mode", "no_answer", "model", "degraded_reason", "citations")


# --------------------------------------------------------------------------
# fixtures
# --------------------------------------------------------------------------
@pytest.fixture(scope="module", params=["off", "on"], ids=["rerank-off", "rerank-on"])
def ex(request, tmp_path_factory, samples, qa):
    """Keyless indexed app, both rerank postures. Read-only for its module."""
    storage = tmp_path_factory.mktemp(f"explain-{request.param}") / "storage"
    with app_client(storage, env={"PROVIDER": "none", "RERANK": request.param}) as client:
        docs = index_samples(client, samples)
        health = client.get("/api/health").json()
        yield SimpleNamespace(
            client=client,
            docs=docs,
            ids=[d["id"] for d in docs.values()],
            provider=health["provider"],
            rerank=health["rerank"],
        )


def explain(client, question, **kw):
    resp = post_query(client, question, explain=True, **kw)
    assert resp.status_code == 200, f"explain query failed: HTTP {resp.status_code} {resp.text[:400]}"
    body = resp.json()
    assert "pipeline" in body, (
        "SS1.9: `pipeline` is present if and only if the request set explain:true"
    )
    return body


def stages_of(pipeline):
    return {s["stage"]: s for s in pipeline["stages"]}


# --------------------------------------------------------------------------
# SS1.9 presence / absence -- "no key at all", not null, not {}
# --------------------------------------------------------------------------
@pytest.mark.parametrize("extra", [{}, {"explain": False}], ids=["omitted", "false"])
def test_pipeline_key_absent_unless_explain_true(ex, extra):
    body = post_query(ex.client, ANSWERABLE, **extra).json()
    assert "pipeline" not in body, (
        "SS1.9: when explain is omitted or false the response must contain NO "
        f"`pipeline` key -- not null, not {{}}. Got: {body.get('pipeline')!r}"
    )


def test_explain_null_behaves_exactly_as_absent(ex):
    """SS1.6 (ruled r3): `null` is 'no value', not a wrong value -- HTTP 200, no
    `pipeline` key, no error. A client serializing an unset optional emits null."""
    resp = post_query(ex.client, ANSWERABLE, explain=None)
    assert resp.status_code == 200, (
        f"SS1.6 (ruled r3): explain:null must NOT be an error. Got {resp.status_code}: "
        f"{resp.text[:300]}"
    )
    body = resp.json()
    assert "pipeline" not in body, "explain:null carries no pipeline key at all (SS1.9)"
    plain = post_query(ex.client, ANSWERABLE).json()
    for field in COMPARED_FIELDS:
        assert body[field] == plain[field], (
            f"explain:null must be indistinguishable from an absent explain ({field})"
        )


@pytest.mark.parametrize(
    "value",
    ["true", "false", 1, 0, [], {}, "maybe", 2, ["true"], 1.0],
    ids=["str-true", "str-false", "int-1", "int-0", "list", "dict", "str-maybe",
         "int-2", "list-str", "float"],
)
def test_non_bool_explain_is_bad_request(ex, qa, value):
    """SS1.6 (ruled r3): any NON-NULL non-bool is 400. Truthiness is never inferred
    from a string or a number -- `"true"` and `1` are rejected, not coerced."""
    qa.assert_error_envelope(
        post_query(ex.client, ANSWERABLE, explain=value), status=400, code="bad_request"
    )


# --------------------------------------------------------------------------
# SS1.9.2 top-level shape
# --------------------------------------------------------------------------
def test_pipeline_top_level_shape(ex):
    body = explain(ex.client, ANSWERABLE, top_k=6)
    p = body["pipeline"]
    assert set(p) == {"mode", "rerank", "top_k", "stages"}, f"SS1.9.2 top-level keys: {sorted(p)}"
    assert p["mode"] == ex.provider, "pipeline.mode must be the EFFECTIVE provider (SS1.9.2)"
    assert p["rerank"] == ex.rerank, "pipeline.rerank must be the EFFECTIVE rerank state"
    assert p["top_k"] == 6 and isinstance(p["top_k"], int)
    assert isinstance(p["stages"], list) and p["stages"], "stages must be a non-empty array"


def test_stages_are_in_execution_order(ex):
    p = explain(ex.client, ANSWERABLE)["pipeline"]
    order = [s["stage"] for s in p["stages"]]
    canonical = ["bm25", "dense", "fusion", "rerank", "guardrail"]
    assert order == [s for s in canonical if s in order], (
        f"SS1.9.2: stages must be ordered by execution order, got {order}"
    )
    assert len(order) == len(set(order)), f"duplicate stages: {order}"


# --------------------------------------------------------------------------
# SS1.9.3 -- THE keyless assertion: bm25 + fusion + rerank present, dense absent
# --------------------------------------------------------------------------
def test_keyless_pipeline_has_bm25_fusion_guardrail_and_no_dense(ex):
    p = explain(ex.client, ANSWERABLE)["pipeline"]
    names = set(stages_of(p))
    assert {"bm25", "fusion", "guardrail"} <= names, (
        f"SS1.9.3: bm25, fusion and guardrail are always present, got {sorted(names)}"
    )
    assert "dense" not in names, (
        "SS1.9.3: the `dense` stage is gemini-mode only and must be OMITTED ENTIRELY "
        f"when keyless -- a stage exists iff work happened (resolution 7). Got {sorted(names)}"
    )


def test_rerank_stage_presence_matches_effective_state(ex):
    p = explain(ex.client, ANSWERABLE)["pipeline"]
    present = "rerank" in stages_of(p)
    assert present == (ex.rerank == "on"), (
        f"SS1.9.3: rerank stage present iff rerank is effective on "
        f"(effective={ex.rerank}, stage_present={present})"
    )


def test_fusion_is_passthrough_with_null_dense_ranks_when_keyless(ex):
    # top_k deliberately NON-DEFAULT and != FUSION_POOL: an implementation that
    # echoes back either constant cannot pass this by coincidence.
    fusion = stages_of(explain(ex.client, ANSWERABLE, top_k=4)["pipeline"])["fusion"]
    assert fusion["method"] == "passthrough", (
        "SS1.9.3 + resolution 7: keyless runs no RRF, so fusion.method is "
        f"'passthrough' (never omitted, never 'rrf'). Got {fusion.get('method')!r}"
    )
    expected = 12 if ex.rerank == "on" else 4
    assert fusion["k"] == expected, (
        "SS1.9.3 (clarified r3): k is the depth the stage ACTUALLY operated at, never a "
        "constant echoed back. Keyless rerank-on fuses over the FUSION_POOL(12) pool; "
        "keyless rerank-off is a passthrough over a top_k-deep BM25 list. "
        f"rerank={ex.rerank}, top_k=4 -> expected {expected}, got {fusion['k']!r}"
    )
    for item in fusion["items"]:
        assert item["dense_rank"] is None, (
            f"SS1.9.3: in none mode dense_rank is null for every item, got {item['dense_rank']!r}"
        )
        assert item["bm25_rank"] is not None, (
            "SS1.9.3: at least one of bm25_rank/dense_rank is non-null for every fusion item"
        )
        assert isinstance(item["bm25_rank"], int) and item["bm25_rank"] >= 1


@pytest.mark.parametrize("top_k", [3, 9], ids=["k3", "k9"])
def test_stage_k_tracks_the_requested_depth(ex, top_k):
    """SS1.9.3 (clarified r3): vary top_k and watch every k follow the real depth.

    A stage that hardcodes FUSION_POOL, SPARSE_TOP_K or EXPLAIN_RERANK_K passes a
    single-value test by luck; it cannot pass two different depths.
    """
    stages = stages_of(explain(ex.client, ANSWERABLE, top_k=top_k)["pipeline"])
    if ex.rerank == "on":
        assert stages["bm25"]["k"] == 12, stages["bm25"]["k"]
        assert stages["fusion"]["k"] == 12, stages["fusion"]["k"]
        assert stages["rerank"]["k"] == top_k, (
            f"rerank k is the `keep` argument = top_k({top_k}), got {stages['rerank']['k']!r}"
        )
    else:
        assert "rerank" not in stages, sorted(stages)
        assert stages["bm25"]["k"] == top_k, (
            f"keyless rerank-off retrieves BM25 top_k({top_k}), got {stages['bm25']['k']!r}"
        )
        assert stages["fusion"]["k"] == top_k, (
            "SS1.9.3 (clarified r3): passthrough fusion reports the depth of the list it "
            f"passed through ({top_k}), not FUSION_POOL. Got {stages['fusion']['k']!r}"
        )
    for name, stage in stages.items():
        if "items" in stage:
            assert len(stage["items"]) <= stage["k"] or len(stage["items"]) <= EXPLAIN_CAPS[name], (
                f"{name}: len(items)={len(stage['items'])} exceeds both k={stage['k']} and "
                f"the display cap {EXPLAIN_CAPS[name]}"
            )


def test_bm25_stage_depth_matches_the_path_actually_run(ex):
    p = explain(ex.client, ANSWERABLE, top_k=5)["pipeline"]
    bm25 = stages_of(p)["bm25"]
    expected = 12 if ex.rerank == "on" else 5
    assert bm25["k"] == expected, (
        "SS1.9.3: keyless bm25 k is the sparse depth the pipeline actually used -- "
        f"FUSION_POOL(12) with rerank on, top_k without it. rerank={ex.rerank}, "
        f"top_k=5, got k={bm25['k']!r}"
    )


# --------------------------------------------------------------------------
# SS1.9.3 item shape + display caps
# --------------------------------------------------------------------------
def test_stage_items_match_their_frozen_field_sets(ex):
    p = explain(ex.client, ANSWERABLE)["pipeline"]
    doc_ids = set(ex.ids)
    doc_names = set(SAMPLE_FILENAMES.values())
    for name, stage in stages_of(p).items():
        if name == "guardrail":
            continue
        assert isinstance(stage["items"], list), f"{name}.items must be a list"
        assert len(stage["items"]) <= EXPLAIN_CAPS[name], (
            f"SS1.9.3 display cap: {name} shows at most {EXPLAIN_CAPS[name]} items, "
            f"got {len(stage['items'])}"
        )
        for item in stage["items"]:
            assert set(item) == ITEM_KEYS[name], (
                f"SS1.9.3 {name} item fields: expected {sorted(ITEM_KEYS[name])}, "
                f"got {sorted(item)}"
            )
            assert item["doc_id"] in doc_ids, f"{name} item doc_id not in the corpus: {item['doc_id']}"
            assert item["doc_name"] in doc_names, f"{name} item doc_name unknown: {item['doc_name']}"
            assert isinstance(item["chunk_ix"], int) and item["chunk_ix"] >= 0
            assert item["page"] is None or (isinstance(item["page"], int) and item["page"] >= 1)
            assert isinstance(item["score"], (int, float)) and not isinstance(item["score"], bool)
            assert round(float(item["score"]), 4) == float(item["score"]), (
                f"SS1.9.3: stage scores are rounded to 4 dp, got {item['score']!r}"
            )
            snippet = item["snippet"]
            assert isinstance(snippet, str) and len(snippet) <= 120, (
                f"SS1.9.3: inspector snippets are <= 120 chars, got {len(snippet)} in {name}"
            )
            assert "  " not in snippet, f"SS1.9.3: whitespace is collapsed, got {snippet!r}"


def test_inspector_snippet_has_no_provenance_prefix(ex):
    p = explain(ex.client, ANSWERABLE)["pipeline"]
    for name, stage in stages_of(p).items():
        for item in stage.get("items", []):
            assert not item["snippet"].lstrip().startswith("["), (
                f"SS1.9.3: the `[doc - p.N]` provenance prefix must be stripped "
                f"from inspector snippets ({name}): {item['snippet'][:60]!r}"
            )


def test_inspector_snippet_is_the_chunk_head_not_the_citation_window(ex, qa):
    """SS1.9.3 (law): the inspector must NOT call synthesis.make_snippet -- its
    snippet is the chunk HEAD, verified here against the docstore text itself."""
    ingest = qa.backend_module("ingest")
    store = qa.backend_module("stores").get_store()
    heads = {}
    for node in store.nodes_for(None):
        md = node.metadata
        raw = node.get_content()
        prefix = ingest.provenance_prefix(md["doc_name"], md["page"])
        stripped = raw[len(prefix):] if raw.startswith(prefix) else raw
        heads[(md["doc_id"], md["chunk_ix"])] = " ".join(stripped.split())
    seen = 0
    for stage in explain(ex.client, ANSWERABLE)["pipeline"]["stages"]:
        for item in stage.get("items", []):
            head = heads[(item["doc_id"], item["chunk_ix"])]
            snippet = item["snippet"]
            body = snippet[:-1].rstrip() if snippet.endswith("…") else snippet
            assert head.startswith(body), (
                "SS1.9.3: the inspector snippet is the chunk HEAD (whitespace collapsed), "
                "deliberately NOT the question-relevant citation window.\n"
                f"  snippet: {snippet!r}\n  head:    {head[:160]!r}"
            )
            seen += 1
    assert seen, "inspector produced no snippets at all"


@pytest.mark.parametrize("stage_name", ["bm25", "fusion"])
def test_stage_items_are_rank_ordered_by_score(ex, stage_name):
    stage = stages_of(explain(ex.client, ANSWERABLE)["pipeline"])[stage_name]
    scores = [float(i["score"]) for i in stage["items"]]
    assert scores == sorted(scores, reverse=True), (
        f"SS1.9.2: {stage_name} items are shown in the retriever's own rank order, so "
        f"their scores descend. Got {scores}. A non-monotonic list means the stage is "
        "displaying scores from a LATER stage over an EARLIER stage's ordering."
    )


def test_stage_scores_are_not_shared_across_stages(ex):
    """SS1.9.3: 'Per stage it is the score that stage produced ... Scores are not
    comparable across stages.'

    The recorders capture the NodeWithScore objects themselves, so any later stage
    that mutates `.score` in place (rerank does) will retroactively rewrite the
    bm25/fusion stages unless their scores were snapshotted at capture time.
    """
    stages = stages_of(explain(ex.client, ANSWERABLE)["pipeline"])
    if "rerank" not in stages:
        pytest.skip("rerank effective off -- no later stage exists to leak scores")
    rerank_scores = {
        (i["doc_id"], i["chunk_ix"]): float(i["score"]) for i in stages["rerank"]["items"]
    }
    for name in ("bm25", "fusion"):
        shared = [
            key
            for i in stages[name]["items"]
            for key in [(i["doc_id"], i["chunk_ix"])]
            if key in rerank_scores and float(i["score"]) == rerank_scores[key]
        ]
        assert not shared, (
            f"SS1.9.3: the {name} stage is reporting the CROSS-ENCODER score for "
            f"{shared} -- an earlier stage must report its own score. Snapshot the "
            "float when the recorder captures the list, not the node reference."
        )


def test_rerank_stage_ranks_are_coherent(ex):
    if ex.rerank != "on":
        pytest.skip("rerank effective off -- stage is omitted by contract")
    # top_k=4 != EXPLAIN_RERANK_K(6) and != FUSION_POOL(12): a constant echo is visible.
    p = explain(ex.client, ANSWERABLE, top_k=4)["pipeline"]
    stage = stages_of(p)["rerank"]
    assert isinstance(stage.get("model"), str) and stage["model"], (
        "SS1.9.3: rerank.model is never null while the stage is present"
    )
    assert stage["k"] == p["top_k"] == 4, (
        "SS1.9.3 (clarified r3): rerank k is the depth it operated at -- the `keep` "
        f"argument, i.e. top_k(4) -- never EXPLAIN_RERANK_K. Got {stage['k']!r}"
    )
    after = [i["after_rank"] for i in stage["items"]]
    assert after == list(range(1, len(after) + 1)), (
        f"SS1.9.3: after_rank is contiguous from 1 within the shown items, got {after}"
    )
    for i in stage["items"]:
        assert isinstance(i["before_rank"], int) and i["before_rank"] >= 1, (
            f"SS1.9.3: before_rank is the 1-based rank in the fusion pool, got {i['before_rank']!r}"
        )


def test_display_caps_are_display_only(ex):
    """SS1.9.3: top_k=12 keeps rerank at 6 shown items while citations may hold 12."""
    # 11, not 12: still well above EXPLAIN_RERANK_K(6) but distinguishable from
    # FUSION_POOL, so a rerank stage echoing the pool constant is caught.
    body = explain(ex.client, ANSWERABLE, top_k=11)
    p = body["pipeline"]
    assert p["top_k"] == 11
    stages = stages_of(p)
    stage = stages.get("rerank")
    if stage is not None:
        assert len(stage["items"]) <= EXPLAIN_CAPS["rerank"], (
            f"rerank display cap is 6 even at top_k=11, got {len(stage['items'])}"
        )
        assert stage["k"] == 11, (
            f"SS1.9.3: rerank.k is the depth it kept to (top_k=11); the 6-item cap is "
            f"display-only. Got {stage['k']!r}"
        )
        assert stages["fusion"]["k"] == 12, stages["fusion"]["k"]
    else:
        assert stages["fusion"]["k"] == 11, (
            f"keyless rerank-off passthrough fuses a top_k(11)-deep list, got "
            f"{stages['fusion']['k']!r}"
        )
    assert len(body["citations"]) <= 11


# --------------------------------------------------------------------------
# SS1.9.4 guardrail stage
# --------------------------------------------------------------------------
@pytest.mark.parametrize("question", [ANSWERABLE, UNANSWERABLE], ids=["answerable", "refusal"])
def test_guardrail_stage_shape_and_agreement(ex, question):
    body = explain(ex.client, question)
    stage = stages_of(body["pipeline"])["guardrail"]
    assert set(stage) == {"stage", "passed", "checks"}, f"SS1.9.4 keys: {sorted(stage)}"
    assert isinstance(stage["passed"], bool)
    assert stage["passed"] is (not body["no_answer"]), (
        f"SS1.9.4: passed == not no_answer (passed={stage['passed']}, "
        f"no_answer={body['no_answer']})"
    )
    checks = stage["checks"]
    assert isinstance(checks, dict) and checks, "checks must be a non-empty object"
    unknown = set(checks) - GUARDRAIL_CHECKS
    assert not unknown, f"SS1.9.4 check names are frozen; unknown: {sorted(unknown)}"
    assert set(checks.values()) <= {"pass", "fail"}, (
        f"SS1.9.4: check values are exactly 'pass' or 'fail' -- no third value: {checks}"
    )
    fails = [k for k, v in checks.items() if v == "fail"]
    assert len(fails) <= 1, f"SS1.9.4: the guardrail short-circuits, so at most one fail: {checks}"
    names = list(checks)
    assert names[0] == "nonempty", (
        f"SS1.9.4: `nonempty` is evaluated first in every mode, got order {names}"
    )
    if fails:
        assert names[-1] == fails[0], (
            "SS1.9.4: checks after a fail are OMITTED (never guessed), so the fail is "
            f"the last recorded entry. Got {names}"
        )


def test_refusal_reports_a_failing_guardrail_check(ex):
    body = explain(ex.client, UNANSWERABLE)
    if not body["no_answer"]:
        pytest.fail(
            "the unanswerable probe was answered -- guardrail regression, not an explain bug: "
            f"{body['answer'][:200]!r}"
        )
    checks = stages_of(body["pipeline"])["guardrail"]["checks"]
    assert "fail" in checks.values(), (
        f"SS1.9.4: a refusal must record exactly one failing check, got {checks}"
    )


def test_empty_corpus_short_circuits_to_guardrail_only(stack, qa):
    body = post_query(stack.client, ANSWERABLE, explain=True).json()
    assert body["no_answer"] is True and body["citations"] == []
    p = body["pipeline"]
    assert p["stages"] == [
        {"stage": "guardrail", "passed": False, "checks": {"nonempty": "fail"}}
    ], (
        "SS1.9.4: an empty corpus short-circuits BEFORE retrieval runs -- the only "
        f"stage is the guardrail with nonempty:fail. Got {p['stages']}"
    )
    assert set(p) == {"mode", "rerank", "top_k", "stages"} and p["top_k"] == 6


def test_empty_doc_ids_scope_behaves_like_all_documents(ex):
    """SS1.6: absent/[] doc_ids = all documents -- [] must not be read as 'empty scope'."""
    body = explain(ex.client, ANSWERABLE, doc_ids=[])
    assert stages_of(body["pipeline"])["guardrail"]["checks"].get("nonempty") == "pass", (
        "an empty doc_ids list means ALL documents (SS1.6), not an empty corpus"
    )


# --------------------------------------------------------------------------
# SS1.9.1 HARD CONSTRAINT -- purity. The reason this file exists.
# --------------------------------------------------------------------------
@pytest.mark.parametrize("question", [ANSWERABLE, UNANSWERABLE], ids=["answerable", "refusal"])
def test_explain_makes_zero_provider_calls(ex, question):
    with no_provider_calls() as calls:
        resp = post_query(ex.client, question, explain=True)
    assert resp.status_code == 200, (
        "explain must not reach providers.py at all in keyless mode: "
        f"HTTP {resp.status_code} {resp.text[:300]}"
    )
    assert calls == [], f"SS1.9.1: explain made provider calls: {calls}"


def test_explain_and_plain_agree_field_for_field(ex):
    """SS1.9.1 rule 2 -- named in the contract as a QA gate."""
    for question in (ANSWERABLE, UNANSWERABLE, "How did NRR trend?"):
        plain = post_query(ex.client, question, top_k=6).json()
        with_explain = post_query(ex.client, question, top_k=6, explain=True).json()
        for field in COMPARED_FIELDS:
            assert with_explain[field] == plain[field], (
                f"SS1.9.1: explain changed `{field}` for {question!r} -- explain is an "
                f"observability view, never a second pass.\n"
                f"  explain:false -> {plain[field]!r}\n  explain:true  -> {with_explain[field]!r}"
            )
        assert "pipeline" not in plain


def test_explain_does_not_reorder_or_rescore_citations(ex):
    plain = post_query(ex.client, ANSWERABLE, top_k=4).json()["citations"]
    boxed = post_query(ex.client, ANSWERABLE, top_k=4, explain=True).json()["citations"]
    assert [c["n"] for c in boxed] == list(range(1, len(boxed) + 1))
    assert [(c["doc_id"], c["page"], c["score"]) for c in boxed] == [
        (c["doc_id"], c["page"], c["score"]) for c in plain
    ], "SS1.9.1: identical ordering AND identical scores, explain on or off"


def test_explain_is_stable_across_repeats(ex):
    first = explain(ex.client, ANSWERABLE)["pipeline"]
    second = explain(ex.client, ANSWERABLE)["pipeline"]
    assert first == second, "the inspector must be deterministic for an unchanged corpus"


def test_explain_respects_doc_id_scoping(ex):
    target = ex.docs[SAMPLE_FILENAMES["northwind"]]["id"]
    body = explain(ex.client, "What was revenue?", doc_ids=[target])
    seen = {
        item["doc_id"]
        for stage in body["pipeline"]["stages"]
        for item in stage.get("items", [])
    }
    assert seen <= {target}, (
        f"SS1.6: filters are built only from the validated doc_ids; inspector leaked {seen}"
    )


def test_explain_timings_still_present_and_sane(ex):
    body = explain(ex.client, ANSWERABLE)
    t = body["timings"]
    assert set(t) == {"retrieval_ms", "rerank_ms", "llm_ms", "total_ms"}
    assert all(isinstance(v, int) and v >= 0 for v in t.values()), t
    assert t["llm_ms"] == 0, "SS1.6: keyless is extractive, so llm_ms is 0"
    if ex.rerank == "off":
        assert t["rerank_ms"] == 0, "SS1.6: rerank_ms is 0 when rerank is effective off"


# --------------------------------------------------------------------------
# PROVIDER=auto -- the shipped default (SS5)
# --------------------------------------------------------------------------
def test_explain_under_auto_default_is_keyless_shaped(auto_indexed_stack):
    body = post_query(auto_indexed_stack.client, ANSWERABLE, explain=True).json()
    p = body["pipeline"]
    assert p["mode"] == "none", f"PROVIDER=auto with no key resolves to none (SS5): {p['mode']!r}"
    names = set(stages_of(p))
    assert "dense" not in names, f"auto-keyless must omit the dense stage, got {sorted(names)}"
    assert {"bm25", "fusion", "guardrail"} <= names


def test_explain_under_auto_makes_zero_provider_calls(auto_indexed_stack):
    with no_provider_calls() as calls:
        resp = post_query(auto_indexed_stack.client, ANSWERABLE, explain=True)
    assert resp.status_code == 200, resp.text[:300]
    assert calls == [], f"SS1.9.1 under the shipped default: provider calls made: {calls}"


# --------------------------------------------------------------------------
# Harness self-checks -- a green suite must be able to prove it can go red.
# The v1.1 round shipped a broken default behind 101 passing tests; these pin
# that the instruments in this file actually detect what they claim to.
# --------------------------------------------------------------------------
def test_provider_tripwire_actually_trips(ex, qa):
    providers = qa.backend_module("providers")
    with no_provider_calls() as calls:
        with pytest.raises(AssertionError):
            providers.embed_texts_cached(["anything"], "some-model")
        with pytest.raises(AssertionError):
            providers.complete_with_backoff("anything")
    assert calls == ["embed_texts_cached", "complete_with_backoff"], (
        f"the tripwire must record every intercepted call, got {calls}"
    )


def test_explain_assertions_are_not_vacuous(ex):
    """Every shape assertion above iterates `items`; prove they are non-empty."""
    body = explain(ex.client, ANSWERABLE)
    assert body["citations"], "the answerable probe must produce citations"
    stages = stages_of(body["pipeline"])
    counted = {name: len(s.get("items", [])) for name, s in stages.items() if "items" in s}
    assert counted, "no stage carried an items list"
    assert all(n > 0 for n in counted.values()), (
        f"a stage with zero items makes its field-set assertions vacuous: {counted}"
    )


# --------------------------------------------------------------------------
# SS1.6 payload edges, with explain on
# --------------------------------------------------------------------------
def test_explain_respects_question_length_bounds(ex, qa):
    assert post_query(ex.client, "x" * 2000, explain=True).status_code == 200
    qa.assert_error_envelope(
        post_query(ex.client, "x" * 2001, explain=True), status=400, code="bad_request"
    )
    qa.assert_error_envelope(
        post_query(ex.client, "   ", explain=True), status=400, code="bad_request"
    )


@pytest.mark.parametrize("top_k", [0, 13, -1], ids=["zero", "thirteen", "negative"])
def test_explain_respects_top_k_bounds(ex, qa, top_k):
    qa.assert_error_envelope(
        post_query(ex.client, ANSWERABLE, top_k=top_k, explain=True),
        status=400,
        code="bad_request",
    )


@pytest.mark.parametrize("top_k", ["6", 6.0], ids=["string", "float"])
def test_top_k_wrong_type_is_bad_request(indexed_stack, qa, top_k):
    """SS1.1 lists 'bad types' under bad_request for /api/query, and SS1.6 makes
    `explain` a strict bool for exactly that reason. `top_k` must be as strict:
    a JSON string or float is a bad type, not something to silently coerce."""
    qa.assert_error_envelope(
        post_query(indexed_stack.client, ANSWERABLE, top_k=top_k), status=400, code="bad_request"
    )


def test_explain_with_unknown_doc_id_is_404_not_a_pipeline(ex, qa):
    import uuid as _uuid

    resp = post_query(ex.client, ANSWERABLE, doc_ids=[str(_uuid.uuid4())], explain=True)
    qa.assert_error_envelope(resp, status=404, code="not_found")
    assert "pipeline" not in resp.text, "an error response must never carry a pipeline"


# --------------------------------------------------------------------------
# Always-default sweep: a field that never leaves its default value passes every
# presence and type assertion ever written. These pin VARIATION, not shape.
# --------------------------------------------------------------------------
def test_pipeline_scores_and_ranks_are_not_stuck_at_a_default(ex):
    p = explain(ex.client, ANSWERABLE, top_k=6)["pipeline"]
    for name, stage in stages_of(p).items():
        items = stage.get("items")
        if not items:
            continue
        scores = [float(i["score"]) for i in items]
        assert any(s != 0.0 for s in scores), (
            f"SS1.9.3: every {name} score is 0.0 -- a stage reporting a constant tells the "
            f"reader nothing through the feature built to show them the truth. {scores}"
        )
        if len(scores) >= 2:
            assert len(set(scores)) > 1, (
                f"SS1.9.3: all {len(scores)} {name} scores are identical ({scores[0]}); "
                "ranked retrieval does not produce ties across a whole stage"
            )


def test_fusion_bm25_ranks_are_distinct_and_one_based(ex):
    fusion = stages_of(explain(ex.client, ANSWERABLE)["pipeline"])["fusion"]
    ranks = [i["bm25_rank"] for i in fusion["items"]]
    assert ranks == sorted(ranks), f"SS1.9.3: passthrough preserves BM25 order, got {ranks}"
    assert len(set(ranks)) == len(ranks), (
        f"SS1.9.3: bm25_rank is a 1-based rank WITHIN the retriever's own list, so the "
        f"values are distinct -- not a constant repeated per row. Got {ranks}"
    )
    assert ranks[0] == 1, f"the top passthrough item is bm25_rank 1, got {ranks[0]!r}"


def test_rerank_before_ranks_are_distinct(ex):
    stages = stages_of(explain(ex.client, ANSWERABLE)["pipeline"])
    if "rerank" not in stages:
        pytest.skip("rerank effective off -- stage omitted by contract")
    before = [i["before_rank"] for i in stages["rerank"]["items"]]
    assert len(set(before)) == len(before), (
        f"SS1.9.3: before_rank is each chunk's own position in the fusion pool, so the "
        f"values are distinct -- not all 1. Got {before}"
    )


def test_citation_scores_vary_across_citations(ex):
    citations = post_query(ex.client, ANSWERABLE, top_k=6).json()["citations"]
    if len(citations) < 2:
        pytest.skip("single-citation answer -- nothing to compare")
    scores = [c["score"] for c in citations]
    assert len(set(scores)) > 1, f"SS1.6: citation scores are stuck at one value: {scores}"
    assert scores == sorted(scores, reverse=True), f"citations are in rank order: {scores}"

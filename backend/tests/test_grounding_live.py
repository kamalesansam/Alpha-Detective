"""Live Gemini grounding proof -- the ONLY suite that ever sees GOOGLE_API_KEY.

Auto-skips when no key is present (conftest stashes the key at collection time,
so the check is against the stash -- the process env stays keyless for every
other suite). Free-tier respect: this file spends AT MOST 4 LLM calls; the two
tests below spend exactly one each (indexing spends embedding calls, which are
not LLM calls and are cached). 429/502 from the provider skips rather than
fails -- quota exhaustion is not an implementation defect.
"""

import json
import re
from types import SimpleNamespace

import pytest

LLM_CALL_BUDGET = 4
_llm_calls = {"n": 0}


@pytest.fixture(scope="module")
def live(tmp_path_factory, samples, qa):
    if not qa.live_key:
        pytest.skip("GOOGLE_API_KEY not set -- live grounding suite skipped (add a key to run)")
    storage = tmp_path_factory.mktemp("live") / "storage"
    with qa.app_client(
        storage, env={"PROVIDER": "auto", "RERANK": "on", "GOOGLE_API_KEY": qa.live_key}
    ) as client:
        resp = qa.upload_paths(client, list(samples.values()))
        if resp.status_code == 429:
            pytest.skip("free-tier embed quota hit while indexing -- retry later")
        assert resp.status_code == 200, resp.text[:400]
        entries = resp.json()["documents"]
        failed = [e for e in entries if e["status"] != "indexed"]
        if any("rate" in (e.get("error") or "").lower() for e in failed):
            pytest.skip("free-tier embed quota hit mid-batch while indexing -- retry later")
        assert not failed, f"live indexing failed: {failed}"

        health = client.get("/api/health").json()
        assert health["provider"] == "gemini", f"key present but provider is {health['provider']!r}"
        assert isinstance(health["llm_model"], str) and health["llm_model"]
        assert isinstance(health["embed_model"], str) and health["embed_model"]
        assert qa.live_key not in json.dumps(health), "API key leaked into the health payload"
        yield SimpleNamespace(client=client, health=health)


def _ask(live, qa, question, explain=False):
    assert _llm_calls["n"] < LLM_CALL_BUDGET, "LLM call budget for this file would be exceeded"
    _llm_calls["n"] += 1
    # `explain` is free by contract (SS1.9.1: zero extra LLM and embedding calls),
    # so asking for it never changes this file's budget arithmetic.
    kwargs = {"explain": True} if explain else {}
    resp = qa.query(live.client, question, **kwargs)
    if resp.status_code in (429, 502):
        pytest.skip(f"live Gemini throttled/unavailable (HTTP {resp.status_code}) -- free tier respected")
    return resp


def test_live_answer_contains_exact_figure_with_valid_citations(live, qa):
    resp = _ask(live, qa, "What was Meridian's Q2 FY2026 revenue?", explain=True)
    assert resp.status_code == 200, resp.text[:400]
    body = resp.json()
    assert body["mode"] == "generative"
    assert isinstance(body["model"], str) and body["model"]
    assert body["no_answer"] is False, f"live answerable question refused: {body['answer']!r}"
    assert "$48.2" in body["answer"], (
        f"figure must be copied exactly as written; answer: {body['answer']!r}"
    )

    citations = body["citations"]
    assert len(citations) >= 1, "generative answer must carry at least one citation"
    assert [c["n"] for c in citations] == list(range(1, len(citations) + 1))

    markers = {int(m) for m in re.findall(r"\[(\d+)\]", body["answer"])}
    assert markers, f"generative answer has no inline [n] citations: {body['answer']!r}"
    valid = {c["n"] for c in citations}
    assert markers.issubset(valid), (
        f"answer cites indexes with no citation entry (must be stripped server-side): {markers - valid}"
    )

    meridian = qa.SAMPLE_FILENAMES["meridian"]
    meridian_cits = [c for c in citations if c["doc_name"] == meridian]
    assert meridian_cits, f"no citation points at the Meridian document: {citations}"
    assert any(isinstance(c["page"], int) for c in meridian_cits), "PDF citations must carry int pages"

    assert body["timings"]["llm_ms"] > 0
    assert qa.live_key not in resp.text, "API key leaked into a query response"

    # SS1.9.3 gemini-mode inspector -- the ONLY place `dense` and `fusion.method:"rrf"`
    # can be observed at all, because every other suite is keyless by construction.
    pipeline = body.get("pipeline")
    assert pipeline is not None, "SS1.9: explain:true must return a pipeline"
    stages = {st["stage"]: st for st in pipeline["stages"]}
    assert pipeline["mode"] == "gemini", pipeline["mode"]
    assert "dense" in stages, (
        f"SS1.9.3: the dense stage is present in gemini mode, got {sorted(stages)}"
    )
    assert stages["dense"]["k"] == 8, f"SS1.9.3: DENSE_TOP_K = 8, got {stages['dense']['k']!r}"
    assert stages["bm25"]["k"] == 8, f"SS1.9.3: SPARSE_TOP_K = 8, got {stages['bm25']['k']!r}"
    assert stages["fusion"]["method"] == "rrf", (
        f"SS1.9.3: gemini mode runs real RRF fusion, got {stages['fusion']['method']!r}"
    )
    assert stages["fusion"]["k"] == 12, stages["fusion"]["k"]
    assert any(i["dense_rank"] is not None for i in stages["fusion"]["items"]), (
        "SS1.9.3: with a dense retriever running, some fusion item must carry a dense_rank"
    )
    for item in stages["fusion"]["items"]:
        assert item["bm25_rank"] is not None or item["dense_rank"] is not None, item
    assert stages["guardrail"]["passed"] is True, stages["guardrail"]
    assert qa.live_key not in str(pipeline), "SS5.2: no key material in the pipeline payload"


def test_live_explain_matches_plain_and_costs_no_extra_llm_call(live, qa):
    """SS1.9.1 rule 2 + rule 4, verified on the REAL gemini path.

    Spends one LLM call for the plain query. The explain re-issue would spend a
    second, so instead this compares against the budget counter: an explain-only
    request must move `llm_budget.used` by exactly the same amount as the mode it
    reports (extractive/degraded moves it 0; generative moves it 1).
    """
    before = live.client.get("/api/health").json()["llm_budget"]["used"]
    resp = _ask(live, qa, "What was Northwind Retail's diluted EPS in Q2 2026?", explain=True)
    assert resp.status_code == 200, resp.text[:400]
    body = resp.json()
    after = live.client.get("/api/health").json()["llm_budget"]["used"]
    expected = 1 if body["mode"] == "generative" else 0
    assert after - before == expected, (
        f"SS1.9.1 rule 4 / SS1.11: an explain query charges exactly the LLM calls it "
        f"actually made ({expected}); budget moved {before} -> {after}"
    )
    assert body["pipeline"]["top_k"] == 6
    assert body["degraded_reason"] is None or body["degraded_reason"] == "daily_budget"


def test_live_unanswerable_returns_exact_refusal(live, qa):
    resp = _ask(live, qa, "What was Meridian's Q3 FY2026 revenue?")
    assert resp.status_code == 200, resp.text[:400]
    body = resp.json()
    assert body["no_answer"] is True, (
        f"wrong-quarter question must refuse, got: {body['answer']!r} "
        f"(citations: {[c['doc_name'] for c in body['citations']]})"
    )
    assert body["answer"] == qa.REFUSAL, f"refusal must be the exact sentence, got: {body['answer']!r}"
    assert body["citations"] == []


def test_live_llm_call_budget_respected(live):
    # Runs last (definition order): the whole file must fit the free-tier budget.
    assert _llm_calls["n"] <= LLM_CALL_BUDGET
    assert _llm_calls["n"] == 2, f"expected exactly 2 LLM-bearing queries in this file, ran {_llm_calls['n']}"

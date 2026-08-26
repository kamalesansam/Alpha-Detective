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


def _ask(live, qa, question):
    assert _llm_calls["n"] < LLM_CALL_BUDGET, "LLM call budget for this file would be exceeded"
    _llm_calls["n"] += 1
    resp = qa.query(live.client, question)
    if resp.status_code in (429, 502):
        pytest.skip(f"live Gemini throttled/unavailable (HTTP {resp.status_code}) -- free tier respected")
    return resp


def test_live_answer_contains_exact_figure_with_valid_citations(live, qa):
    resp = _ask(live, qa, "What was Meridian's Q2 FY2026 revenue?")
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

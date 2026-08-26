"""THE ACCURACY GATE (CLAUDE_CODE_PROMPT SS7/SS11, CONTRACTS.md SS5) -- keyless, PROVIDER=none.

100% required: every answerable eval case must place its expected document in
the top-3 citations with `expect_substring` present in one of that document's
returned snippets. Unanswerable cases must return no_answer:true with zero
citations and the exact refusal sentence. doc_ids scoping must never leak
citations from outside the scope.

The whole gate runs TWICE via the parametrized module fixture:
  * RERANK=on  -- or gracefully degraded: if the local cross-encoder is
    unavailable, health must report the effective state "off" and the gate
    must still pass (degradation may never crash or change API shape);
  * RERANK=off -- pure BM25 ordering (PROVIDER=none path per SS5 matrix).

Each case is parametrized individually so a failure names the exact question
and prints the top-3 results for triage.
"""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

EVAL_PATH = Path(__file__).resolve().parent / "eval_set.json"
EVAL_CASES = json.loads(EVAL_PATH.read_text(encoding="utf-8"))
ANSWERABLE = [c for c in EVAL_CASES if not c.get("expect_no_answer")]
UNANSWERABLE = [c for c in EVAL_CASES if c.get("expect_no_answer")]

assert len(EVAL_CASES) >= 20, "eval_set.json must hold >= 20 cases"
assert len(UNANSWERABLE) >= 3, "eval_set.json must hold >= 3 unanswerable cases"


def _cid(case):
    slug = "".join(ch if ch.isalnum() else "-" for ch in case["question"].lower())
    slug = "-".join(part for part in slug.split("-") if part)[:60]
    return f"{case.get('category', 'case')}--{slug}"


def _fmt_top(citations):
    lines = []
    for i, c in enumerate(citations):
        lines.append(
            f"  #{i + 1} {c.get('doc_name')} p.{c.get('page')} score={c.get('score')} "
            f":: {str(c.get('snippet'))[:90]!r}"
        )
    return "\n".join(lines) if lines else "  (no citations)"


@pytest.fixture(scope="module", params=["on", "off"], ids=["rerank-on", "rerank-off"])
def gate(request, tmp_path_factory, samples, qa):
    storage = tmp_path_factory.mktemp(f"gate-{request.param}") / "storage"
    with qa.app_client(storage, env={"PROVIDER": "none", "RERANK": request.param}) as client:
        docs = qa.index_samples(client, samples)
        health = client.get("/api/health").json()
        assert health["provider"] == "none", f"gate must run keyless: {health}"
        if request.param == "off":
            assert health["rerank"] == "off", f"RERANK=off not honored: {health}"
        else:
            # requested on: either truly on, or gracefully degraded to off
            assert health["rerank"] in ("on", "off"), f"invalid effective rerank: {health}"
        yield SimpleNamespace(
            client=client,
            docs=docs,
            ids={name: entry["id"] for name, entry in docs.items()},
            requested=request.param,
            effective=health["rerank"],
        )


def test_rerank_effective_state_is_honest(gate):
    health = gate.client.get("/api/health").json()
    assert health["rerank"] == gate.effective, "health rerank state changed between calls"
    if gate.requested == "off":
        assert health["rerank"] == "off"


@pytest.mark.parametrize("case", ANSWERABLE, ids=_cid)
def test_eval_case_hits_expected_doc_top3(gate, qa, case):
    resp = qa.query(gate.client, case["question"])
    assert resp.status_code == 200, (
        f"Q: {case['question']!r} -> HTTP {resp.status_code}: {resp.text[:300]}"
    )
    body = resp.json()
    assert body["mode"] == "extractive" and body["model"] is None, (
        f"PROVIDER=none must answer extractively with model:null: mode={body['mode']} model={body['model']}"
    )
    top3 = body["citations"][:3]
    ctx = (
        f"\n[rerank requested={gate.requested} effective={gate.effective}]"
        f"\nQ: {case['question']!r}"
        f"\nexpected: {case['expect_doc']} with snippet containing {case['expect_substring']!r}"
        f"\ntop-3:\n{_fmt_top(top3)}"
    )
    assert body["no_answer"] is False, "answerable eval case returned no_answer" + ctx
    assert any(c["doc_name"] == case["expect_doc"] for c in top3), (
        "expected document not in top-3" + ctx
    )
    hits = [
        c
        for c in top3
        if c["doc_name"] == case["expect_doc"] and case["expect_substring"] in c["snippet"]
    ]
    assert hits, "expect_substring not found in any top-3 snippet from the expected doc" + ctx
    if case.get("expect_page") is not None:
        assert any(c["page"] == case["expect_page"] for c in hits), (
            f"no hit snippet on expected page {case['expect_page']}" + ctx
        )
    if case.get("category") == "trap":
        # cross-document trap: the answer's source must be this company's doc and
        # never another -- rank-1 must be the expected doc, and the figure must
        # not surface from any other document's snippet.
        assert top3[0]["doc_name"] == case["expect_doc"], (
            "trap case: rank-1 result is another company's document" + ctx
        )
        for c in top3:
            if c["doc_name"] != case["expect_doc"]:
                assert case["expect_substring"] not in c["snippet"], (
                    "trap case: another document's snippet carries this company's figure" + ctx
                )


@pytest.mark.parametrize("case", UNANSWERABLE, ids=_cid)
def test_unanswerable_case_refuses(gate, qa, case):
    resp = qa.query(gate.client, case["question"])
    assert resp.status_code == 200, (
        f"no_answer is never an HTTP error; Q: {case['question']!r} -> {resp.status_code}: {resp.text[:300]}"
    )
    body = resp.json()
    ctx = (
        f"\n[rerank requested={gate.requested} effective={gate.effective}]"
        f"\nQ: {case['question']!r} ({case.get('note', '')})"
        f"\nanswer: {body.get('answer')!r}\ncitations:\n{_fmt_top(body.get('citations', [])[:3])}"
    )
    assert body["no_answer"] is True, "unanswerable case produced an answer" + ctx
    assert body["answer"] == qa.REFUSAL, "refusal sentence not exact" + ctx
    assert body["citations"] == [], "no_answer responses must carry zero citations" + ctx


def test_scoping_meridian_question_scoped_to_northwind_refuses(gate, qa):
    """Ask a Meridian question scoped to Northwind only: no_answer, and never a
    citation from outside the validated doc_ids scope (Chroma/BM25 filter law)."""
    northwind_id = gate.ids[qa.SAMPLE_FILENAMES["northwind"]]
    resp = qa.query(gate.client, "What was Meridian's Q2 FY2026 revenue?", doc_ids=[northwind_id])
    assert resp.status_code == 200, resp.text[:300]
    body = resp.json()
    leaked = [c for c in body["citations"] if c["doc_id"] != northwind_id]
    assert not leaked, f"doc_ids scope leaked citations from outside the scope: {leaked}"
    assert "$48.2" not in resp.text, "Meridian figure surfaced despite Northwind-only scope"
    assert body["no_answer"] is True, (
        f"scoped-out question must refuse; got answer={body['answer']!r} "
        f"citations:\n{_fmt_top(body['citations'][:3])}"
    )
    assert body["citations"] == [] and body["answer"] == qa.REFUSAL


def test_scoping_positive_control_and_timings(gate, qa):
    """Scoping must not break in-scope answers; timings per SS1.6."""
    northwind_id = gate.ids[qa.SAMPLE_FILENAMES["northwind"]]
    resp = qa.query(gate.client, "What was Northwind Retail's revenue in Q2 2026?", doc_ids=[northwind_id])
    assert resp.status_code == 200, resp.text[:300]
    body = resp.json()
    assert body["no_answer"] is False, "in-scope question refused:\n" + _fmt_top(body["citations"][:3])
    assert body["citations"], "in-scope question returned zero citations"
    assert all(c["doc_id"] == northwind_id for c in body["citations"]), (
        f"scoped query returned out-of-scope citations: {_fmt_top(body['citations'])}"
    )
    assert any("$1.84" in c["snippet"] for c in body["citations"][:3]), (
        "scoped Northwind revenue not found in top-3 snippets:\n" + _fmt_top(body["citations"][:3])
    )
    timings = body["timings"]
    for key in ("retrieval_ms", "rerank_ms", "llm_ms", "total_ms"):
        assert isinstance(timings[key], int) and timings[key] >= 0, f"bad timing {key}: {timings}"
    assert timings["llm_ms"] == 0, "PROVIDER=none must never spend an LLM call"
    if gate.effective == "off":
        assert timings["rerank_ms"] == 0, "rerank_ms must be 0 when rerank is effectively off"

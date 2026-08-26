"""Deployment rails -- CONTRACTS.md v1.2 SS1.10, SS1.11, SS5 (+ SS1.2 llm_budget).

These are the surfaces that only exist because the app is going on a public URL:
the ACCESS_CODE quota gate, the per-IP throttle, the daily LLM budget, the
corpus cap, CORS-from-env and startup auto-seeding.

Two laws drive most of the assertions:
  * SS1.11 / resolution 14 -- budget exhaustion DEGRADES, it never 429s.
    "Retrieval never stops." Search, citations, chunks and uploads keep working;
    only generation is suspended.
  * SS1.10 (law) -- ACCESS_CODE is a QUOTA GATE, not a security boundary, and
    GET routes are deliberately exempt from the throttle so a polling frontend
    cannot lock an honest user out of their own UI.

Keyless throughout. The one place that needs gemini semantics (the
budget-degraded query) fakes ONLY the api-layer bundle, deliberately leaving
retrieval on the keyless path so the test cannot fail for provider reasons.
"""

import contextlib
import datetime as dt
import json
import logging
import sys
import uuid

import pytest

from conftest import (
    BACKEND_DIR,
    SAMPLE_FILENAMES,
    app_client,
    post_query,
    upload_bytes,
)

CODE = "s3cret-quota-gate"
TXT = ("rails.txt", b"Rails fixture document with retrievable Vantage content.\n")
THROTTLE_MSG = "Too many requests — slow down"


def today_utc():
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")


@contextlib.contextmanager
def force_gemini_at_the_api_layer(qa):
    """Make api.py believe the provider is gemini WITHOUT moving retrieval off the
    keyless path (SS5's four-path matrix would otherwise try to embed the query).

    SS2 api.py: `generative = (bundle.provider == "gemini") and providers.reserve_llm_call()`.
    That single boolean is what this exercises.
    """
    import dataclasses

    providers = qa.backend_module("providers")
    real = providers.get_bundle()
    fake = dataclasses.replace(
        real,
        provider="gemini",
        llm_model_name="gemini-flash-latest",
        embed_model_name="gemini-embedding-001",
    )
    # Patch the api layer ONLY. Rebinding providers.get_bundle would also flip
    # retrieval onto the gemini path (SS5 matrix) and it would try to embed the
    # question with a null embed model -- a 502 for reasons unrelated to SS1.11.
    api = qa.backend_module("api")
    assert hasattr(api, "get_bundle"), (
        "SS2 api.py resolves the bundle via a module-level `get_bundle` -- the seam "
        "this test needs to fake gemini mode without moving retrieval off keyless"
    )
    targets = [api]
    saved = [(m, m.get_bundle) for m in targets]
    for m in targets:
        m.get_bundle = lambda _f=fake: _f
    try:
        yield fake
    finally:
        for m, old in saved:
            m.get_bundle = old


# ==========================================================================
# SS1.10 -- ACCESS_CODE gate
# ==========================================================================
@pytest.fixture()
def gated(tmp_path, samples, qa):
    """Keyless app with the quota gate armed and one document indexed."""
    storage = tmp_path / "storage"
    with app_client(storage, env={"ACCESS_CODE": CODE}) as client:
        # the upload route is gated too, so seeding the fixture needs the header
        resp = client.post(
            "/api/documents",
            files=[("files", (TXT[0], TXT[1], "text/plain"))],
            headers={"X-Access-Code": CODE},
        )
        assert resp.status_code == 200, resp.text[:300]
        entry = resp.json()["documents"][0]
        assert entry["status"] == "indexed", entry
        yield type("G", (), {"client": client, "doc_id": entry["id"], "storage": storage})()


def test_gate_is_off_by_default(stack, qa):
    """SS5: ACCESS_CODE defaults to empty and the gate is FULLY disabled."""
    for path in ("/api/health", "/api/documents"):
        assert stack.client.get(path).status_code == 200, path
    assert post_query(stack.client, "anything").status_code == 200


@pytest.mark.parametrize(
    "method,path,kwargs",
    [
        ("get", "/api/documents", {}),
        ("post", "/api/query", {"json": {"question": "revenue"}}),
        ("delete", "/api/documents/{doc}", {}),
        ("get", "/api/documents/{doc}/chunks", {}),
    ],
    ids=["list", "query", "delete", "chunks"],
)
def test_missing_access_code_is_401_on_every_api_route(gated, qa, method, path, kwargs):
    resp = getattr(gated.client, method)(path.format(doc=gated.doc_id), **kwargs)
    err = qa.assert_error_envelope(resp, status=401, code="unauthorized")
    assert err["message"] == "Access code required", (
        f"SS1.10 freezes the missing-header message, got {err['message']!r}"
    )
    assert "retry_after_s" not in err, f"SS1.1: 401 carries no retry_after_s, got {err}"


def test_upload_route_is_gated(gated, qa):
    resp = gated.client.post(
        "/api/documents", files=[("files", ("x.txt", b"body text", "text/plain"))]
    )
    qa.assert_error_envelope(resp, status=401, code="unauthorized")


@pytest.mark.parametrize(
    "wrong",
    ["", "wrong", CODE.upper(), CODE + "x", CODE[:-1], "s3cret-quota-gatE"],
    ids=["empty", "wrong", "case", "longer", "prefix", "lastchar"],
)
def test_wrong_access_code_is_401_invalid(gated, qa, wrong):
    resp = gated.client.get("/api/documents", headers={"X-Access-Code": wrong})
    err = qa.assert_error_envelope(resp, status=401, code="unauthorized")
    assert err["message"] == "Invalid access code", (
        "SS1.10 (ruled r3): an EMPTY `X-Access-Code:` header is PRESENT, not absent -- "
        "the client sent the header, took a turn and got it wrong, so it falls through "
        "to the normal hmac.compare_digest and yields 'Invalid access code'. No "
        "`if not provided` shortcut, no length check, no early return. "
        f"Header={wrong!r} got {err['message']!r}"
    )


def test_correct_access_code_is_200(gated):
    headers = {"X-Access-Code": CODE}
    assert gated.client.get("/api/documents", headers=headers).status_code == 200
    assert gated.client.get(
        f"/api/documents/{gated.doc_id}/chunks", headers=headers
    ).status_code == 200
    assert gated.client.post(
        "/api/query", json={"question": "Vantage content"}, headers=headers
    ).status_code == 200


def test_health_is_the_only_exempt_route(gated):
    """SS1.2 (law): useHealth must work before a code is entered; it is also the probe."""
    resp = gated.client.get("/api/health")
    assert resp.status_code == 200, (
        f"SS1.10 (law): GET /api/health is exempt from the gate, got {resp.status_code}"
    )
    assert resp.json()["status"] == "ok"


def test_options_preflight_is_exempt(gated):
    resp = gated.client.options(
        "/api/query",
        headers={
            "Origin": "http://localhost:3000",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type,x-access-code",
        },
    )
    assert resp.status_code < 400, (
        "SS1.10: OPTIONS preflight is exempt -- CORS must complete before the browser "
        f"sends custom headers. Got {resp.status_code}: {resp.text[:200]}"
    )


def test_access_code_is_never_logged(tmp_path, samples, caplog):
    storage = tmp_path / "storage"
    with caplog.at_level(logging.DEBUG):
        with app_client(storage, env={"ACCESS_CODE": CODE}) as client:
            client.get("/api/documents", headers={"X-Access-Code": CODE})
            client.get("/api/documents", headers={"X-Access-Code": "guessing"})
            client.get("/api/documents")
    blob = "\n".join(r.getMessage() for r in caplog.records)
    assert CODE not in blob, "SS1.10/SS5.2: the access code is never logged, at any level"
    assert "guessing" not in blob, "a rejected header value must not be logged either"


def test_startup_log_reports_the_gate_without_the_value(tmp_path, samples, caplog):
    with caplog.at_level(logging.INFO):
        with app_client(tmp_path / "storage", env={"ACCESS_CODE": CODE}) as client:
            client.get("/api/health")
    blob = "\n".join(r.getMessage() for r in caplog.records)
    assert "access_code=on" in blob, (
        "SS2 main.py: the startup line reports `access_code=on|off` -- never the value. "
        f"Log was:\n{blob[-800:]}"
    )
    assert CODE not in blob


def test_access_code_comparison_is_constant_time(stack, qa):
    """SS1.10: hmac.compare_digest is MANDATORY -- never ==, never a length precheck."""
    import inspect

    main = qa.backend_module("main")
    middleware = qa.require_attr(main, "AccessCodeMiddleware", "SS2 main.py")
    src = inspect.getsource(middleware)
    assert "compare_digest" in src, (
        "SS1.10: the access-code comparison MUST use hmac.compare_digest -- never ==, "
        "never a length precheck, never an early return on first mismatch. "
        f"AccessCodeMiddleware source does not mention it:\n{src[:600]}"
    )


# ==========================================================================
# SS1.10 -- per-IP throttle
# ==========================================================================
@pytest.fixture()
def throttled(tmp_path, samples, qa):
    storage = tmp_path / "storage"
    with app_client(storage, env={"RATE_LIMIT_PER_MIN": "3"}) as client:
        resp = upload_bytes(client, [TXT])
        assert resp.status_code == 200, resp.text[:300]
        entry = resp.json()["documents"][0]
        yield type("T", (), {"client": client, "doc_id": entry["id"], "limit": 3})()


def test_query_throttle_trips_with_a_429_envelope(throttled, qa):
    """The fixture's own upload may have consumed a slot, so find the boundary
    rather than assuming it -- what matters is that the window closes and that
    it closes with the SS1.1 envelope."""
    responses = [post_query(throttled.client, "revenue") for _ in range(throttled.limit + 4)]
    codes = [r.status_code for r in responses]
    assert set(codes) <= {200, 429}, f"unexpected statuses under throttle: {codes}"
    assert 200 in codes, f"the window must allow some requests through: {codes}"
    assert 429 in codes, f"SS1.10: the window must close at RATE_LIMIT_PER_MIN: {codes}"
    first = codes.index(429)
    assert all(c == 429 for c in codes[first:]), (
        f"a closed fixed window stays closed for the rest of the window: {codes}"
    )
    err = qa.assert_error_envelope(responses[first], status=429, code="rate_limited")
    assert err["message"] == THROTTLE_MSG, (
        f"SS1.10 freezes the throttle message, got {err['message']!r}"
    )
    assert isinstance(err.get("retry_after_s"), int), (
        f"SS1.1: retry_after_s is REQUIRED on every 429, got {err}"
    )
    assert 1 <= err["retry_after_s"] <= 60, (
        f"SS1.10: seconds until the window frees a slot (ceil, min 1), got {err['retry_after_s']}"
    )


@pytest.mark.parametrize("path", ["/api/health", "/api/documents"])
def test_get_routes_are_exempt_from_the_throttle(throttled, path):
    """SS1.10 (law): useHealth polls every 10 s -- throttling reads locks out honest users."""
    codes = [throttled.client.get(path).status_code for _ in range(throttled.limit * 4)]
    assert set(codes) == {200}, f"{path} must never be throttled, got {sorted(set(codes))}"


def test_chunks_route_is_exempt_from_the_throttle(throttled):
    path = f"/api/documents/{throttled.doc_id}/chunks"
    codes = [throttled.client.get(path).status_code for _ in range(throttled.limit * 4)]
    assert set(codes) == {200}, (
        f"SS1.8: the chunks endpoint is a UI read path and is throttle-exempt, got {sorted(set(codes))}"
    )


def test_reads_still_work_after_the_write_throttle_trips(throttled, qa):
    for _ in range(throttled.limit + 2):
        post_query(throttled.client, "revenue")
    assert throttled.client.get("/api/health").status_code == 200
    assert throttled.client.get("/api/documents").status_code == 200
    assert throttled.client.get(f"/api/documents/{throttled.doc_id}/chunks").status_code == 200


def test_upload_and_delete_are_throttled(tmp_path, samples, qa):
    with app_client(tmp_path / "storage", env={"RATE_LIMIT_PER_MIN": "2"}) as client:
        codes = [
            upload_bytes(client, [(f"spam{i}.txt", b"content here %d" % i)]).status_code
            for i in range(5)
        ]
        assert 429 in codes, f"SS1.10: POST /api/documents is throttled, got {codes}"
        qa.assert_error_envelope(
            upload_bytes(client, [("spam9.txt", b"more content")]), status=429, code="rate_limited"
        )
        resp = client.delete(f"/api/documents/{uuid.uuid4()}")
    assert resp.status_code == 429, (
        "SS1.10: DELETE /api/documents/{id} is throttled, and the throttle runs BEFORE "
        f"routing, so even an unknown id 429s rather than 404s. Got {resp.status_code}"
    )


def test_shipped_default_throttle_is_ten_per_minute(tmp_path, samples, qa):
    """The general fixtures set RATE_LIMIT_PER_MIN=0 so the eval gate can run; this is
    the one place that exercises the SHIPPED default end to end."""
    with app_client(tmp_path / "storage", env={"RATE_LIMIT_PER_MIN": None}) as client:
        settings = qa.backend_module("config").get_settings()
        assert settings.rate_limit_per_min == 10, settings.rate_limit_per_min
        codes = [post_query(client, "revenue").status_code for _ in range(12)]
    assert codes[:10] == [200] * 10, f"SS5: the first 10 requests/min must pass: {codes}"
    assert codes[10] == 429, f"SS5: the 11th request in the window is throttled: {codes}"


def test_throttle_disabled_when_zero(tmp_path, samples):
    with app_client(tmp_path / "storage", env={"RATE_LIMIT_PER_MIN": "0"}) as client:
        codes = [post_query(client, "revenue").status_code for _ in range(25)]
    assert set(codes) == {200}, f"SS5: RATE_LIMIT_PER_MIN=0 disables the throttle, got {set(codes)}"


def test_spoofed_forwarded_for_does_not_mint_a_fresh_bucket(tmp_path, samples):
    """SS1.10 client identity, AMENDED r3: the v1.2 'first hop of X-Forwarded-For'
    rule was a contract DEFECT and is withdrawn. At the default
    TRUSTED_PROXY_HOPS=0 the header is ignored entirely.

    Full identity matrix (hops 0/1/2, victim targeting, malformed and short-header
    fallbacks, and the bucket keys themselves) lives in tests/test_security_r3.py.
    """
    with app_client(tmp_path / "storage", env={"RATE_LIMIT_PER_MIN": "2"}) as client:
        codes = [
            client.post(
                "/api/query", json={"question": "revenue"},
                headers={"X-Forwarded-For": f"10.0.0.{i}"},
            ).status_code
            for i in range(6)
        ]
    assert 429 in codes, (
        "a rotating spoofed X-Forwarded-For must not make the throttle disappear: "
        f"{codes}"
    )


def test_401_does_not_consume_a_throttle_slot(tmp_path, samples, qa):
    """SS1.10: ordering is body-size -> access gate -> throttle -> routing."""
    env = {"ACCESS_CODE": CODE, "RATE_LIMIT_PER_MIN": "2"}
    with app_client(tmp_path / "storage", env=env) as client:
        for _ in range(8):
            resp = client.post("/api/query", json={"question": "revenue"})
            qa.assert_error_envelope(resp, status=401, code="unauthorized")
        ok = client.post(
            "/api/query", json={"question": "revenue"}, headers={"X-Access-Code": CODE}
        )
    assert ok.status_code == 200, (
        "SS1.10: a 401 must not consume a throttle slot -- after 8 rejected requests the "
        f"first authenticated request must still pass, got {ok.status_code}: {ok.text[:200]}"
    )


def test_throttle_constants_are_frozen(stack, qa):
    config = qa.backend_module("config")
    assert qa.require_attr(config, "RATE_LIMIT_WINDOW_S", "SS2 config.py") == 60
    assert qa.require_attr(config, "RATE_LIMIT_MAX_TRACKED_IPS", "SS2 config.py") == 4096, (
        "SS1.10: the tracked-IP table is bounded at 4096 so a spoofed-XFF flood cannot "
        "exhaust memory"
    )


# ==========================================================================
# SS1.11 / SS1.2 -- daily LLM budget
# ==========================================================================
def test_health_reports_an_llm_budget_object(stack):
    budget = stack.client.get("/api/health").json().get("llm_budget")
    assert isinstance(budget, dict), (
        f"SS1.2: llm_budget is an object and is NEVER null, even in none mode. Got {budget!r}"
    )
    assert set(budget) == {"used", "limit", "remaining", "day"}, sorted(budget)
    assert all(isinstance(budget[k], int) for k in ("used", "limit", "remaining")), budget
    assert budget["remaining"] == max(0, budget["limit"] - budget["used"]), budget
    assert budget["day"] == today_utc(), (
        f"SS1.2: day is the current UTC date, got {budget['day']!r} (today is {today_utc()})"
    )
    assert budget["used"] == 0, (
        f"SS1.2: in none mode no LLM calls are made, so used stays 0. Got {budget['used']}"
    )
    assert budget["limit"] == 200, f"SS5: DAILY_LLM_BUDGET defaults to 200, got {budget['limit']}"


def test_health_budget_limit_follows_the_env(tmp_path, samples):
    with app_client(tmp_path / "storage", env={"DAILY_LLM_BUDGET": "7"}) as client:
        budget = client.get("/api/health").json()["llm_budget"]
    assert budget["limit"] == 7 and budget["remaining"] == 7, budget


def test_health_response_carries_no_key_material(stack):
    body = stack.client.get("/api/health").text
    for leak in ("AIza", "api_key", "GOOGLE_API_KEY"):
        assert leak not in body, f"SS5.2: health must never carry key material ({leak})"


def test_degraded_reason_is_null_in_plain_keyless_mode(indexed_stack):
    body = post_query(indexed_stack.client, "What was Meridian's Q2 FY2026 revenue?").json()
    assert body["mode"] == "extractive"
    assert body["degraded_reason"] is None, (
        "SS1.6: degraded_reason is null in EVERY normal response, including plain "
        f"keyless none mode. Got {body['degraded_reason']!r}"
    )


def test_zero_budget_never_429s_a_keyless_query(tmp_path, samples, qa):
    """SS1.11 / resolution 14: budget exhaustion DEGRADES, it never rate-limits."""
    with app_client(tmp_path / "storage", env={"DAILY_LLM_BUDGET": "0"}) as client:
        qa.index_samples(client, samples)
        resp = post_query(client, "What was Meridian's Q2 FY2026 revenue?")
        assert resp.status_code == 200, (
            f"SS1.11: a spent budget must never produce a 429, got {resp.status_code}: "
            f"{resp.text[:300]}"
        )
        body = resp.json()
        assert body["mode"] == "extractive" and body["model"] is None
        assert body["citations"], "(law) Retrieval never stops -- citations must still be built"
        assert client.get("/api/documents").status_code == 200
        assert upload_bytes(client, [TXT]).status_code == 200, (
            "SS1.11 (law): uploads keep working when generation is suspended"
        )


def test_budget_exhaustion_degrades_to_extractive_with_reason(tmp_path, samples, qa):
    with app_client(tmp_path / "storage", env={"DAILY_LLM_BUDGET": "0"}) as client:
        qa.index_samples(client, samples)
        with force_gemini_at_the_api_layer(qa):
            with qa.patch_backend_attr("complete_with_backoff", _never_call("complete_with_backoff")):
                resp = post_query(client, "What was Meridian's Q2 FY2026 revenue?")
        assert resp.status_code == 200, (
            f"SS1.11: degrade, never 429. Got {resp.status_code}: {resp.text[:300]}"
        )
        body = resp.json()
    assert body["degraded_reason"] == "daily_budget", (
        "SS1.6/SS1.11: a gemini-mode query that fell back because DAILY_LLM_BUDGET was "
        f"exhausted reports degraded_reason:'daily_budget'. Got {body['degraded_reason']!r}"
    )
    assert body["mode"] == "extractive", body["mode"]
    assert body["model"] is None, f"SS1.11: model is null when degraded, got {body['model']!r}"
    assert body["timings"]["llm_ms"] == 0, body["timings"]
    assert body["citations"], "(law) Retrieval never stops"
    assert body["no_answer"] is False


def _never_call(label):
    def _boom(*a, **kw):
        raise AssertionError(
            f"providers.{label} was called after the budget was exhausted -- SS1.11 "
            "suspends generation entirely"
        )

    return _boom


def test_reserve_llm_call_counts_down_and_persists(stack, qa, tmp_path):
    providers = qa.backend_module("providers")
    config = qa.backend_module("config")
    reserve = qa.require_attr(providers, "reserve_llm_call", "SS2 providers.py")
    state = qa.require_attr(providers, "llm_budget_state", "SS2 providers.py")
    path = qa.require_attr(config, "LLM_BUDGET_PATH", "SS2 config.py")
    limit = state()["limit"]
    assert limit == 200
    for i in range(3):
        assert reserve() is True, f"reservation {i} must succeed under a 200 budget"
    assert state()["used"] == 3, state()
    assert state()["remaining"] == limit - 3, state()
    on_disk = json.loads(open(path, encoding="utf-8").read())
    assert on_disk == {"day": today_utc(), "used": 3}, (
        f"SS1.11: the counter persists as {{'day','used'}} at storage/llm_budget.json, got {on_disk}"
    )


def test_reserve_llm_call_returns_false_at_the_ceiling(tmp_path, samples, qa):
    with app_client(tmp_path / "storage", env={"DAILY_LLM_BUDGET": "2"}) as client:
        providers = qa.backend_module("providers")
        reserve = qa.require_attr(providers, "reserve_llm_call", "SS2 providers.py")
        assert [reserve(), reserve()] == [True, True]
        assert reserve() is False, "SS1.11: reserve returns False once the budget is spent"
        assert reserve() is False, "and stays False"
        budget = client.get("/api/health").json()["llm_budget"]
        assert budget["used"] == 2 and budget["remaining"] == 0, budget


def test_zero_budget_reserves_nothing(tmp_path, samples, qa):
    with app_client(tmp_path / "storage", env={"DAILY_LLM_BUDGET": "0"}):
        providers = qa.backend_module("providers")
        reserve = qa.require_attr(providers, "reserve_llm_call", "SS2 providers.py")
        assert reserve() is False, "SS5: DAILY_LLM_BUDGET=0 disables generation entirely"


def test_budget_file_rolls_over_lazily_at_utc_midnight(stack, qa):
    providers = qa.backend_module("providers")
    config = qa.backend_module("config")
    path = qa.require_attr(config, "LLM_BUDGET_PATH", "SS2 config.py")
    state = qa.require_attr(providers, "llm_budget_state", "SS2 providers.py")
    yesterday = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=1)).strftime("%Y-%m-%d")
    open(path, "w", encoding="utf-8").write(json.dumps({"day": yesterday, "used": 199}))
    got = state()
    assert got["day"] == today_utc() and got["used"] == 0, (
        f"SS1.11: day rollover is lazy -- a counter from another day resets to today/0. Got {got}"
    )


@pytest.mark.parametrize(
    "content", ["", "not json at all", '{"day": 5}', '{"used": "many"}', "[]"],
    ids=["empty", "garbage", "badday", "badused", "wrongtype"],
)
def test_unparseable_budget_file_is_recreated_not_corruption(stack, qa, content):
    """SS1.11 + SS3.4(2): the budget counter is DERIVED data, never a corruption condition."""
    providers = qa.backend_module("providers")
    config = qa.backend_module("config")
    path = qa.require_attr(config, "LLM_BUDGET_PATH", "SS2 config.py")
    state = qa.require_attr(providers, "llm_budget_state", "SS2 providers.py")
    open(path, "w", encoding="utf-8").write(content)
    got = state()
    assert got["day"] == today_utc() and got["used"] == 0, got
    assert stack.client.get("/api/health").status_code == 200


def test_unwritable_budget_file_favours_availability(stack, qa, tmp_path):
    """SS2 providers.py: an unwritable budget file logs once and returns True."""
    providers = qa.backend_module("providers")
    reserve = qa.require_attr(providers, "reserve_llm_call", "SS2 providers.py")
    config = qa.backend_module("config")
    with qa.patch_backend_attr("LLM_BUDGET_PATH", tmp_path / "nope" / "deeper" / "b.json"):
        assert reserve() is True, (
            "SS2: 'an unwritable budget file logs once and returns True (availability "
            "over accounting)' -- it must never raise for budget reasons"
        )
    assert config is not None


def test_no_answer_refusal_reserves_nothing(tmp_path, samples, qa):
    with app_client(tmp_path / "storage", env={"DAILY_LLM_BUDGET": "5"}) as client:
        qa.index_samples(client, samples)
        body = post_query(client, "What was Contoso Manufacturing's FY2031 dividend policy?").json()
        assert body["no_answer"] is True, body["answer"]
        budget = client.get("/api/health").json()["llm_budget"]
    assert budget["used"] == 0, (
        f"SS1.11: 'a no_answer refusal and an extractive answer reserve nothing'. Got {budget}"
    )


# ==========================================================================
# SS5 -- AUTO_SEED
# ==========================================================================
def test_auto_seed_populates_an_empty_manifest(tmp_path, samples):
    with app_client(tmp_path / "storage", env={"AUTO_SEED": "on"}) as client:
        listed = client.get("/api/documents").json()
    assert listed["totals"]["documents"] > 0, (
        "SS5/SS2: with AUTO_SEED=on and an empty manifest, startup ingests "
        f"backend/sample_data/ through the normal ingest path. Got {listed['totals']}"
    )
    names = {d["name"] for d in listed["documents"]}
    assert names & set(SAMPLE_FILENAMES.values()), f"seeded names look wrong: {sorted(names)}"
    assert all(d["chunks"] >= 1 for d in listed["documents"]), listed


def test_auto_seed_off_leaves_the_corpus_empty(tmp_path, samples):
    with app_client(tmp_path / "storage", env={"AUTO_SEED": "off"}) as client:
        listed = client.get("/api/documents").json()
    assert listed["totals"]["documents"] == 0, (
        f"SS5: AUTO_SEED=off must not seed anything, got {listed['totals']}"
    )


def test_auto_seed_does_not_re_seed_a_non_empty_manifest(tmp_path, samples):
    storage = tmp_path / "storage"
    with app_client(storage, env={"AUTO_SEED": "off"}) as client:
        assert upload_bytes(client, [TXT]).status_code == 200
        first = client.get("/api/documents").json()
    assert first["totals"]["documents"] == 1
    with app_client(storage, env={"AUTO_SEED": "on"}) as client:
        second = client.get("/api/documents").json()
    assert second["totals"] == first["totals"], (
        "SS5/SS2: seeding runs ONLY when the manifest is empty -- a restart against a "
        f"populated store must change nothing. {first['totals']} -> {second['totals']}"
    )
    assert [d["id"] for d in second["documents"]] == [d["id"] for d in first["documents"]]


def test_seeded_corpus_is_immediately_queryable_keyless(tmp_path, samples):
    with app_client(tmp_path / "storage", env={"AUTO_SEED": "on"}) as client:
        body = post_query(client, "What was Meridian's Q2 FY2026 revenue?").json()
    assert body["no_answer"] is False, (
        f"SS2: auto-seeding is keyless-safe -- it indexes without vectors and BM25 "
        f"still answers. Got: {body['answer']!r}"
    )
    assert body["citations"], body


def test_seed_sample_data_exists_and_reports_what_it_did(tmp_path, samples, qa, caplog):
    """SS2: seeding logs its result and must never block or fail startup."""
    import inspect

    with caplog.at_level(logging.INFO):
        with app_client(tmp_path / "storage", env={"AUTO_SEED": "on"}) as client:
            ingest = qa.backend_module("ingest")
            seed = qa.require_attr(ingest, "seed_sample_data", "SS2 ingest.py")
            assert inspect.iscoroutinefunction(seed), (
                "SS2: `async def seed_sample_data() -> int`"
            )
            assert client.get("/api/health").status_code == 200, (
                "SS2: seeding must never block or fail startup"
            )
    blob = "\n".join(r.getMessage() for r in caplog.records)
    assert "auto-seeded" in blob.lower(), (
        "SS2: seeding logs `auto-seeded N documents from sample_data` (or the reason it "
        f"seeded nothing). Log tail:\n{blob[-800:]}"
    )


def test_sample_data_dir_constant_points_at_the_read_only_corpus(stack, qa):
    config = qa.backend_module("config")
    from pathlib import Path

    got = Path(str(qa.require_attr(config, "SAMPLE_DATA_DIR", "SS2 config.py"))).resolve()
    assert got == (BACKEND_DIR / "sample_data").resolve(), (
        f"SS2: SAMPLE_DATA_DIR = BACKEND_DIR/'sample_data' (read-only seed corpus), got {got}"
    )


# ==========================================================================
# SS5 -- CORS_ORIGINS
# ==========================================================================
def test_default_cors_origin_is_localhost_3000(stack):
    resp = stack.client.get("/api/health", headers={"Origin": "http://localhost:3000"})
    assert resp.headers.get("access-control-allow-origin") == "http://localhost:3000", (
        f"SS5: the default origin list is ['http://localhost:3000']. Headers: {dict(resp.headers)}"
    )


def test_cors_origins_are_read_from_the_env(tmp_path, samples):
    origin = "https://alpha-detective.vercel.app"
    with app_client(tmp_path / "storage", env={"CORS_ORIGINS": f"{origin} , "}) as client:
        allowed = client.get("/api/health", headers={"Origin": origin})
        denied = client.get("/api/health", headers={"Origin": "https://evil.example"})
    assert allowed.headers.get("access-control-allow-origin") == origin, (
        f"SS5: CORS_ORIGINS is split on ',', trimmed, empties dropped. Got "
        f"{allowed.headers.get('access-control-allow-origin')!r}"
    )
    assert denied.headers.get("access-control-allow-origin") != "https://evil.example"


def test_preflight_allows_the_access_code_header_and_the_three_methods(stack):
    resp = stack.client.options(
        "/api/documents",
        headers={
            "Origin": "http://localhost:3000",
            "Access-Control-Request-Method": "DELETE",
            "Access-Control-Request-Headers": "x-access-code",
        },
    )
    assert resp.status_code < 400, resp.text[:200]
    allow_headers = (resp.headers.get("access-control-allow-headers") or "").lower()
    assert "x-access-code" in allow_headers or allow_headers.strip() == "*", (
        f"SS5: allow_headers must include X-Access-Code, got {allow_headers!r}"
    )
    allow_methods = (resp.headers.get("access-control-allow-methods") or "").upper()
    for method in ("GET", "POST", "DELETE"):
        assert method in allow_methods or allow_methods.strip() == "*", (
            f"SS5: methods stay GET, POST, DELETE, OPTIONS. Got {allow_methods!r}"
        )
    assert resp.headers.get("access-control-allow-credentials") != "true", (
        "SS5: credentials stay OFF"
    )


def test_error_responses_still_carry_cors_headers(tmp_path, samples, qa):
    """SS2 main.py: CORSMiddleware is OUTERMOST so every refusal is readable by the browser."""
    with app_client(tmp_path / "storage", env={"ACCESS_CODE": CODE}) as client:
        resp = client.get("/api/documents", headers={"Origin": "http://localhost:3000"})
    qa.assert_error_envelope(resp, status=401, code="unauthorized")
    assert resp.headers.get("access-control-allow-origin") == "http://localhost:3000", (
        f"a 401 must still carry CORS headers, got {dict(resp.headers)}"
    )


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("", ["http://localhost:3000"]),
        ("   ", ["http://localhost:3000"]),
        (",,", ["http://localhost:3000"]),
        ("https://a.example", ["https://a.example"]),
        (" https://a.example , https://b.example ", ["https://a.example", "https://b.example"]),
        ("https://a.example,,", ["https://a.example"]),
        ("*", ["*"]),
    ],
    ids=["empty", "blank", "commas", "one", "two", "trailing", "wildcard"],
)
def test_parse_cors_origins_unit(stack, qa, raw, expected):
    config = qa.backend_module("config")
    parse = qa.require_attr(config, "parse_cors_origins", "SS2 config.py")
    assert parse(raw) == expected, f"parse_cors_origins({raw!r}) -> {parse(raw)!r}"


# ==========================================================================
# SS5 -- MAX_DOCUMENTS corpus cap
# ==========================================================================
def test_corpus_cap_fails_per_file_with_the_frozen_string(tmp_path, samples, qa):
    with app_client(tmp_path / "storage", env={"MAX_DOCUMENTS": "2"}) as client:
        first = upload_bytes(client, [("a.txt", b"alpha content one"), ("b.txt", b"beta content two")])
        assert [e["status"] for e in first.json()["documents"]] == ["indexed", "indexed"]
        third = upload_bytes(client, [("c.txt", b"gamma content three")])
        assert third.status_code == 200, (
            f"SS5: the corpus cap fails PER FILE, HTTP stays 200. Got {third.status_code}"
        )
        entry = third.json()["documents"][0]
        assert entry["status"] == "failed"
        assert entry["error"] == qa.ERR.corpus_full.format(cap=2), (
            f"SS1.3 frozen string:\n  expected: {qa.ERR.corpus_full.format(cap=2)!r}\n"
            f"  actual:   {entry['error']!r}"
        )
        listed = client.get("/api/documents").json()
    assert listed["totals"]["documents"] == 2, listed["totals"]


def test_duplicates_do_not_count_against_the_corpus_cap(tmp_path, samples, qa):
    with app_client(tmp_path / "storage", env={"MAX_DOCUMENTS": "1"}) as client:
        assert upload_bytes(client, [TXT]).json()["documents"][0]["status"] == "indexed"
        again = upload_bytes(client, [("copy.txt", TXT[1])]).json()["documents"][0]
    assert again["status"] == "duplicate", (
        "SS1.3: 'duplicates do not count against the cap because they add nothing' -- "
        f"got {again}"
    )
    assert "error" not in again, again


# ==========================================================================
# SS5 -- the twelve-variable matrix and its code constants
# ==========================================================================
@pytest.mark.parametrize(
    "field,default",
    [
        ("port", 8000),
        ("cors_origins", "http://localhost:3000"),
        ("access_code", ""),
        ("daily_llm_budget", 200),
        ("rate_limit_per_min", 10),
        ("max_documents", 50),
        ("auto_seed", "on"),
    ],
)
def test_new_settings_fields_exist_with_contract_defaults(tmp_path, samples, qa, field, default):
    """Built with the v1.2 vars UNSET so the shipped defaults are what is measured
    (the shared fixtures pin AUTO_SEED/RATE_LIMIT_PER_MIN for suite determinism)."""
    unset = {
        "PORT": None, "CORS_ORIGINS": None, "ACCESS_CODE": None, "DAILY_LLM_BUDGET": None,
        "RATE_LIMIT_PER_MIN": None, "MAX_DOCUMENTS": None, "AUTO_SEED": None,
        "TRUSTED_PROXY_HOPS": None,
    }
    with app_client(tmp_path / "storage", env=unset):
        config = qa.backend_module("config")
        # Unsetting the env vars is only half of it: pydantic-settings would then
        # fall through to the DEVELOPER'S backend/.env, so this would measure their
        # machine instead of the shipped defaults. Detach the env file too.
        config.Settings.model_config["env_file"] = None
        config.get_settings.cache_clear()
        settings = config.get_settings()
        assert hasattr(settings, field), (
            f"SS5: Settings.{field} is required by the v1.2 env matrix"
        )
        got = getattr(settings, field)
    assert got == default, f"SS5: {field} defaults to {default!r}, got {got!r}"


@pytest.mark.parametrize(
    "name,value",
    [("DEFAULT_PORT", 8000), ("LOCAL_HOST", "127.0.0.1"), ("DEPLOY_HOST", "0.0.0.0")],
)
def test_bind_constants_are_frozen(stack, qa, name, value):
    config = qa.backend_module("config")
    assert qa.require_attr(config, name, "SS2 config.py") == value


def test_malformed_int_env_falls_back_to_the_default(tmp_path, samples, qa):
    """SS5 (r3): malformed ints fall back to the field default with one warning."""
    env = {"DAILY_LLM_BUDGET": "not-a-number", "RATE_LIMIT_PER_MIN": "  # comment only"}
    with app_client(tmp_path / "storage", env=env) as client:
        budget = client.get("/api/health").json()["llm_budget"]
        settings = qa.backend_module("config").get_settings()
    assert budget["limit"] == 200, f"garbage DAILY_LLM_BUDGET must fall back to 200, got {budget}"
    assert settings.rate_limit_per_min == 10, settings.rate_limit_per_min


# ==========================================================================
# SS1.9.1 rule 4 -- explain must not interact with the budget
# ==========================================================================
def test_explain_does_not_consume_budget(tmp_path, samples, qa):
    with app_client(tmp_path / "storage", env={"DAILY_LLM_BUDGET": "5"}) as client:
        qa.index_samples(client, samples)
        before = client.get("/api/health").json()["llm_budget"]["used"]
        for _ in range(4):
            assert post_query(client, "What was Meridian's Q2 FY2026 revenue?", explain=True
                              ).status_code == 200
        after = client.get("/api/health").json()["llm_budget"]["used"]
    assert after == before, (
        "SS1.9.1 rule 4: explain must not consume DAILY_LLM_BUDGET differently from a "
        f"normal query -- the budget is charged by the LLM call, which explain never adds. "
        f"used {before} -> {after}"
    )


def test_budget_degraded_response_still_serves_the_inspector(tmp_path, samples, qa):
    """SS1.11 (law) 'Retrieval never stops' -- the inspector is retrieval, not generation."""
    with app_client(tmp_path / "storage", env={"DAILY_LLM_BUDGET": "0"}) as client:
        qa.index_samples(client, samples)
        with force_gemini_at_the_api_layer(qa):
            body = post_query(
                client, "What was Meridian's Q2 FY2026 revenue?", explain=True
            ).json()
    assert body["degraded_reason"] == "daily_budget", body["degraded_reason"]
    assert "pipeline" in body, "explain:true must still return a pipeline when degraded"
    stages = [s["stage"] for s in body["pipeline"]["stages"]]
    assert "bm25" in stages and "fusion" in stages and "guardrail" in stages, stages
    assert body["citations"], "(law) Retrieval never stops"
    # `mode` here reports the retrieval layer's real provider: this test fakes gemini
    # only at the api boundary, so `none` is the honest answer and proves the
    # inspector reports what the pipeline ACTUALLY ran, not what api.py believed.
    assert body["pipeline"]["mode"] in ("gemini", "none"), body["pipeline"]["mode"]


def test_only_two_401_messages_exist_and_neither_is_informative(gated, qa):
    """SS1.10 (law, ruled r3): the two messages stay DISTINGUISHABLE BUT UNINFORMATIVE.

    They may reveal only whether the header was sent -- never whether a code is
    configured, how long it is, how close a guess was, or anything about
    GOOGLE_API_KEY. A future 'helpful' error string is exactly how this regresses.
    """
    probes = {
        "absent": None,
        "empty": "",
        "space": " ",
        "wrong": "nope",
        "prefix": CODE[:-1],
        "suffix": CODE[1:],
        "longer": CODE + "x",
        "case": CODE.upper(),
        "same-length": "x" * len(CODE),
    }
    messages = {}
    for label, header in probes.items():
        headers = {} if header is None else {"X-Access-Code": header}
        err = qa.assert_error_envelope(
            gated.client.get("/api/documents", headers=headers), status=401, code="unauthorized"
        )
        messages[label] = err["message"]
    distinct = set(messages.values())
    assert distinct <= {"Access code required", "Invalid access code"}, (
        f"SS1.10 (law): no third message may ever exist. Got {sorted(distinct)}"
    )
    assert messages["absent"] == "Access code required", messages
    assert {messages[k] for k in probes if k != "absent"} == {"Invalid access code"}, (
        "SS1.10 (ruled r3): every SENT header -- empty, whitespace, wrong length, right "
        f"length, near-miss -- yields the identical message. Got {messages}"
    )
    blob = " ".join(messages.values())
    for leak in (CODE, str(len(CODE)), "length", "character", "expected", "configured", "close"):
        assert leak.lower() not in blob.lower(), (
            f"SS1.10 (law): the 401 message leaks {leak!r}: {blob!r}"
        )

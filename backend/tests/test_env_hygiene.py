"""Environment-parsing hygiene and PROVIDER=auto fallback (ratified r3).

These pin the regression that a green 101-case suite missed entirely: every
suite forced PROVIDER=none, so `effective_provider` short-circuited before
GOOGLE_API_KEY was ever read and the whole gemini startup path -- the shipped
default -- had zero coverage. A `.env` copied verbatim from `.env.example`
loaded the trailing comment as the key, which reached an HTTP auth header and
killed the process with a misleading "check the API key and network" error.

Covered here:
  (a) the shipped .env.example, loaded verbatim, boots keyless
  (b) implausible keys are dropped, and the warning leaks no key material
  (c) PROVIDER=auto survives any provider failure (retrieval-only, no exit)
  (d) explicit PROVIDER=gemini still fails loudly
  (e) .env.example stays ASCII with no inline comments after `=`
"""

import asyncio
import contextlib
import logging
import sys

import pytest
from fastapi.testclient import TestClient

from conftest import (
    BACKEND_DIR,
    HERMETIC_ENV,
    _set_env,
    load_backend,
    purge_backend_modules,
)

ENV_EXAMPLE = BACKEND_DIR / ".env.example"

# Shaped like a real Google key but inert: 39 chars, no whitespace, pure ASCII.
# It must pass config sanitization so the failure under test comes from the
# provider call, not from the key being rejected first.
FAKE_KEY = "AIzaSy" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q"

IMPLAUSIBLE_KEYS = {
    "comment_only": "# free key: https://aistudio.google.com/apikey -- empty = mode",
    "em_dash": "abc — def",
    "internal_space": "a b",
    "hash_inside": "abc#def",
    "non_printable": "abc\x01def",
}


@contextlib.contextmanager
def prepared_app(storage_dir, env=None, env_file=None, break_provider=False):
    """Build an app with full control over .env, env vars and provider health.

    Yields `(main, app)` *before* the lifespan runs, so callers choose how to
    start it. `env_file` repoints Settings at another file instead of writing to
    backend/.env -- the developer's real .env is never touched. `break_provider`
    makes the model lister raise, exercising the seam at providers.py.
    """
    # Same hermetic baseline as every other app build (conftest.HERMETIC_ENV):
    # nothing ambient may reconfigure the app. PROVIDER is unpinned here on
    # purpose -- these tests are ABOUT provider resolution.
    merged = dict(HERMETIC_ENV)
    merged["PROVIDER"] = None
    merged.update(env or {})
    with _set_env(merged):
        main = load_backend(storage_dir)
        config = sys.modules["backend.app.config"]
        config.Settings.model_config["env_file"] = (
            str(env_file) if env_file is not None else None
        )
        config.get_settings.cache_clear()

        if break_provider:
            providers = sys.modules["backend.app.providers"]

            def _boom(api_key):
                raise RuntimeError("simulated: cannot reach the Gemini model list")

            providers._make_genai_client = _boom

        try:
            yield main, main.create_app()
        finally:
            purge_backend_modules()


@contextlib.contextmanager
def booted_app(storage_dir, **kwargs):
    """A fully started app, served through TestClient (lifespan on __enter__)."""
    with prepared_app(storage_dir, **kwargs) as (_main, app):
        with TestClient(app) as client:
            yield client


def assert_startup_exits(storage_dir, **kwargs):
    """Assert the lifespan raises SystemExit(1); return nothing.

    The lifespan is driven directly rather than through TestClient: Starlette
    runs it inside an anyio task group, which repackages SystemExit as a
    CancelledError and would hide the exit code under test.
    """
    with prepared_app(storage_dir, **kwargs) as (main, app):

        async def _run():
            async with main.lifespan(app):
                pass

        with pytest.raises(SystemExit) as exc:
            asyncio.run(_run())
        assert exc.value.code == 1, f"expected exit 1, got {exc.value.code!r}"


def build_settings(monkeypatch, key):
    """Settings() with only GOOGLE_API_KEY set; the .env file is bypassed."""
    from backend.app import config

    monkeypatch.setenv("GOOGLE_API_KEY", key)
    return config.Settings(_env_file=None)


# --------------------------------------------------------------------------
# (a) the shipped template must boot, forever
# --------------------------------------------------------------------------
def test_shipped_env_example_boots_keyless(tmp_path, samples):
    """A .env copied verbatim from .env.example yields no key and boots keyless.

    This is the exact operator action from the README, and the exact shape that
    used to kill startup. It pins .env.example itself against regression.
    """
    env_file = tmp_path / "copied.env"
    env_file.write_text(ENV_EXAMPLE.read_text(encoding="utf-8"), encoding="utf-8")

    with booted_app(tmp_path / "storage", env_file=env_file) as client:
        health = client.get("/api/health").json()
        assert health["status"] == "ok"
        assert health["provider"] == "none", (
            "the shipped .env.example must resolve to retrieval-only, not a "
            "half-configured gemini mode"
        )
        assert health["llm_model"] is None and health["embed_model"] is None


def test_shipped_env_example_yields_unset_key(tmp_path):
    """Settings built from the template expose an empty key and provider none."""
    from backend.app import config

    env_file = tmp_path / "copied.env"
    env_file.write_text(ENV_EXAMPLE.read_text(encoding="utf-8"), encoding="utf-8")
    settings = config.Settings(_env_file=str(env_file))

    assert settings.google_api_key == ""
    assert settings.provider == "auto"
    assert settings.effective_provider == "none"


# --------------------------------------------------------------------------
# (b) implausible keys are dropped, and nothing leaks
# --------------------------------------------------------------------------
@pytest.mark.parametrize("label", sorted(IMPLAUSIBLE_KEYS))
def test_implausible_key_treated_as_unset(monkeypatch, caplog, label):
    key = IMPLAUSIBLE_KEYS[label]
    with caplog.at_level(logging.WARNING, logger="alpha.config"):
        settings = build_settings(monkeypatch, key)

    assert settings.google_api_key == "", f"{label} must be treated as UNSET"
    assert settings.effective_provider == "none"

    logged = caplog.text
    assert logged.strip(), f"{label} must produce a warning, not silent ignore"

    # No key material, in whole or in part.
    assert key not in logged
    for chunk in (key[:8], key[-8:]):
        if len(chunk.strip()) >= 4:
            assert chunk not in logged, f"warning leaked a slice of the {label} key"


@pytest.mark.parametrize(
    "key,expected",
    [
        (FAKE_KEY, FAKE_KEY),                       # clean key survives
        (f"{FAKE_KEY}  # my key", FAKE_KEY),        # trailing comment stripped
        ("   " + FAKE_KEY + "  ", FAKE_KEY),        # surrounding whitespace trimmed
    ],
)
def test_plausible_key_survives_sanitization(monkeypatch, key, expected):
    settings = build_settings(monkeypatch, key)
    assert settings.google_api_key == expected
    assert settings.effective_provider == "gemini"


def test_empty_key_is_silent(monkeypatch, caplog):
    """An intentionally empty key is normal operation, not a warning."""
    with caplog.at_level(logging.WARNING, logger="alpha.config"):
        settings = build_settings(monkeypatch, "")
    assert settings.google_api_key == ""
    assert "malformed" not in caplog.text


@pytest.mark.parametrize(
    "raw,expected",
    [("   # note", "auto"), ("none  # retrieval only", "none"), ("gemini", "gemini")],
)
def test_commented_enum_never_breaks_validation(monkeypatch, raw, expected):
    """A commented PROVIDER line falls back to the default instead of raising."""
    from backend.app import config

    monkeypatch.setenv("PROVIDER", raw)
    assert config.Settings(_env_file=None).provider == expected


# --------------------------------------------------------------------------
# (c) + (d) auto degrades, explicit gemini fails loudly
# --------------------------------------------------------------------------
def test_auto_falls_back_to_none_when_provider_fails(tmp_path, samples, caplog):
    """PROVIDER=auto + a real key + a dead provider must boot retrieval-only."""
    with caplog.at_level(logging.WARNING):
        with booted_app(
            tmp_path / "storage",
            env={"GOOGLE_API_KEY": FAKE_KEY, "PROVIDER": "auto"},
            break_provider=True,
        ) as client:
            health = client.get("/api/health").json()

    assert health["status"] == "ok"
    assert health["provider"] == "none", "auto must degrade, not stay half-broken"
    assert health["llm_model"] is None
    assert "retrieval-only" in caplog.text, "the cause must be logged once"
    assert FAKE_KEY not in caplog.text, "the fallback warning leaked the key"


def test_auto_still_serves_queries_after_fallback(tmp_path, samples, qa):
    """Degraded mode is a working app: upload + query still answer extractively."""
    from conftest import index_samples, post_query

    with booted_app(
        tmp_path / "storage",
        env={"GOOGLE_API_KEY": FAKE_KEY, "PROVIDER": "auto"},
        break_provider=True,
    ) as client:
        index_samples(client, samples)
        resp = post_query(client, "What was Meridian's revenue in Q2 FY2026?")
        assert resp.status_code == 200
        body = resp.json()
        assert body["mode"] == "extractive"
        assert body["no_answer"] is False
        assert body["citations"], "a degraded answer must still cite sources"


def test_explicit_gemini_still_fails_loudly(tmp_path, samples):
    """PROVIDER=gemini is a promise; a dead provider must still exit 1."""
    assert_startup_exits(
        tmp_path / "storage",
        env={"GOOGLE_API_KEY": FAKE_KEY, "PROVIDER": "gemini"},
        break_provider=True,
    )


def test_explicit_gemini_without_key_still_exits(tmp_path, samples):
    """The pre-existing empty-key guard is unchanged by the r3 fallback."""
    assert_startup_exits(
        tmp_path / "storage", env={"GOOGLE_API_KEY": "", "PROVIDER": "gemini"}
    )


# --------------------------------------------------------------------------
# PROVIDER=auto parity with none (conftest matrix coverage)
# --------------------------------------------------------------------------
def test_auto_keyless_matches_none_mode(auto_stack, samples, qa):
    """Keyless PROVIDER=auto behaves exactly like PROVIDER=none."""
    from conftest import index_samples, post_query

    health = auto_stack.client.get("/api/health").json()
    assert health["provider"] == "none"

    index_samples(auto_stack.client, samples)
    resp = post_query(auto_stack.client, "What was Meridian's revenue in Q2 FY2026?")
    assert resp.status_code == 200
    assert resp.json()["mode"] == "extractive"


# --------------------------------------------------------------------------
# (e) the template file itself
# --------------------------------------------------------------------------
def test_env_example_is_pure_ascii():
    raw = ENV_EXAMPLE.read_bytes()
    assert raw.isascii(), (
        ".env.example must be pure ASCII: a non-ASCII character in a value "
        "reaches an HTTP header and raises UnicodeEncodeError inside the transport"
    )


def test_env_example_has_no_inline_comments():
    """Every comment on its own line -- the rule that makes the template safe."""
    offenders = []
    for lineno, line in enumerate(
        ENV_EXAMPLE.read_text(encoding="utf-8").splitlines(), start=1
    ):
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        if "#" in stripped.split("=", 1)[1]:
            offenders.append(f"line {lineno}: {line}")
    assert not offenders, (
        "inline comments after `=` are parsed as the value by python-dotenv "
        "when the value is empty:\n" + "\n".join(offenders)
    )


V12_ENV_VARS = (
    "GOOGLE_API_KEY",
    "PROVIDER",
    "GEMINI_LLM_MODEL",
    "GEMINI_EMBED_MODEL",
    "RERANK",
    "PORT",
    "CORS_ORIGINS",
    "ACCESS_CODE",
    "DAILY_LLM_BUDGET",
    "RATE_LIMIT_PER_MIN",
    "MAX_DOCUMENTS",
    "AUTO_SEED",
    "TRUSTED_PROXY_HOPS",  # 13th var, ratified r3 (SS1.10 client identity)
)


@pytest.mark.parametrize("var", V12_ENV_VARS)
def test_env_example_declares_all_thirteen_vars(var):
    """CONTRACTS.md SS5: '.env.example must document all twelve vars ...
    tests/test_env_hygiene.py pins this.'"""
    text = ENV_EXAMPLE.read_text(encoding="utf-8")
    assert f"\n{var}=" in f"\n{text}", (
        f"{var} missing from .env.example -- SS5 grew the matrix from five vars to twelve"
    )


def test_env_example_declares_no_undocumented_vars():
    text = ENV_EXAMPLE.read_text(encoding="utf-8")
    declared = {
        line.split("=", 1)[0].strip()
        for line in text.splitlines()
        if "=" in line and not line.strip().startswith("#")
    }
    extra = declared - set(V12_ENV_VARS)
    assert not extra, (
        "SS5: 'no future variable may be added without an architect ratification'. "
        f"Undocumented vars in .env.example: {sorted(extra)}"
    )


@pytest.mark.parametrize("var", ["GOOGLE_API_KEY", "ACCESS_CODE"])
def test_env_example_ships_secrets_empty(var):
    """SS5: the template must ship ACCESS_CODE / GOOGLE_API_KEY EMPTY."""
    for line in ENV_EXAMPLE.read_text(encoding="utf-8").splitlines():
        if line.strip().startswith(f"{var}="):
            assert line.strip() == f"{var}=", (
                f"SS5: {var} must ship empty in .env.example, got {line.strip()!r}"
            )
            return
    raise AssertionError(f"{var} not declared in .env.example")


# --------------------------------------------------------------------------
# (f) harness hermeticity -- the run must mean the same thing on every machine
# --------------------------------------------------------------------------
def test_hermetic_env_pins_every_contract_variable():
    """Anything left unpinned is read from the developer's backend/.env or an
    inherited process env, and silently reconfigures the app under test."""
    missing = [v for v in V12_ENV_VARS if v not in HERMETIC_ENV]
    assert not missing, (
        f"SS5 variables not pinned by the harness: {missing}. Every one of them can "
        "reconfigure the app; an unpinned ACCESS_CODE alone turned this suite into "
        "132 failures of `401 unauthorized` that looked like product bugs."
    )
    assert HERMETIC_ENV["ACCESS_CODE"] == "", "the gate must be off for the general fixtures"
    assert HERMETIC_ENV["GOOGLE_API_KEY"] is None, "keyless by construction"


@pytest.mark.parametrize(
    "var,value",
    [
        ("ACCESS_CODE", "ambient-code-1234"),
        ("AUTO_SEED", "on"),
        ("MAX_DOCUMENTS", "1"),
        ("RATE_LIMIT_PER_MIN", "1"),
        ("DAILY_LLM_BUDGET", "0"),
        ("PROVIDER", "gemini"),
    ],
)
def test_ambient_environment_cannot_reconfigure_the_app_under_test(tmp_path, samples, qa, var, value):
    """A hostile/ambient value in the process environment -- exactly what a `.env`
    edited for a screenshot, or an inherited shell export, looks like to
    pydantic-settings -- must not reach the app the harness builds."""
    from conftest import _set_env, app_client, upload_bytes

    with _set_env({var: value}):
        with app_client(tmp_path / "storage") as client:
            health = client.get("/api/health").json()
            listed = client.get("/api/documents")
            up = upload_bytes(client, [("a.txt", b"ambient isolation probe body")])
            settings = qa.backend_module("config").get_settings()

    assert listed.status_code == 200, (
        f"ambient {var}={value!r} reached the app: GET /api/documents returned "
        f"{listed.status_code} ({listed.text[:120]})"
    )
    assert up.status_code == 200 and up.json()["documents"][0]["status"] == "indexed", (
        f"ambient {var}={value!r} broke uploads: {up.status_code} {up.text[:160]}"
    )
    assert health["provider"] == "none", f"ambient {var} changed the provider: {health}"
    assert settings.access_code == "", f"ambient {var} leaked ACCESS_CODE: {settings.access_code!r}"
    assert settings.auto_seed == "off" and settings.rate_limit_per_min == 0, settings

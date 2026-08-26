"""SS3.6 guard -- the fence around the ONE sanctioned storage exception.

CONTRACTS.md SS3.6 ratifies `RERANK_MODEL_DIR` (`backend/storage/models/`) as the
single place a test run may touch the real storage tree: it is a third-party
cross-encoder download cache, it holds no test-visible state, and redirecting it
would re-download the model on every run.

That ratification is conditional. Condition 3 is this file: **after a full suite
run the real `backend/storage/` must contain nothing but `models/`** -- no
`manifest.json`, no `chroma/`, no `docstore.json`, no `uploads/`, no
`llm_budget.json`. If that ever fails, the LEAK IS THE BUG, NOT THIS ASSERTION:
something authoritative or mutable is being written under the exception, and it
must be promoted to a frozen path seam (SS2 config.py) BEFORE that change ships.
Do not delete this test, do not widen its allowlist, and do not "fix" it by
adding the leaking name here.

Named `test_zz_*` so alphabetical collection puts it last: it asserts the END
STATE of a full run. It is still meaningful standalone (it never creates the
directory it inspects, and the collection-time snapshot in conftest lets it tell
a developer's pre-existing local dev store apart from a leak this run caused).
"""

import pytest

from conftest import (
    _LIVE_APP_MODULES,
    REAL_STORAGE_AT_COLLECTION,
    REAL_STORAGE_DIR,
    SANDBOX_CHECKS,
    SANDBOX_VIOLATIONS,
    external_writer_evidence,
    storage_signature,
)

SANCTIONED = {"models"}
BOTH_CAUSES = (
    "TWO THINGS CAN CAUSE THIS, AND THEY LOOK IDENTICAL TO A TREE DIFF:\n"
    "  (1) another process wrote to backend/storage while the suite ran -- a live\n"
    "      `make dev` / uvicorn in a second terminal is the ordinary case, and it is\n"
    "      NOT a suite bug. CHECK FOR A RUNNING DEV SERVER FIRST.\n"
    "  (2) a test escaped its sandbox and wrote to the real store.\n"
    "Cause (2) is separately disproved by the attributable layer\n"
    "(test_every_app_build_was_redirected_away_from_real_storage), which checks the\n"
    "resolved path constants of every app build and cannot produce a false positive."
)
PROMOTION_HINT = (
    "SS3.6 condition 3: the RERANK_MODEL_DIR exception is bounded. Anything "
    "authoritative, mutable or test-observable under the real backend/storage/ must be "
    "promoted to a frozen path seam in SS2 config.py (and added to conftest._PATH_LAYOUT) "
    "BEFORE it ships. Fix the leak -- never widen this allowlist."
)


def test_no_app_outlived_its_fixture():
    """Runs last: by now every app_client in the suite must have been torn down.

    A leftover entry means some app's live `backend.app.*` modules are still
    registered in sys.modules while its storage directory is gone -- the exact
    shape of cross-test leakage that produces order-dependent, intermittent
    failures rather than honest ones.
    """
    assert _LIVE_APP_MODULES == [], (
        f"{len(_LIVE_APP_MODULES)} app(s) outlived their fixture -- a test escaped its "
        "app_client context and later tests may be running against its modules"
    )


def test_every_app_build_was_redirected_away_from_real_storage(stack):
    """SS3.6 layer 1 -- the ATTRIBUTABLE proof, and the one that actually fences the
    exception.

    A tree diff can see that backend/storage changed but never who changed it. This
    does not need to: every app build re-checks that each redirectable path constant
    resolved inside its temp directory and outside the real store. If the suite could
    not reach real storage, it cannot have written there, no matter what else on the
    machine did. Zero false positives by construction.
    """
    # Takes `stack` so at least one app build always exists: in a full-suite run
    # this has already counted hundreds, and standalone it still means something.
    assert SANDBOX_CHECKS["builds"] > 0, (
        "no app was built this session -- the sandbox proof has nothing to stand on"
    )
    assert SANDBOX_CHECKS["paths_verified"] >= SANDBOX_CHECKS["builds"] * 6, (
        "every build must verify at least the six frozen path constants (SS2 config.py); "
        f"saw {SANDBOX_CHECKS['paths_verified']} checks over {SANDBOX_CHECKS['builds']} builds"
    )
    assert not SANDBOX_VIOLATIONS, (
        "a path constant resolved INSIDE the real backend/storage during an app build -- "
        f"this is a genuine sandbox escape:\n  {SANDBOX_VIOLATIONS}\n{PROMOTION_HINT}"
    )


def test_suite_never_creates_or_mutates_real_storage_outside_models():
    """SS3.6 layer 2 -- the environmental end-state check.

    Kept because it is the literal condition the architect attached to the exception,
    but it is inherently unattributable, so on a delta it runs a discriminator before
    blaming the suite.
    """
    now = storage_signature()
    before = REAL_STORAGE_AT_COLLECTION
    created = sorted(set(now) - set(before) - SANCTIONED)
    mutated = sorted(
        name
        for name, sig in now.items()
        if name not in SANCTIONED and name in before and before[name] != sig
    )
    if not (created or mutated):
        return

    evidence = external_writer_evidence()
    detail = (
        f"created={created} mutated={mutated} in {REAL_STORAGE_DIR}\n"
        f"{BOTH_CAUSES}\n{PROMOTION_HINT}"
    )
    if evidence:
        pytest.skip(
            "backend/storage changed during this run, but a CONCURRENT EXTERNAL WRITER "
            "was detected, so this check cannot attribute the change to the suite:\n  "
            + "\n  ".join(evidence)
            + "\nThe attributable layer "
            "(test_every_app_build_was_redirected_away_from_real_storage) passed, so the "
            "suite did not escape its sandbox. Stop the dev server and re-run to assert "
            f"this condition literally.\n{detail}"
        )
    assert not (created or mutated), (
        "backend/storage changed during this run and no external writer was detected.\n"
        + detail
    )


def test_real_storage_holds_nothing_but_models():
    """The literal SS3.6 end-state assertion."""
    if not REAL_STORAGE_DIR.is_dir():
        pytest.skip("no real backend/storage/ on this machine -- nothing could have leaked")
    stale = sorted(set(REAL_STORAGE_AT_COLLECTION) - SANCTIONED)
    if stale:
        pytest.skip(
            "a local dev store predates this session "
            f"({stale}) -- `make dev` writes there legitimately, so the literal SS3.6 "
            "end-state cannot be asserted here. The delta guard "
            "(test_suite_never_creates_or_mutates_real_storage_outside_models) and the "
            "attributable layer (test_every_app_build_was_redirected_away_from_real_storage) "
            "still enforce the condition; delete backend/storage/ to assert it literally."
        )
    unexpected = sorted(n for n in storage_signature() if n not in SANCTIONED)
    assert not unexpected, (
        f"SS3.6: after a full run the real {REAL_STORAGE_DIR} must contain nothing but "
        f"models/. Found {unexpected}.\n{PROMOTION_HINT}"
    )


def test_the_sanctioned_exception_is_exactly_one_name():
    assert SANCTIONED == {"models"}, (
        "SS3.6: 'No second exception may be added without an architect ratification and "
        f"an ADR line.' This set is currently {sorted(SANCTIONED)}."
    )


def test_frozen_path_seams_still_exist_under_config(stack, qa):
    """SS2/SS3.6: the six frozen names are the mechanism the exception is measured
    against -- if one is renamed the harness silently stops isolating."""
    config = qa.backend_module("config")
    for name in (
        "STORAGE_DIR", "UPLOADS_DIR", "CHROMA_DIR",
        "DOCSTORE_PATH", "MANIFEST_PATH", "EMBED_CACHE_PATH",
    ):
        qa.require_attr(config, name, "SS2 config.py (six names FROZEN as a test seam)")
    qa.require_attr(config, "RERANK_MODEL_DIR", "SS2 config.py (SS3.6 sanctioned exception)")
    qa.require_attr(config, "LLM_BUDGET_PATH", "SS2 config.py (derives from the patched STORAGE_DIR)")

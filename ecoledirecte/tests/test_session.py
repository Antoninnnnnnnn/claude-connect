"""Login backoff, credential scrubbing and payload decoding.

The backoff is the guard that stops a rejected login being replayed on every API
call, which is what risks an account lock upstream.
"""

import json
from datetime import datetime
from pathlib import Path

import pytest

from ed_session import EDSessionManager, scrub_secrets
from main import current_school_year, decode_content


# --------------------------------------------------------- credential scrubbing


def test_scrub_replaces_json_credential_fields():
    text = '{"identifiant":"jdoe", "motdepasse":"s3cret", "isRelogin": false}'
    scrubbed = scrub_secrets(text)
    assert "jdoe" not in scrubbed
    assert "s3cret" not in scrubbed
    assert scrubbed.count("[redacted]") == 2


def test_scrub_replaces_literal_values():
    assert "hunter2" not in scrub_secrets("login failed for hunter2", "hunter2")


def test_scrub_handles_unquoted_field_names():
    text = 'data={identifiant:"jdoe", motdepasse:"s3cret"}'
    scrubbed = scrub_secrets(text)
    assert "jdoe" not in scrubbed and "s3cret" not in scrubbed


def test_scrub_ignores_empty_secrets():
    assert scrub_secrets("nothing here", "", None or "") == "nothing here"


def test_scrub_accepts_non_string_input():
    assert scrub_secrets(500) == "500"


def test_scrub_leaves_innocent_text_alone():
    assert scrub_secrets("Token invalide !") == "Token invalide !"


def test_recorded_failure_is_scrubbed(manager):
    """`_last_error` reaches /status and the journal, so it must be clean."""
    manager._record_failure(
        'LoginException: {"identifiant":"test-user", "motdepasse":"test-pass"}',
        credential=True,
    )
    assert "test-pass" not in manager._last_error
    assert "test-pass" not in json.dumps(manager.status())


# ---------------------------------------------------------------------- backoff


def test_first_failure_arms_the_base_backoff(manager):
    manager._record_failure("boom", credential=False)
    status = manager.status()
    assert status["consecutive_failures"] == 1
    assert 0 < status["retry_in_s"] <= 10


def test_backoff_grows_exponentially(manager):
    delays = []
    for _ in range(4):
        manager._record_failure("boom", credential=False)
        delays.append(manager.status()["retry_in_s"])
    assert delays[0] < delays[1] < delays[2], f"backoff not growing: {delays}"


def test_backoff_is_capped(manager):
    for _ in range(20):
        manager._record_failure("boom", credential=False)
    assert manager.status()["retry_in_s"] <= manager.settings.ed_login_backoff_max


def test_credential_failures_counted_separately(manager):
    manager._record_failure("network", credential=False)
    manager._record_failure("bad creds", credential=True)
    status = manager.status()
    assert status["consecutive_failures"] == 2
    assert status["credential_failures"] == 1


def test_credentials_rejected_after_threshold(manager):
    for _ in range(manager.settings.ed_max_credential_failures):
        manager._record_failure("bad creds", credential=True)
    assert manager.status()["credentials_rejected"] is True


def test_credentials_not_rejected_by_transient_failures(manager):
    for _ in range(10):
        manager._record_failure("network", credential=False)
    assert manager.status()["credentials_rejected"] is False


def test_success_resets_everything(manager):
    manager._record_failure("boom", credential=True)
    manager._record_success()
    status = manager.status()
    assert status["consecutive_failures"] == 0
    assert status["credential_failures"] == 0
    assert status["retry_in_s"] == 0
    assert status["last_error"] is None


def test_status_shape(manager):
    assert set(manager.status()) == {
        "logged_in", "eleve_id", "mfa_pending", "consecutive_failures",
        "credential_failures", "credentials_rejected", "retry_in_s", "last_error",
    }


# -------------------------------------------------------------------------- MFA


def test_submit_mfa_without_pending_question(manager):
    ok, message = manager.submit_mfa("whatever")
    assert ok is False and "No pending" in message


def test_submit_mfa_rejects_answer_outside_propositions(manager):
    manager.pending_question = "Quelle est votre classe ?"
    manager.pending_propositions = ["TS1", "TS2"]
    ok, message = manager.submit_mfa("TS9")
    assert ok is False and "must be one of" in message


def test_submit_mfa_persists_answer(manager):
    manager.pending_question = "Quelle est votre classe ?"
    manager.pending_propositions = ["TS1", "TS2"]
    ok, _ = manager.submit_mfa("TS1")
    assert ok is True
    stored = json.loads(Path(manager.settings.qcm_file).read_text())
    assert stored["Quelle est votre classe ?"] == ["TS1"]


def test_qcm_file_is_owner_only(manager):
    manager.pending_question = "q"
    manager.pending_propositions = ["a", "b"]
    manager.submit_mfa("a")
    mode = Path(manager.settings.qcm_file).stat().st_mode & 0o777
    assert mode == 0o600, "security answers must not be world readable"


def test_submit_mfa_clears_pending_and_unblocks(manager):
    manager.pending_question = "q"
    manager.pending_propositions = ["a", "b"]
    manager.submit_mfa("a")
    assert manager.pending_question is None
    assert manager.get_pending_mfa() is None
    assert manager.mfa_event.is_set()


def test_submit_mfa_resets_backoff(manager):
    """A new answer may unblock credentials that were previously rejected."""
    for _ in range(3):
        manager._record_failure("bad creds", credential=True)
    manager.pending_question = "q"
    manager.pending_propositions = ["a", "b"]
    manager.submit_mfa("a")
    status = manager.status()
    assert status["credential_failures"] == 0
    assert status["credentials_rejected"] is False
    assert status["retry_in_s"] == 0


def test_get_pending_mfa_shape(manager):
    manager.pending_question = "q"
    manager.pending_propositions = ["a", "b"]
    assert manager.get_pending_mfa() == {"question": "q", "propositions": ["a", "b"]}


# ------------------------------------------------------------------- qcm store


def test_missing_qcm_file_is_empty(settings):
    assert EDSessionManager(settings).qcm_json == {}


def test_malformed_qcm_file_is_empty(settings):
    Path(settings.qcm_file).write_text("{not json")
    assert EDSessionManager(settings).qcm_json == {}


def test_existing_qcm_file_is_loaded(settings):
    Path(settings.qcm_file).write_text(json.dumps({"q": ["a"]}))
    assert EDSessionManager(settings).qcm_json == {"q": ["a"]}


# ----------------------------------------------------------- payload decoding


def test_decode_base64_content():
    import base64

    encoded = base64.b64encode("Exercices 4 et 5 page 32".encode()).decode()
    assert decode_content(encoded) == "Exercices 4 et 5 page 32"


def test_decode_handles_accents():
    import base64

    encoded = base64.b64encode("Réviser le chapitre 2".encode()).decode()
    assert decode_content(encoded) == "Réviser le chapitre 2"


def test_decode_passes_through_plain_text():
    """Upstream sometimes sends the field already decoded."""
    assert decode_content("déjà en clair !") == "déjà en clair !"


@pytest.mark.parametrize("value", [None, "", 0])
def test_decode_empty_is_none(value):
    assert decode_content(value) is None


def test_decode_never_raises_on_garbage():
    assert decode_content("!!!not base64!!!") is not None


# ------------------------------------------------------------- school year


def test_school_year_matches_current_date():
    now = datetime.now()
    expected_start = now.year if now.month >= 8 else now.year - 1
    assert current_school_year() == f"{expected_start}-{expected_start + 1}"


def test_school_year_format():
    assert len(current_school_year()) == 9
    assert current_school_year()[4] == "-"

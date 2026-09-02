"""Single shared EcoleDirecte session, with MFA relay and login backoff.

EcoleDirecte locks an account after a few rejected logins, so a failed login is
never retried on the next API call: failures are counted and spaced out with an
exponential backoff, and credentials that are rejected outright stop the loop
until the QCM answers or the credentials change.
"""

import asyncio
import json
import logging
import re
import time

from ecoledirecte_api.client import EDClient
from ecoledirecte_api.exceptions import LoginException, MFARequiredException

from config import Settings, get_settings

# Patch for ecoledirecte_api bug on token expiration
if not hasattr(EDClient, "freshlogin"):
    EDClient.freshlogin = EDClient.login

logger = logging.getLogger(__name__)

# ecoledirecte_api puts the whole login payload in its exception messages, so the
# credentials would otherwise land in the journal *and* in our HTTP error bodies.
# Match the JSON fields rather than the literal secret: the library url-encodes it.
_CREDENTIAL_FIELDS = re.compile(r'("?(?:identifiant|motdepasse)"?\s*:\s*")[^"]*(")')


def scrub_secrets(text: str, *secrets: str) -> str:
    """Replace credentials with a placeholder in anything we log or return."""
    scrubbed = _CREDENTIAL_FIELDS.sub(r"\1[redacted]\2", str(text))
    for secret in secrets:
        if secret:
            scrubbed = scrubbed.replace(secret, "[redacted]")
    return scrubbed


class EDSessionError(RuntimeError):
    """Base class for every session-level failure surfaced to the API layer."""


class CredentialsMissing(EDSessionError):
    pass


class CredentialsRejected(EDSessionError):
    pass


class MfaRequired(EDSessionError):
    pass


class LoginInProgress(EDSessionError):
    pass


class LoginBackoff(EDSessionError):
    pass


class EDSessionManager:
    def __init__(self, settings: Settings | None = None):
        self.settings = settings or get_settings()
        self.qcm_file = self.settings.qcm_file
        self.qcm_json = self._load_qcm()
        self.client: EDClient | None = None
        self.pending_question = None
        self.pending_propositions = None
        self.mfa_event = asyncio.Event()
        self.login_task: asyncio.Task | None = None
        self.eleve_id = None
        self.is_logged_in = False
        self._lock = asyncio.Lock()
        self._consecutive_failures = 0
        self._credential_failures = 0
        self._next_login_allowed = 0.0
        self._last_error: str | None = None

    # --------------------------------------------------------------- qcm store

    def _load_qcm(self) -> dict:
        if self.qcm_file.exists():
            try:
                return json.loads(self.qcm_file.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                logger.error("Error loading %s: %s", self.qcm_file, exc)
        return {}

    def _save_qcm(self) -> None:
        try:
            self.qcm_file.parent.mkdir(parents=True, exist_ok=True)
            self.qcm_file.write_text(json.dumps(self.qcm_json, indent=4), encoding="utf-8")
            self.qcm_file.chmod(0o600)
        except OSError as exc:
            logger.error("Error saving %s: %s", self.qcm_file, exc)

    # ------------------------------------------------------------------- state

    def status(self) -> dict:
        wait = max(0.0, self._next_login_allowed - time.monotonic())
        return {
            "logged_in": self.is_logged_in,
            "eleve_id": self.eleve_id,
            "mfa_pending": self.pending_question is not None,
            "consecutive_failures": self._consecutive_failures,
            "credential_failures": self._credential_failures,
            "credentials_rejected": self._credential_failures >= self.settings.ed_max_credential_failures,
            "retry_in_s": round(wait, 1) if wait else 0,
            "last_error": self._last_error,
        }

    def _record_failure(self, error: str, *, credential: bool) -> None:
        self._consecutive_failures += 1
        if credential:
            self._credential_failures += 1
        error = scrub_secrets(error, self.settings.ed_password, self.settings.ed_username)
        self._last_error = error
        backoff = min(
            self.settings.ed_login_backoff_base * (2 ** (self._consecutive_failures - 1)),
            self.settings.ed_login_backoff_max,
        )
        self._next_login_allowed = time.monotonic() + backoff
        logger.warning(
            "EcoleDirecte login failed (%s attempt(s), credential=%s): %s — next try in %.0fs",
            self._consecutive_failures,
            credential,
            error,
            backoff,
        )

    def _record_success(self) -> None:
        self._consecutive_failures = 0
        self._credential_failures = 0
        self._next_login_allowed = 0.0
        self._last_error = None

    # ------------------------------------------------------------------- login

    async def _on_new_question(self, updated_qcm) -> None:
        logger.info("New QCM question intercepted")
        for question, propositions in updated_qcm.items():
            if len(propositions) > 1:
                self.pending_question = question
                self.pending_propositions = propositions
                break

        self.mfa_event.clear()
        # Block the login coroutine until the answer arrives through POST /mfa.
        await self.mfa_event.wait()

    async def get_client(self) -> EDClient:
        if not self.settings.ed_username or not self.settings.ed_password:
            raise CredentialsMissing("ED_USERNAME or ED_PASSWORD not set in environment")

        if self.client is not None and self.is_logged_in:
            return self.client

        if self.pending_question:
            raise MfaRequired("A QCM answer is pending")

        # One login at a time: concurrent callers wait on the running task instead of
        # each spawning their own (which would multiply rejected attempts).
        async with self._lock:
            if self.client is not None and self.is_logged_in:
                return self.client
            if self.pending_question:
                raise MfaRequired("A QCM answer is pending")

            task = self.login_task
            if task is None or task.done():
                if self._credential_failures >= self.settings.ed_max_credential_failures:
                    raise CredentialsRejected(
                        f"Credentials rejected {self._credential_failures} times; "
                        f"fix ED_USERNAME/ED_PASSWORD or the QCM answers, then restart. "
                        f"Last error: {self._last_error}"
                    )
                wait = self._next_login_allowed - time.monotonic()
                if wait > 0:
                    raise LoginBackoff(
                        f"Login backoff active, retry in {wait:.0f}s. Last error: {self._last_error}"
                    )
                await self._close_client()
                self.client = EDClient(
                    username=self.settings.ed_username,
                    password=self.settings.ed_password,
                    qcm_json=self.qcm_json,
                )
                self.client.on_new_question(self._on_new_question)
                task = asyncio.create_task(self._perform_login())
                self.login_task = task

        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=self.settings.ed_login_timeout)
        except asyncio.TimeoutError:
            if self.pending_question:
                raise MfaRequired("A QCM answer is pending") from None
            raise LoginInProgress("Login in progress, please retry in a few seconds") from None

        if not self.is_logged_in or self.client is None:
            raise EDSessionError(self._last_error or "Login failed")
        return self.client

    async def _perform_login(self) -> None:
        self.is_logged_in = False
        client = self.client
        if client is None:
            self._record_failure("no client instance", credential=False)
            return
        try:
            res = await client.login()
        except MFARequiredException:
            self._record_failure("MFA required but no answer available", credential=True)
            return
        except LoginException as exc:
            detail = f"LoginException: message={getattr(exc, 'message', '')}, status={getattr(exc, 'status', '')}"
            self._record_failure(detail, credential=True)
            return
        except Exception as exc:  # noqa: BLE001 - network/library failures are transient
            self._record_failure(f"{type(exc).__name__}: {exc}", credential=False)
            return

        if not res or res.get("code") != 200:
            self._record_failure(f"unexpected login response: {res}", credential=True)
            return

        for account in res.get("data", {}).get("accounts", []):
            if account.get("typeCompte") == "E":
                self.eleve_id = account.get("id")
                break

        if not self.eleve_id:
            self._record_failure("no student account (typeCompte=E) on this login", credential=True)
            return

        self.is_logged_in = True
        self.pending_question = None
        self.pending_propositions = None
        self._record_success()
        logger.info("Successfully logged in as eleve_id: %s", self.eleve_id)

    # -------------------------------------------------------------------- mfa

    def get_pending_mfa(self) -> dict | None:
        if self.pending_question:
            return {
                "question": self.pending_question,
                "propositions": self.pending_propositions,
            }
        return None

    def submit_mfa(self, answer: str) -> tuple[bool, str]:
        if not self.pending_question:
            return False, "No pending MFA question"

        if answer not in (self.pending_propositions or []):
            return False, f"Answer must be one of: {self.pending_propositions}"

        self.qcm_json[self.pending_question] = [answer]
        self._save_qcm()

        self.pending_question = None
        self.pending_propositions = None
        # A new answer may unblock credentials that were previously rejected.
        self._credential_failures = 0
        self._consecutive_failures = 0
        self._next_login_allowed = 0.0
        self.mfa_event.set()

        return True, "MFA answer submitted, login resuming"

    # --------------------------------------------------------------- lifecycle

    async def _close_client(self) -> None:
        client = self.client
        self.client = None
        self.is_logged_in = False
        if client is None:
            return
        close = getattr(client, "close", None)
        if close is None:
            return
        try:
            result = close()
            if asyncio.iscoroutine(result):
                await result
        except Exception as exc:  # noqa: BLE001 - best-effort cleanup
            logger.debug("Error closing EDClient: %s", exc)

    async def close(self) -> None:
        task = self.login_task
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception as exc:  # noqa: BLE001 - shutdown path, log and move on
                logger.debug("Login task ended with error during shutdown: %s", exc)
        self.login_task = None
        await self._close_client()


session_manager = EDSessionManager()

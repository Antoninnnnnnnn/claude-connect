import asyncio
import base64
import html
import logging
import re
import secrets
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from typing import Any, Optional

from fastapi import Body, Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from config import Settings, get_settings
from ed_session import (
    CredentialsMissing,
    CredentialsRejected,
    EDSessionError,
    LoginBackoff,
    LoginInProgress,
    MfaRequired,
    session_manager,
)


logger = logging.getLogger(__name__)
settings = get_settings()


@asynccontextmanager
async def lifespan(_: FastAPI):
    if not settings.api_key:
        raise RuntimeError("API_KEY is not configured")
    if not settings.ed_username or not settings.ed_password:
        # Not fatal: the service still answers /health and reports the problem.
        logger.warning("ED_USERNAME / ED_PASSWORD are not set; every data endpoint will fail")
    try:
        yield
    finally:
        await session_manager.close()


app = FastAPI(
    title="Ecole Directe API",
    description="Mini-API REST pour récupérer les données d'École Directe.",
    version="1.1.0",
    docs_url="/docs",
    lifespan=lifespan,
)


@app.exception_handler(HTTPException)
async def http_error_handler(_: Request, exc: HTTPException) -> JSONResponse:
    detail = exc.detail
    if isinstance(detail, dict):
        # Structured details (the MFA payload) are merged, not stringified.
        return JSONResponse(status_code=exc.status_code, content={"ok": False, **detail})
    return JSONResponse(status_code=exc.status_code, content={"ok": False, "error": str(detail)})


@app.exception_handler(RequestValidationError)
async def validation_error_handler(_: Request, exc: RequestValidationError) -> JSONResponse:
    return JSONResponse(status_code=422, content={"ok": False, "error": str(exc.errors())})


@app.exception_handler(Exception)
async def generic_error_handler(_: Request, exc: Exception) -> JSONResponse:
    logger.exception("Unhandled error: %s", exc)
    return JSONResponse(status_code=500, content={"ok": False, "error": "Internal server error"})


def verify_api_key(
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    current_settings: Settings = Depends(get_settings),
) -> None:
    if not current_settings.api_key:
        raise HTTPException(status_code=500, detail="API_KEY is not configured on server")
    if not x_api_key or not secrets.compare_digest(x_api_key, current_settings.api_key):
        raise HTTPException(status_code=401, detail="Invalid API Key")


async def get_ed_client():
    """Resolve the shared client, mapping session failures onto HTTP statuses."""
    try:
        return await session_manager.get_client()
    except MfaRequired:
        raise HTTPException(
            status_code=401,
            detail={
                "error": "MFA_REQUIRED",
                "message": "Veuillez répondre au QCM via l'endpoint POST /mfa",
                "mfa_data": session_manager.get_pending_mfa(),
            },
        ) from None
    except LoginInProgress as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from None
    except LoginBackoff as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from None
    except CredentialsRejected as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from None
    except CredentialsMissing as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from None
    except EDSessionError as exc:
        raise HTTPException(status_code=502, detail=f"Failed to get client: {exc}") from None


def require_eleve_id() -> str:
    if not session_manager.eleve_id:
        raise HTTPException(status_code=502, detail="Eleve ID not found on the logged-in account")
    return str(session_manager.eleve_id)


def current_school_year() -> str:
    now = datetime.now()
    year = now.year if now.month >= 8 else now.year - 1
    return f"{year}-{year + 1}"


def decode_content(value: Any) -> str | None:
    """Decode a base64 EcoleDirecte rich-text field, or return it as-is."""
    if not value:
        return None
    try:
        decoded = base64.b64decode(value).decode("utf-8")
    except Exception:  # noqa: BLE001 - upstream sometimes sends plain text
        decoded = str(value)
    return html_to_text(decoded)


def html_to_text(value: str) -> str:
    """Flatten the HTML teachers type in EcoleDirecte into readable plain text."""
    text = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</li>", "\n", value)
    text = re.sub(r"(?i)<li[^>]*>", "- ", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = html.unescape(text).replace("\xa0", " ")
    lines = [line.strip() for line in text.splitlines()]
    return "\n".join(line for line in lines if line).strip()


@app.get("/")
async def root() -> dict[str, Any]:
    return {"ok": True, "message": "Ecole Directe API is running."}


@app.get("/health")
async def health() -> dict[str, Any]:
    """Unauthenticated liveness probe: no session detail, it is reachable publicly."""
    return {"ok": True, "data": {"status": "up"}}


@app.get("/status", dependencies=[Depends(verify_api_key)])
async def status() -> dict[str, Any]:
    """Login state, failure counters and backoff. Behind the API key."""
    return {"ok": True, "data": {"status": "up", "session": session_manager.status()}}


@app.get("/mfa", dependencies=[Depends(verify_api_key)])
async def get_mfa_status() -> dict[str, Any]:
    """Affiche la question QCM en attente s'il y en a une."""
    mfa_data = session_manager.get_pending_mfa()
    if mfa_data:
        return {"ok": False, "mfa_required": True, "data": mfa_data}
    return {"ok": True, "mfa_required": False, "message": "No pending MFA question."}


@app.post("/mfa", dependencies=[Depends(verify_api_key)])
async def submit_mfa_answer(answer: str = Body(..., embed=True)) -> dict[str, Any]:
    """Soumet la réponse au QCM en attente."""
    success, message = session_manager.submit_mfa(answer)
    if not success:
        raise HTTPException(status_code=409, detail=message)
    return {"ok": True, "message": message}


@app.get("/schedule", dependencies=[Depends(verify_api_key)])
async def get_schedule(
    start_date: Optional[str] = Query(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    end_date: Optional[str] = Query(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
) -> dict[str, Any]:
    """Récupère l'emploi du temps. Dates au format YYYY-MM-DD."""
    client = await get_ed_client()
    eleve_id = require_eleve_id()

    start_date = start_date or datetime.now().strftime("%Y-%m-%d")
    end_date = end_date or (datetime.now() + timedelta(days=7)).strftime("%Y-%m-%d")

    try:
        raw_data = await client.get_lessons(eleve_id, start_date, end_date)
    except Exception as exc:  # noqa: BLE001 - upstream failure, not our bug
        raise HTTPException(status_code=502, detail=f"EcoleDirecte error: {exc}") from None

    lessons = []
    for lesson in raw_data.get("data", []):
        # On a COURS the label lives in "matiere"; on an EVENEMENT (outing, meeting,
        # exam slot) it only lives in "text", so fall back before deciding it is empty.
        subject = (lesson.get("matiere") or lesson.get("text") or "").strip()
        if not subject:
            continue
        lessons.append(
            {
                # Per-occurrence id, stable across calls: use it as the upsert key
                # when syncing to an external calendar so re-runs don't duplicate.
                "id": lesson.get("id"),
                "start": lesson.get("start_date"),
                "end": lesson.get("end_date"),
                "subject": subject,
                "subject_code": lesson.get("codeMatiere") or None,
                "type": lesson.get("typeCours"),
                "prof": lesson.get("prof"),
                "room": lesson.get("salle"),
                # Empty when the whole class attends, set for half-class / option groups.
                "group": lesson.get("groupeCode") or None,
                "color": lesson.get("color") or None,
                "has_homework": lesson.get("devoirAFaire", False),
                "has_content": lesson.get("contenuDeSeance", False),
                "is_cancelled": lesson.get("isAnnule", False),
                "is_modified": lesson.get("isModifie", False),
            }
        )
    lessons.sort(key=lambda item: (item["start"] or "", item["end"] or ""))
    return {"ok": True, "data": lessons}


@app.get("/homework", dependencies=[Depends(verify_api_key)])
async def get_homework(
    with_content: bool = Query(default=True, description="Fetch the assignment text (one extra call per date)"),
) -> dict[str, Any]:
    """Récupère les devoirs, avec le texte de chaque devoir."""
    client = await get_ed_client()
    eleve_id = require_eleve_id()

    try:
        payload = await client.get_homeworks(eleve_id)
    except Exception as exc:  # noqa: BLE001 - upstream failure, not our bug
        raise HTTPException(status_code=502, detail=f"EcoleDirecte error: {exc}") from None

    index = payload.get("data") or {}
    # The index only flags that an assignment exists ("aFaire": true); the text lives
    # in the per-day endpoint, so fetch each day and key the results by idDevoir.
    contents: dict[int, dict[str, Any]] = {}
    if with_content and index:
        contents = await fetch_homework_contents(client, eleve_id, list(index))

    homework: dict[str, Any] = {}
    for date, entries in index.items():
        day: list[dict[str, Any]] = []
        for entry in entries or []:
            detail = contents.get(entry.get("idDevoir")) or {}
            item: dict[str, Any] = {
                "subject": entry.get("matiere"),
                "subject_code": entry.get("codeMatiere") or None,
                "done": bool(entry.get("effectue", False)),
                # EcoleDirecte's flag for "this one is a graded test", not just homework.
                "is_test": bool(entry.get("interrogation", False)),
                "given_on": entry.get("donneLe"),
            }
            if detail.get("prof"):
                item["prof"] = detail["prof"]
            if detail.get("content"):
                item["content"] = detail["content"]
            day.append(item)
        if day:
            homework[date] = day

    return {"ok": True, "data": homework}


async def fetch_homework_contents(client: Any, eleve_id: str, dates: list[str]) -> dict[int, dict[str, Any]]:
    """Load the assignment text for each date, capped so we don't hammer EcoleDirecte."""
    semaphore = asyncio.Semaphore(4)

    async def load(date: str) -> dict[int, dict[str, Any]]:
        async with semaphore:
            try:
                detail = await client.get_homeworks_by_date(eleve_id, date)
            except Exception as exc:  # noqa: BLE001 - a missing text must not fail the endpoint
                logger.warning("Homework detail failed for %s: %s", date, exc)
                return {}
        found: dict[int, dict[str, Any]] = {}
        for subject in (detail.get("data") or {}).get("matieres") or []:
            to_do = subject.get("aFaire")
            if not isinstance(to_do, dict):
                continue
            found[to_do.get("idDevoir")] = {
                "content": decode_content(to_do.get("contenu")),
                "prof": subject.get("nomProf"),
            }
        return found

    merged: dict[int, dict[str, Any]] = {}
    for result in await asyncio.gather(*(load(date) for date in dates)):
        merged.update(result)
    return merged


@app.get("/grades", dependencies=[Depends(verify_api_key)])
async def get_grades(annee_scolaire: Optional[str] = Query(default=None, pattern=r"^\d{4}-\d{4}$")) -> dict[str, Any]:
    """Récupère les notes. Sans année, utilise l'année scolaire en cours."""
    client = await get_ed_client()
    eleve_id = require_eleve_id()
    annee_scolaire = annee_scolaire or current_school_year()

    try:
        raw_data = await client.get_grades_evaluations(eleve_id, annee_scolaire)
    except Exception as exc:  # noqa: BLE001 - upstream failure, not our bug
        raise HTTPException(status_code=502, detail=f"EcoleDirecte error: {exc}") from None

    data = raw_data.get("data") or {}
    periods = [
        {
            "id": period.get("idPeriode"),
            "nom": period.get("periode"),
            "moyenne_eleve": (period.get("ensembleMatieres") or {}).get("moyenneEleve"),
            "moyenne_classe": (period.get("ensembleMatieres") or {}).get("moyenneClasse"),
        }
        for period in data.get("periodes") or []
    ]
    grades = [
        {
            "date": note.get("date"),
            "subject": note.get("libelleMatiere"),
            "name": note.get("devoir"),
            "grade": note.get("valeur"),
            "out_of": note.get("noteSur"),
            "coef": note.get("coef"),
            "class_avg": note.get("moyenneClasse"),
        }
        for note in data.get("notes") or []
    ]
    return {"ok": True, "data": {"annee_scolaire": annee_scolaire, "periodes": periods, "notes": grades}}


@app.get("/messages", dependencies=[Depends(verify_api_key)])
async def get_messages(annee_scolaire: Optional[str] = Query(default=None, pattern=r"^\d{4}-\d{4}$")) -> dict[str, Any]:
    """Récupère la messagerie (messages reçus)."""
    client = await get_ed_client()
    eleve_id = require_eleve_id()
    annee_scolaire = annee_scolaire or current_school_year()

    try:
        raw_data = await client.get_messages(None, eleve_id, annee_scolaire)
    except Exception as exc:  # noqa: BLE001 - upstream failure, not our bug
        raise HTTPException(status_code=502, detail=f"EcoleDirecte error: {exc}") from None

    received = ((raw_data.get("data") or {}).get("messages") or {}).get("received") or []
    messages = []
    for message in received:
        sender = message.get("from") or {}
        messages.append(
            {
                "date": message.get("date"),
                "subject": message.get("subject"),
                "from": f"{sender.get('prenom', '')} {sender.get('nom', '')}".strip(),
                "read": message.get("read", False),
                "has_files": bool(message.get("files")),
            }
        )
    return {"ok": True, "data": messages}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host=settings.host, port=settings.port)

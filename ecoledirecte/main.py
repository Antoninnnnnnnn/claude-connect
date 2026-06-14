import os
from datetime import datetime, timedelta
from typing import Optional
from fastapi import FastAPI, Depends, HTTPException, Header, Body
from dotenv import load_dotenv

# Load environment variables
load_dotenv("/home/antonin/claude-connect/.env")

from ed_session import session_manager

app = FastAPI(
    title="Ecole Directe API",
    description="Mini-API REST pour récupérer les données d'École Directe.",
    version="1.0.0"
)

# API Key Dependency
def verify_api_key(x_api_key: str = Header(None)):
    expected_api_key = os.getenv("API_KEY")
    if not expected_api_key:
        raise HTTPException(status_code=500, detail="API_KEY not configured on server")
    if x_api_key != expected_api_key:
        raise HTTPException(status_code=401, detail="Invalid API Key")
    return x_api_key

async def get_ed_client():
    try:
        client = await session_manager.get_client()
        return client
    except ValueError as ve:
        raise HTTPException(status_code=500, detail=str(ve))
    except Exception as e:
        if str(e) == "MFA_REQUIRED":
            mfa_data = session_manager.get_pending_mfa()
            raise HTTPException(
                status_code=401, 
                detail={
                    "error": "MFA_REQUIRED", 
                    "message": "Veuillez répondre au QCM via l'endpoint POST /mfa",
                    "mfa_data": mfa_data
                }
            )
        elif str(e) == "LOGIN_IN_PROGRESS":
            raise HTTPException(status_code=503, detail="Login in progress, please retry in a few seconds")
        else:
            raise HTTPException(status_code=500, detail=f"Failed to get client: {e}")

@app.get("/")
async def root():
    return {"ok": True, "message": "Ecole Directe API is running."}

@app.get("/mfa")
async def get_mfa_status(api_key: str = Depends(verify_api_key)):
    """Affiche la question QCM en attente s'il y en a une."""
    mfa_data = session_manager.get_pending_mfa()
    if mfa_data:
        return {"ok": False, "mfa_required": True, "data": mfa_data}
    return {"ok": True, "mfa_required": False, "message": "No pending MFA question."}

@app.post("/mfa")
async def submit_mfa_answer(answer: str = Body(..., embed=True), api_key: str = Depends(verify_api_key)):
    """Soumet la réponse au QCM en attente."""
    success, msg = session_manager.submit_mfa(answer)
    if success:
        return {"ok": True, "message": msg}
    else:
        return {"ok": False, "error": msg}

@app.get("/schedule")
async def get_schedule(start_date: Optional[str] = None, end_date: Optional[str] = None, api_key: str = Depends(verify_api_key)):
    """Récupère l'emploi du temps. Dates au format YYYY-MM-DD."""
    client = await get_ed_client()
    
    if not start_date:
        start_date = datetime.now().strftime("%Y-%m-%d")
    if not end_date:
        # Default to end of week or +7 days
        end_date = (datetime.now() + timedelta(days=7)).strftime("%Y-%m-%d")
        
    if not session_manager.eleve_id:
        raise HTTPException(status_code=500, detail="Eleve ID not found")
        
    try:
        raw_data = await client.get_lessons(str(session_manager.eleve_id), start_date, end_date)
        filtered_data = []
        for lesson in raw_data.get("data", []):
            if lesson.get("typeCours") == "EVENEMENT" and not lesson.get("matiere").strip():
                continue # Skip empty events
            filtered_data.append({
                "start": lesson.get("start_date"),
                "end": lesson.get("end_date"),
                "subject": lesson.get("matiere"),
                "prof": lesson.get("prof"),
                "room": lesson.get("salle"),
                "is_cancelled": lesson.get("isAnnule", False),
                "is_modified": lesson.get("isModifie", False),
            })
        return {"ok": True, "data": filtered_data}
    except Exception as e:
        return {"ok": False, "error": str(e)}

@app.get("/homework")
async def get_homework(api_key: str = Depends(verify_api_key)):
    """Récupère les devoirs."""
    client = await get_ed_client()
    if not session_manager.eleve_id:
        raise HTTPException(status_code=500, detail="Eleve ID not found")
        
    try:
        hw = await client.get_homeworks(str(session_manager.eleve_id))
        data = hw.get("data", {})
        
        filtered_hw = {}
        for date, hw_list in data.items():
            day_hw = []
            for h in hw_list:
                hw_item = {
                    "subject": h.get("matiere"),
                    "done": h.get("effectue", False),
                }
                if isinstance(h.get("aFaire"), dict):
                    content_b64 = h["aFaire"].get("contenu", "")
                    if content_b64:
                        try:
                            hw_item["content"] = base64.b64decode(content_b64).decode("utf-8")
                        except:
                            hw_item["content"] = "Erreur de décodage"
                day_hw.append(hw_item)
            if day_hw:
                filtered_hw[date] = day_hw
                        
        return {"ok": True, "data": filtered_hw}
    except Exception as e:
        return {"ok": False, "error": str(e)}

@app.get("/grades")
async def get_grades(annee_scolaire: Optional[str] = None, api_key: str = Depends(verify_api_key)):
    """Récupère les notes. Si l'année n'est pas fournie, utilise l'année en cours (ex: 2023-2024)."""
    client = await get_ed_client()
    if not session_manager.eleve_id:
        raise HTTPException(status_code=500, detail="Eleve ID not found")
        
    if not annee_scolaire:
        now = datetime.now()
        year = now.year if now.month >= 8 else now.year - 1
        annee_scolaire = f"{year}-{year+1}"
        
    try:
        raw_data = await client.get_grades_evaluations(str(session_manager.eleve_id), annee_scolaire)
        data = raw_data.get("data", {})
        
        filtered_grades = {
            "periodes": [],
            "notes": []
        }
        
        # Simplify periodes
        for p in data.get("periodes", []):
            filtered_grades["periodes"].append({
                "id": p.get("idPeriode"),
                "nom": p.get("periode"),
                "moyenne_eleve": p.get("ensembleMatieres", {}).get("moyenneEleve"),
                "moyenne_classe": p.get("ensembleMatieres", {}).get("moyenneClasse")
            })
            
        # Simplify notes
        for n in data.get("notes", []):
            filtered_grades["notes"].append({
                "date": n.get("date"),
                "subject": n.get("libelleMatiere"),
                "name": n.get("devoir"),
                "grade": n.get("valeur"),
                "out_of": n.get("noteSur"),
                "coef": n.get("coef"),
                "class_avg": n.get("moyenneClasse")
            })
            
        return {"ok": True, "data": filtered_grades}
    except Exception as e:
        return {"ok": False, "error": str(e)}

@app.get("/messages")
async def get_messages(annee_scolaire: Optional[str] = None, api_key: str = Depends(verify_api_key)):
    """Récupère la messagerie."""
    client = await get_ed_client()
    if not session_manager.eleve_id:
        raise HTTPException(status_code=500, detail="Eleve ID not found")
        
    if not annee_scolaire:
        now = datetime.now()
        year = now.year if now.month >= 8 else now.year - 1
        annee_scolaire = f"{year}-{year+1}"
        
    try:
        raw_data = await client.get_messages(None, str(session_manager.eleve_id), annee_scolaire)
        data = raw_data.get("data", {}).get("messages", {}).get("received", [])
        
        filtered_msgs = []
        for m in data:
            filtered_msgs.append({
                "date": m.get("date"),
                "subject": m.get("subject"),
                "from": f"{m.get('from', {}).get('prenom', '')} {m.get('from', {}).get('nom', '')}".strip(),
                "read": m.get("read", False),
                "has_files": len(m.get("files", [])) > 0
            })
            
        return {"ok": True, "data": filtered_msgs}
    except Exception as e:
        return {"ok": False, "error": str(e)}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8093, reload=True)

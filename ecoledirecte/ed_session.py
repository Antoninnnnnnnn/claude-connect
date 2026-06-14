import json
import asyncio
import os
import base64
import logging
from ecoledirecte_api.client import EDClient
from ecoledirecte_api.exceptions import QCMException, MFARequiredException, LoginException

# Patch for ecoledirecte_api bug on token expiration
if not hasattr(EDClient, "freshlogin"):
    EDClient.freshlogin = EDClient.login

logger = logging.getLogger(__name__)

class EDSessionManager:
    def __init__(self):
        self.qcm_file = "/home/antonin/claude-connect/ecoledirecte/qcm.json"
        self.qcm_json = self._load_qcm()
        self.client = None
        self.pending_question = None
        self.pending_propositions = None
        self.mfa_event = asyncio.Event()
        self.login_task = None
        self.eleve_id = None
        self.is_logged_in = False
        
    def _load_qcm(self):
        if os.path.exists(self.qcm_file):
            try:
                with open(self.qcm_file, "r") as f:
                    return json.load(f)
            except Exception as e:
                logger.error(f"Error loading qcm.json: {e}")
        return {}
        
    def _save_qcm(self):
        try:
            with open(self.qcm_file, "w") as f:
                json.dump(self.qcm_json, f, indent=4)
        except Exception as e:
            logger.error(f"Error saving qcm.json: {e}")

    async def _on_new_question(self, updated_qcm):
        logger.info("New QCM question intercepted")
        for q, props in updated_qcm.items():
            if len(props) > 1:
                self.pending_question = q
                self.pending_propositions = props
                break
                
        self.mfa_event.clear()
        # Wait for the user to answer via the API
        await self.mfa_event.wait()
        
    async def get_client(self):
        # Reload credentials in case they were just added to .env
        username = os.getenv("ED_USERNAME", "").strip()
        password = os.getenv("ED_PASSWORD", "").strip()
        
        if not username or not password:
            raise ValueError("ED_USERNAME or ED_PASSWORD not set in environment")

        if self.client and self.is_logged_in:
            return self.client
            
        if self.pending_question:
            raise Exception("MFA_REQUIRED")
            
        if self.login_task and not self.login_task.done():
            try:
                await asyncio.wait_for(asyncio.shield(self.login_task), timeout=3.0)
            except asyncio.TimeoutError:
                if self.pending_question:
                    raise Exception("MFA_REQUIRED")
                raise Exception("LOGIN_IN_PROGRESS")
            return self.client
            
        self.client = EDClient(username=username, password=password, qcm_json=self.qcm_json)
        self.client.on_new_question(self._on_new_question)
        
        self.login_task = asyncio.create_task(self._perform_login())
        
        try:
            await asyncio.wait_for(asyncio.shield(self.login_task), timeout=10.0)
        except asyncio.TimeoutError:
            if self.pending_question:
                raise Exception("MFA_REQUIRED")
            raise Exception("LOGIN_TIMEOUT")
            
        return self.client
        
    async def _perform_login(self):
        self.is_logged_in = False
        try:
            res = await self.client.login()
            if res and res.get("code") == 200:
                accounts = res.get("data", {}).get("accounts", [])
                for acc in accounts:
                    if acc.get("typeCompte") == "E":
                        self.eleve_id = acc.get("id")
                        break
                self.is_logged_in = True
                self.pending_question = None
                self.pending_propositions = None
                logger.info(f"Successfully logged in as eleve_id: {self.eleve_id}")
            else:
                logger.error(f"Login failed: {res}")
        except MFARequiredException as e:
            # The library should handle MFA loop inside login(), but just in case
            logger.error("MFA Required Exception raised")
        except LoginException as e:
            logger.error(f"LoginException: message={getattr(e, 'message', '')}, status={getattr(e, 'status', '')}")
        except Exception as e:
            logger.error(f"Error during login: {repr(e)}")
            
    def get_pending_mfa(self):
        if self.pending_question:
            return {
                "question": self.pending_question,
                "propositions": self.pending_propositions
            }
        return None

    def submit_mfa(self, answer: str):
        if not self.pending_question:
            return False, "No pending MFA question"
            
        if answer not in self.pending_propositions:
            return False, f"Answer must be one of: {self.pending_propositions}"
        
        self.qcm_json[self.pending_question] = [answer]
        self._save_qcm()
        
        self.pending_question = None
        self.pending_propositions = None
        self.mfa_event.set()
        
        return True, "MFA answer submitted, login resuming"

session_manager = EDSessionManager()

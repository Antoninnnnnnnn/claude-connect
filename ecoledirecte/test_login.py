import asyncio
import os
from dotenv import load_dotenv
import logging
from ecoledirecte_api.client import EDClient

logging.basicConfig(level=logging.DEBUG)

load_dotenv("/home/antonin/claude-connect/.env")

async def test():
    username = os.getenv("ED_USERNAME", "").strip()
    password = os.getenv("ED_PASSWORD", "").strip()
    
    print(f"Testing with user: {username}")
    
    client = EDClient(username=username, password=password, qcm_json={})
    
    try:
        res = await client.login()
        print(f"Result: {res}")
    except Exception as e:
        if type(e).__name__ == "LoginException":
            print(f"LoginException! message={getattr(e, 'message', '')}, status={getattr(e, 'status', '')}, payload={getattr(e, 'payload', '')}")
        else:
            print(f"Exception: {repr(e)}")

if __name__ == "__main__":
    asyncio.run(test())

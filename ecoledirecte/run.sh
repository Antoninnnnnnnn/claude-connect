#!/bin/bash
cd /home/antonin/claude-connect/ecoledirecte
source venv/bin/activate
uvicorn main:app --host 0.0.0.0 --port 8093

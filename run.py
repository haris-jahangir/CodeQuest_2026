"""ReportFix Server Runner.
Launches the FastAPI backend and serves the Single-Page Application.
"""
import sys
import os
import uvicorn

# Ensure project root is in sys.path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from backend import db

if __name__ == "__main__":
    print("=" * 60)
    print("REPORTFIX: Campus Issue Platform")
    print("Code Quest 2026 | AI Module")
    print("=" * 60)
    
    # Initialize DB
    db.init_db()
    db.seed_data(reset=False)
    
    port = int(os.environ.get("PORT", 8000))
    host = os.environ.get("HOST", "127.0.0.1")
    
    print(f"Starting server on http://{host}:{port} ...")
    print("Press Ctrl+C to stop.")
    uvicorn.run("backend.app:app", host=host, port=port, log_level="info")

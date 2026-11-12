"""The web server. For now it only reports that it's alive.

Run locally with:  uvicorn app.main:app --reload
"""

from fastapi import FastAPI

from app import __doc__ as app_description

app = FastAPI(title="Repo Guide", description=app_description, version="0.1.0")


@app.get("/health")
def health() -> dict[str, str]:
    """Used by hosting platforms (and us) to check the server is up."""
    return {"status": "ok"}

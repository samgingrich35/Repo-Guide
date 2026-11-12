from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_health_returns_ok():
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_home_page_is_served():
    response = client.get("/")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "Repo Guide" in response.text


def test_page_assets_are_served():
    for path in ["/static/app.js", "/static/style.css"]:
        assert client.get(path).status_code == 200

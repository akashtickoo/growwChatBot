from fastapi.testclient import TestClient

from app.main import create_app


def test_healthz_reports_no_index_yet(clean_env):
    """EV-0-04: the server starts and /healthz answers."""
    client = TestClient(create_app())
    response = client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"index_loaded": False}

import pytest
from app import app

@pytest.fixture
def client():
    app.config["TESTING"] = True
    with app.test_client() as c:
        yield c

def test_root(client):
    assert client.get("/").status_code in (200, 302)

def test_dashboard(client):
    # non loggé: redirection login ou 401 selon ta conf
    assert client.get("/dashboard").status_code in (200, 302, 401)

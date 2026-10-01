from fastapi.testclient import TestClient

from wrist_notes.main import app

client = TestClient(app)
FAIL = {"ok": False, "message": "인증 실패"}


def test_health_needs_no_secret():
    assert client.get("/").json() == {"ok": True}


def test_missing_secret():
    r = client.post("/save", json={"text": "x"})
    assert r.status_code == 200
    assert r.json() == FAIL


def test_wrong_secret():
    r = client.post("/save", json={"text": "x"}, headers={"X-Secret": "wrong"})
    assert r.json() == FAIL


def test_non_ascii_secret_is_rejected_not_crashed():
    r = client.post("/save", json={"text": "x"}, headers={"X-Secret": "틀림".encode()})
    assert r.json() == FAIL

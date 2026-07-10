import json

import pytest

import server


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "EXPORT_DIR", tmp_path)
    monkeypatch.setattr(server, "TOKEN", "")
    server.app.config["TESTING"] = True
    with server.app.test_client() as c:
        yield c


def test_healthz_returns_ok(client):
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.get_json() == {"status": "ok"}


def test_post_writes_file_named_by_date(client, tmp_path):
    payload = {"date": "2026-07-09", "sleep": {"start": "2026-07-09T01:00:00-04:00", "core_min": 300}}
    resp = client.post("/health", json=payload)
    assert resp.status_code == 200
    assert resp.get_json() == {"stored": "2026-07-09"}
    written = json.loads((tmp_path / "2026-07-09.json").read_text(encoding="utf-8"))
    assert written == payload


def test_post_overwrites_existing_file(client, tmp_path):
    (tmp_path / "2026-07-09.json").write_text('{"date": "2026-07-09", "old": true}', encoding="utf-8")
    resp = client.post("/health", json={"date": "2026-07-09", "vitals": {"resting_hr": 70}})
    assert resp.status_code == 200
    written = json.loads((tmp_path / "2026-07-09.json").read_text(encoding="utf-8"))
    assert "old" not in written


def test_post_rejects_invalid_date(client, tmp_path):
    resp = client.post("/health", json={"date": "../../etc/passwd"})
    assert resp.status_code == 400
    assert list(tmp_path.iterdir()) == []


def test_post_rejects_missing_date(client):
    assert client.post("/health", json={"sleep": {}}).status_code == 400


def test_post_rejects_non_object_body(client):
    assert client.post("/health", json=[1, 2]).status_code == 400
    assert client.post("/health", data="not json", content_type="application/json").status_code == 400


def test_post_requires_token_when_configured(client, monkeypatch):
    monkeypatch.setattr(server, "TOKEN", "s3cret")
    payload = {"date": "2026-07-09"}
    assert client.post("/health", json=payload).status_code == 401
    assert client.post("/health", json=payload,
                       headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert client.post("/health", json=payload,
                       headers={"Authorization": "Bearer s3cret"}).status_code == 200

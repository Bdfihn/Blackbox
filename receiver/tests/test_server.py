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


def test_post_writes_json_body_named_by_body_date(client, tmp_path):
    payload = {"date": "2026-07-09", "sleep": {"start": "2026-07-09T01:00:00-04:00", "core_min": 300}}
    resp = client.post("/health", json=payload)
    assert resp.status_code == 200
    assert resp.get_json() == {"stored": "2026-07-09"}
    written = json.loads((tmp_path / "2026-07-09.json").read_text(encoding="utf-8"))
    assert written == payload


def test_post_takes_date_from_any_header(client, tmp_path):
    resp = client.post("/health", data=b"raw sample lines",
                       headers={"X-Whatever-Name": "2026-07-09"})
    assert resp.status_code == 200
    assert (tmp_path / "2026-07-09.json").read_bytes() == b"raw sample lines"


def test_post_stores_multipart_file_upload(client, tmp_path):
    resp = client.post("/health",
                       data={"file": (__import__("io").BytesIO(b"sample export"), "samples.txt")},
                       content_type="multipart/form-data",
                       headers={"Date-Exported": "2026-07-09"})
    assert resp.status_code == 200
    assert (tmp_path / "2026-07-09.json").read_bytes() == b"sample export"


def test_post_overwrites_existing_file(client, tmp_path):
    (tmp_path / "2026-07-09.json").write_text('{"date": "2026-07-09", "old": true}', encoding="utf-8")
    resp = client.post("/health", json={"date": "2026-07-09", "vitals": {"resting_hr": 70}})
    assert resp.status_code == 200
    written = json.loads((tmp_path / "2026-07-09.json").read_text(encoding="utf-8"))
    assert "old" not in written


def test_post_locale_date_falls_back_to_server_date(client, tmp_path):
    from datetime import datetime

    resp = client.post("/health", json={"Date": "Jul 9, 2026 at 11:41 PM", "Sleep": ["sample"]})
    assert resp.status_code == 200
    today = datetime.now(server.LOCAL_TZ).strftime("%Y-%m-%d")
    assert resp.get_json() == {"stored": today}
    assert (tmp_path / f"{today}.json").exists()


def test_post_iso_date_in_body_value_is_extracted(client, tmp_path):
    resp = client.post("/health", json={"Date": "2026-07-09T23:41:00-04:00", "Sleep": []})
    assert resp.status_code == 200
    assert resp.get_json() == {"stored": "2026-07-09"}


def test_post_traversal_date_never_reaches_filename(client, tmp_path):
    resp = client.post("/health", json={"date": "../../etc/passwd"})
    assert resp.status_code == 200
    names = [p.name for p in tmp_path.iterdir()]
    assert all(server._DATE_RE.fullmatch(n.removesuffix(".json")) for n in names)


def test_post_rejects_empty_body(client):
    assert client.post("/health", data=b"", headers={"X-Date": "2026-07-09"}).status_code == 400


def test_post_requires_token_when_configured(client, monkeypatch):
    monkeypatch.setattr(server, "TOKEN", "s3cret")
    payload = {"date": "2026-07-09"}
    assert client.post("/health", json=payload).status_code == 401
    assert client.post("/health", json=payload,
                       headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert client.post("/health", json=payload,
                       headers={"Authorization": "Bearer s3cret"}).status_code == 200

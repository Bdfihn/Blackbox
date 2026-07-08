import server


def test_get_diary_rejects_invalid_date():
    resp = server.app.test_client().get("/api/diary/not-a-date")
    assert resp.status_code == 400


def test_get_diary_404_when_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(server, "DIARY_DIR", tmp_path)
    resp = server.app.test_client().get("/api/diary/2026-01-01")
    assert resp.status_code == 404


def test_get_diary_returns_content(monkeypatch, tmp_path):
    monkeypatch.setattr(server, "DIARY_DIR", tmp_path)
    (tmp_path / "2026-01-01.md").write_text("# 2026-01-01\n\nSlept in.\n", encoding="utf-8")

    resp = server.app.test_client().get("/api/diary/2026-01-01")

    assert resp.status_code == 200
    assert resp.get_json() == {"date": "2026-01-01", "content": "# 2026-01-01\n\nSlept in.\n"}


def test_list_diary_returns_dates_newest_first(monkeypatch, tmp_path):
    monkeypatch.setattr(server, "DIARY_DIR", tmp_path)
    (tmp_path / "2026-01-01.md").write_text("a", encoding="utf-8")
    (tmp_path / "2026-01-02.md").write_text("b", encoding="utf-8")

    resp = server.app.test_client().get("/api/diary")

    assert resp.get_json() == {"entries": ["2026-01-02", "2026-01-01"]}


def test_query_requires_question():
    resp = server.app.test_client().post("/api/query", json={"question": "  "})
    assert resp.status_code == 400

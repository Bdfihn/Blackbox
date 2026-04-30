from unittest.mock import MagicMock
import pytest


@pytest.fixture
def fake_insightface(monkeypatch):
    """Patch _load_app so InsightFace models are never loaded in tests."""
    import numpy as np
    mock_app = MagicMock()
    monkeypatch.setattr("sources.face_index._load_app", lambda: mock_app)
    fake_img = np.zeros((100, 100, 3), dtype=np.uint8)
    monkeypatch.setattr("sources.face_index.cv2.imread", lambda path: fake_img)
    return mock_app

import sys
from unittest.mock import MagicMock, patch
import numpy as np
import pytest


@pytest.fixture
def fake_insightface(monkeypatch):
    """Patch _load_app and cv2.imread so InsightFace models are never loaded in tests."""
    mock_app = MagicMock()
    monkeypatch.setattr("sources.face_index._load_app", lambda: mock_app)
    fake_img = np.zeros((100, 100, 3), dtype=np.uint8)
    monkeypatch.setattr("sources.face_index.cv2.imread", lambda path: fake_img)
    return mock_app

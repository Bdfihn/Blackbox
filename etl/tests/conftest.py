import sys
from unittest.mock import MagicMock
import pytest


@pytest.fixture
def fake_face_recognition(monkeypatch):
    """Inject a mock face_recognition module so dlib models are never loaded."""
    mock = MagicMock()
    monkeypatch.setitem(sys.modules, "face_recognition", mock)
    return mock

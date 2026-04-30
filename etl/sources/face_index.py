import logging
import os

import cv2
import numpy as np
from PIL import Image

log = logging.getLogger(__name__)

_IMAGE_EXTS = {'.jpg', '.jpeg', '.png'}
_MATCH_THRESHOLD = 0.4


def _imread(path: str):
    """Load any image format (including HEIC) as a BGR numpy array for cv2/InsightFace."""
    try:
        img = Image.open(path).convert('RGB')
        return cv2.cvtColor(np.array(img), cv2.COLOR_RGB2BGR)
    except Exception:
        return None


def _load_app():
    from insightface.app import FaceAnalysis
    app = FaceAnalysis(name='buffalo_l', providers=['CPUExecutionProvider'])
    app.prepare(ctx_id=0, det_size=(640, 640))
    return app


class FaceIndex:
    """Encodes reference faces at startup and identifies people in photos."""

    def __init__(self, faces_dir: str | None):
        self._encodings: dict[str, list] = {}
        self._app = None
        if faces_dir and os.path.isdir(faces_dir):
            self._load(faces_dir)
        elif faces_dir:
            log.info(f"Faces dir {faces_dir!r} not found — face recognition disabled")

    def _load(self, faces_dir: str) -> None:
        try:
            app = _load_app()
        except Exception as e:
            log.warning(f"InsightFace unavailable — face recognition disabled: {e}")
            return

        self._app = app

        for person in sorted(os.listdir(faces_dir)):
            person_dir = os.path.join(faces_dir, person)
            if not os.path.isdir(person_dir):
                continue
            encodings = []
            for fname in sorted(os.listdir(person_dir)):
                ext = os.path.splitext(fname)[1].lower()
                if not ext:
                    log.warning(f"Skipping reference file with no extension: {os.path.join(person_dir, fname)}")
                    continue
                if ext not in _IMAGE_EXTS:
                    log.warning(f"Skipping unsupported file type {ext!r}: {os.path.join(person_dir, fname)}")
                    continue
                fpath = os.path.join(person_dir, fname)
                try:
                    img = _imread(fpath)
                    if img is None:
                        log.warning(f"Could not read reference image {fpath}")
                        continue
                    faces = app.get(img)
                    if len(faces) != 1:
                        log.warning(f"Reference image has {len(faces)} faces, expected 1 — skipping {fpath}")
                        continue
                    emb = faces[0].embedding
                    if emb is None:
                        log.warning(f"No embedding returned for {fpath} — skipping")
                        continue
                    norm = np.linalg.norm(emb)
                    if norm == 0:
                        log.warning(f"Zero-norm embedding for {fpath} — skipping")
                        continue
                    encodings.append(emb / norm)
                except Exception as e:
                    log.warning(f"Failed to encode reference image {fpath}: {e}")
            if encodings:
                self._encodings[person] = encodings
                log.info(f"Face index: loaded {len(encodings)} encoding(s) for {person!r}")

    @property
    def empty(self) -> bool:
        return not self._encodings

    def identify(self, image_path: str) -> list[str]:
        if self.empty or self._app is None:
            return []
        try:
            img = _imread(image_path)
            if img is None:
                return []
            unknown_faces = self._app.get(img)
            if not unknown_faces:
                return []
            matched: set[str] = set()
            for face in unknown_faces:
                enc = face.embedding
                if enc is None:
                    continue
                norm = np.linalg.norm(enc)
                if norm == 0:
                    continue
                enc = enc / norm
                for person, ref_encs in self._encodings.items():
                    if any(float(np.dot(enc, ref)) >= _MATCH_THRESHOLD for ref in ref_encs):
                        matched.add(person)
            return sorted(matched)
        except Exception as e:
            log.warning(f"Face identification failed for {image_path}: {e}")
            return []

import importlib
import logging
import os
import sys
import types

log = logging.getLogger(__name__)

_IMAGE_EXTS = {'.jpg', '.jpeg', '.png'}


def _ensure_pkg_resources() -> None:
    """Inject a minimal pkg_resources shim if setuptools didn't expose it.

    face_recognition_models.__init__ does `from pkg_resources import resource_filename`
    which fails on Python 3.12-slim when setuptools >= 80 omits the top-level module.
    """
    if 'pkg_resources' in sys.modules:
        return
    try:
        import pkg_resources  # noqa: F401
        return
    except ImportError:
        pass

    def _resource_filename(pkg_name: str, resource_name: str) -> str:
        m = importlib.import_module(pkg_name)
        return os.path.join(os.path.dirname(m.__file__), resource_name)

    shim = types.ModuleType('pkg_resources')
    shim.resource_filename = _resource_filename  # type: ignore[attr-defined]
    sys.modules['pkg_resources'] = shim


class FaceIndex:
    """Encodes reference faces at startup and identifies people in photos."""

    def __init__(self, faces_dir: str | None):
        self._encodings: dict[str, list] = {}
        if faces_dir and os.path.isdir(faces_dir):
            self._load(faces_dir)
        elif faces_dir:
            log.info(f"Faces dir {faces_dir!r} not found — face recognition disabled")

    def _load(self, faces_dir: str) -> None:
        _ensure_pkg_resources()
        try:
            import face_recognition
        except (ImportError, SystemExit):
            log.warning("face_recognition unavailable — face recognition disabled")
            return

        for person in sorted(os.listdir(faces_dir)):
            person_dir = os.path.join(faces_dir, person)
            if not os.path.isdir(person_dir):
                continue
            encodings = []
            for fname in sorted(os.listdir(person_dir)):
                if os.path.splitext(fname)[1].lower() not in _IMAGE_EXTS:
                    continue
                fpath = os.path.join(person_dir, fname)
                try:
                    img = face_recognition.load_image_file(fpath)
                    encs = face_recognition.face_encodings(img)
                    if encs:
                        encodings.append(encs[0])
                    else:
                        log.warning(f"No face detected in reference image {fpath}")
                except Exception as e:
                    log.warning(f"Failed to encode reference image {fpath}: {e}")
            if encodings:
                self._encodings[person] = encodings
                log.info(f"Face index: loaded {len(encodings)} encoding(s) for {person!r}")

    @property
    def empty(self) -> bool:
        return not self._encodings

    def identify(self, image_path: str) -> list[str]:
        if self.empty:
            return []
        try:
            import face_recognition
        except (ImportError, SystemExit):
            return []
        try:
            img = face_recognition.load_image_file(image_path)
            unknown_encs = face_recognition.face_encodings(img)
            if not unknown_encs:
                return []
            matched: set[str] = set()
            for unknown_enc in unknown_encs:
                for person, ref_encs in self._encodings.items():
                    if any(face_recognition.compare_faces(ref_encs, unknown_enc)):
                        matched.add(person)
            return sorted(matched)
        except Exception as e:
            log.warning(f"Face identification failed for {image_path}: {e}")
            return []

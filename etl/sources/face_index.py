import logging
import os

log = logging.getLogger(__name__)

_IMAGE_EXTS = {'.jpg', '.jpeg', '.png'}


class FaceIndex:
    """Encodes reference faces at startup and identifies people in photos."""

    def __init__(self, faces_dir: str | None):
        self._encodings: dict[str, list] = {}
        if faces_dir and os.path.isdir(faces_dir):
            self._load(faces_dir)
        elif faces_dir:
            log.info(f"Faces dir {faces_dir!r} not found — face recognition disabled")

    def _load(self, faces_dir: str) -> None:
        import face_recognition

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

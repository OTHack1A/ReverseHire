"""Extract the candidate's portrait photo from a CV file (PDF, DOCX or image).

Only face *detection* is used, to locate the picture: no recognition or
identification of any kind. Nothing is written to disk; the photo is returned
as PNG bytes in memory. Errors never propagate: they are logged without CV
content or file paths and the result is None.
"""

from __future__ import annotations

import io
import logging
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Callable

log = logging.getLogger(__name__)

IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".bmp", ".tif", ".tiff", ".webp"}
MIN_SIDE_PX = 60  # smaller images are icons
MAX_OUTPUT_SIDE = 512  # cap of the longest side of the returned photo
SCORE_THRESHOLD = 0.5  # minimum heuristic score to accept a candidate
PAGE_SCAN_COVERAGE = 0.5  # an image covering more of the page is a page scan
MAX_IMAGES_PER_PAGE = 40
MAX_DOCX_MEDIA = 40
RENDER_DPI = 150
COLOUR_SAMPLE = 64  # side of the thumbnail used to count distinct colours
CASCADE_FILE = "haarcascade_frontalface_default.xml"
YUNET_FILE = "face_detection_yunet_2023mar.onnx"
YUNET_SCORE = 0.8  # minimum face confidence of the YuNet detector

Box = tuple[int, int, int, int]
Detector = Callable[[object], "list[Box] | None"]


@dataclass
class Photo:
    png: bytes  # PNG-encoded image
    width: int
    height: int
    source: str  # "embedded" (image object inside PDF/DOCX) or "cropped"
    page: int  # 0-based page index (0 for DOCX/images)


@dataclass
class _Candidate:
    image: object  # PIL image (RGB)
    page: int
    position: tuple[float, float, float, float] | None = None  # x0, y0, x1, y1, top-left origin
    page_size: tuple[float, float] | None = None
    colours: int | None = None

    def score(self) -> float:
        value = portrait_score(self.image.width, self.image.height, colours=self.colours,
                               position=self.position, page_size=self.page_size)
        return value * (0.85 if self.page > 0 else 1.0)


# --- public API ---------------------------------------------------------------

def extract_photo(path, *, detector: Detector | None = None, max_pages: int = 2) -> Photo | None:
    """The portrait photo of the CV at ``path``, or None when none is found.

    ``detector`` is a callable ``(PIL.Image) -> list[(x, y, w, h)] | None``
    (None meaning detection unavailable); it defaults to :func:`find_faces`.
    """
    detect = detector if detector is not None else find_faces
    try:
        file = Path(path)
        if not file.is_file():
            return None
        suffix = file.suffix.lower()
        if suffix == ".pdf":
            return _from_pdf(file, detect, max(1, int(max_pages)))
        if suffix == ".docx":
            return _choose_embedded(_docx_candidates(file), detect)
        if suffix in IMAGE_SUFFIXES:
            from PIL import Image

            with Image.open(file) as img:
                img.seek(0)
                page = _to_rgb(img)
            return _crop_from_page(page, detect, 0)
        return None
    except Exception as exc:  # noqa: BLE001 - never crash the caller
        log.info("photo extraction failed (%s)", type(exc).__name__)
        return None


def find_faces(image) -> list[Box] | None:
    """Face boxes ``(x, y, w, h)``: OpenCV's YuNet detector with the model bundled in
    ``cv/data`` (works with OpenCV 4.5.4+ and 5.x), else a Haar cascade when the
    installed OpenCV still ships one (4.x).

    None when OpenCV or a model is not available (or detection fails): the caller
    then uses the heuristics only.
    """
    faces = _yunet_faces(image)
    if faces is not None:
        return faces
    try:
        cascade = _cascade()
        if cascade is None:
            return None
        import numpy as np

        gray = image.convert("L")
        scale = 1.0
        longest = max(gray.size)
        if longest > 1024:
            scale = 1024 / longest
            gray = gray.resize((max(1, round(gray.width * scale)), max(1, round(gray.height * scale))))
        array = np.asarray(gray, dtype=np.uint8)
        min_face = max(24, min(array.shape[:2]) // 20)
        found = cascade.detectMultiScale(array, scaleFactor=1.1, minNeighbors=5,
                                         minSize=(min_face, min_face))
        return [tuple(int(round(v / scale)) for v in box) for box in list(found)]
    except Exception as exc:  # noqa: BLE001
        log.debug("face detection failed (%s)", type(exc).__name__)
        return None


def portrait_score(width, height, *, colours=None, position=None, page_size=None) -> float:
    """Heuristic likelihood (0..1) that an image is a portrait photo.

    ``colours``: distinct colours in a small sample of the image (few = logo).
    ``position``: placement box ``(x0, y0, x1, y1)`` on the page, top-left
    origin, in the same unit as ``page_size`` ``(width, height)``.
    """
    try:
        width, height = float(width), float(height)
    except (TypeError, ValueError):
        return 0.0
    if width <= 0 or height <= 0 or min(width, height) < MIN_SIDE_PX:
        return 0.0
    aspect = width / height
    if 0.6 <= aspect <= 1.1:
        aspect_s = 1.0
    elif 0.5 <= aspect < 0.6 or 1.1 < aspect <= 1.4:
        aspect_s = 0.5
    else:
        return 0.0  # banners, strips, page-like slices
    size_s = 1.0 if min(width, height) >= 100 else 0.6
    if max(width, height) > 4000:
        size_s *= 0.6

    if colours is None:
        colour_s = 0.7
    elif colours <= 8:
        colour_s = 0.1
    elif colours <= 32:
        colour_s = 0.35
    elif colours <= 96:
        colour_s = 0.7
    else:
        colour_s = 1.0

    position_s = 0.7
    if position is not None and page_size is not None:
        try:
            x0, y0, x1, y1 = (float(v) for v in position)
            page_w, page_h = (float(v) for v in page_size)
        except (TypeError, ValueError):
            page_w = page_h = 0.0
        if page_w > 0 and page_h > 0:
            coverage = abs(x1 - x0) * abs(y1 - y0) / (page_w * page_h)
            if coverage > PAGE_SCAN_COVERAGE:
                return 0.0  # a scanned page, not a photo
            centre = (y0 + y1) / 2 / page_h
            position_s = 1.0 if centre < 1 / 3 else 0.6 if centre < 2 / 3 else 0.4
    return round(aspect_s * size_s * (0.6 * colour_s + 0.4 * position_s), 4)


# --- choice -------------------------------------------------------------------

def _choose_embedded(candidates: list[_Candidate], detect: Detector) -> Photo | None:
    if not candidates:
        return None
    faces: list[list[Box] | None] = []
    for candidate in candidates:
        faces.append(_safe_detect(detect, candidate.image))
    single = [(c, f) for c, f in zip(candidates, faces) if f is not None and len(f) == 1]
    if single:
        candidate, (box,) = max(single, key=lambda cf: (cf[0].score(), _area(cf[0].image)))
        aspect = candidate.image.width / candidate.image.height
        if 0.5 <= aspect <= 1.4:
            return _photo(candidate.image, "embedded", candidate.page)
        # A wide or tall picture (e.g. a header) holding a face: cut the portrait out.
        return _photo(_portrait_crop(candidate.image, box), "cropped", candidate.page)
    for candidate in candidates:
        if candidate.colours is None:
            candidate.colours = _count_colours(candidate.image)
    scored = [(c.score(), _area(c.image), i) for i, c in enumerate(candidates)]
    best_score, _, index = max(scored)
    if best_score < SCORE_THRESHOLD:
        return None
    best = candidates[index]
    return _photo(best.image, "embedded", best.page)


def _crop_from_page(page, detect: Detector, page_index: int) -> Photo | None:
    faces = _safe_detect(detect, page)
    if not faces:
        return None
    width, height = page.size
    valid = [f for f in faces if f[2] > 0 and f[3] > 0 and f[0] < width and f[1] < height]
    if not valid:
        return None
    # Prefer large faces high on the page (the CV header).
    box = max(valid, key=lambda f: f[2] * f[3] * (1.0 if f[1] < height / 2 else 0.5))
    return _photo(_portrait_crop(page, box), "cropped", page_index)


def _portrait_crop(image, box: Box):
    """Crop a padded portrait (head and shoulders) around a face box, inside the image."""
    x, y, w, h = (int(v) for v in box)
    width, height = image.size
    cx = x + w / 2
    left = int(max(0, cx - w * 1.0))
    right = int(min(width, cx + w * 1.0))
    top = int(max(0, y - h * 0.6))
    bottom = int(min(height, y + h * 2.0))
    if right - left < 2 or bottom - top < 2:
        left, top, right, bottom = 0, 0, width, height
    return image.crop((left, top, right, bottom))


def _safe_detect(detect: Detector, image) -> list[Box] | None:
    try:
        result = detect(image)
    except Exception as exc:  # noqa: BLE001
        log.debug("face detector raised (%s)", type(exc).__name__)
        return None
    if result is None:
        return None
    return [tuple(int(v) for v in box[:4]) for box in result]


# --- PDF ----------------------------------------------------------------------

def _from_pdf(path: Path, detect: Detector, max_pages: int) -> Photo | None:
    from pypdf import PdfReader

    reader = PdfReader(str(path))
    if reader.is_encrypted:
        try:
            reader.decrypt("")
        except Exception:  # noqa: BLE001
            return None
    candidates: list[_Candidate] = []
    for index, page in enumerate(reader.pages[:max_pages]):
        candidates.extend(_pdf_page_candidates(page, index))
    usable = [c for c in candidates if not _is_page_scan(c)]
    photo = _choose_embedded(usable, detect)
    if photo is not None:
        return photo
    # Scanned page (or photo not stored as an image object): look for a face
    # on the rendered first page.
    page_image = _render_first_page(path)
    if page_image is None:
        scans = [c for c in candidates if c.page == 0 and _is_page_scan(c)]
        if not scans:
            return None
        page_image = max(scans, key=lambda c: _area(c.image)).image
    return _crop_from_page(page_image, detect, 0)


def _render_first_page(path: Path):
    try:
        from . import ocr

        pages = ocr.render_pdf_pages(path, [0], dpi=RENDER_DPI)
        image = pages.get(0) if isinstance(pages, dict) else None
        return _to_rgb(image) if image is not None else None
    except Exception as exc:  # noqa: BLE001
        log.debug("page rendering failed (%s)", type(exc).__name__)
        return None


def _pdf_page_candidates(page, index: int) -> list[_Candidate]:
    try:
        box = page.mediabox
        page_size = (float(box.width), float(box.height))
    except Exception:  # noqa: BLE001
        page_size = None
    try:
        placements = _image_placements(page, page_size)
    except Exception:  # noqa: BLE001 - position is optional
        placements = {}
    candidates = []
    try:
        images = list(page.images)[:MAX_IMAGES_PER_PAGE]
    except Exception:  # noqa: BLE001
        return []
    for item in images:
        try:
            image = _to_rgb(item.image)
        except Exception:  # noqa: BLE001 - unsupported filter or broken stream
            continue
        if min(image.size) < MIN_SIDE_PX:
            continue
        name = PurePosixPath(str(getattr(item, "name", ""))).stem
        position = placements.get(name)
        candidates.append(_Candidate(image, index, position, page_size if position else None))
    return candidates


def _image_placements(page, page_size) -> dict[str, tuple[float, float, float, float]]:
    """Placement boxes (top-left origin, points) of the page's image XObjects."""
    from pypdf.generic import ContentStream

    if page_size is None:
        return {}
    contents = page.get_contents()
    if contents is None:
        return {}
    resources = page.get("/Resources")
    resources = resources.get_object() if resources is not None else {}
    xobjects = resources.get("/XObject") if resources else None
    xobjects = xobjects.get_object() if xobjects is not None else {}
    stream = ContentStream(contents, page.pdf)
    ctm = [1.0, 0.0, 0.0, 1.0, 0.0, 0.0]
    stack: list[list[float]] = []
    origin_y = float(page.mediabox.top)
    origin_x = float(page.mediabox.left)
    found: dict[str, tuple[float, float, float, float]] = {}
    for operands, operator in stream.operations:
        if operator == b"q":
            stack.append(list(ctm))
        elif operator == b"Q":
            if stack:
                ctm = stack.pop()
        elif operator == b"cm" and len(operands) == 6:
            ctm = _mult([float(v) for v in operands], ctm)
        elif operator == b"Do" and operands:
            name = operands[0]
            try:
                xobject = xobjects[name].get_object()
            except (KeyError, TypeError, AttributeError):
                continue
            if xobject.get("/Subtype") != "/Image":
                continue
            xs, ys = [], []
            for ux, uy in ((0, 0), (1, 0), (0, 1), (1, 1)):
                xs.append(ux * ctm[0] + uy * ctm[2] + ctm[4])
                ys.append(ux * ctm[1] + uy * ctm[3] + ctm[5])
            found.setdefault(str(name).lstrip("/"), (
                min(xs) - origin_x, origin_y - max(ys), max(xs) - origin_x, origin_y - min(ys)))
    return found


def _mult(m1: list[float], m2: list[float]) -> list[float]:
    a1, b1, c1, d1, e1, f1 = m1
    a2, b2, c2, d2, e2, f2 = m2
    return [a1 * a2 + b1 * c2, a1 * b2 + b1 * d2,
            c1 * a2 + d1 * c2, c1 * b2 + d1 * d2,
            e1 * a2 + f1 * c2 + e2, e1 * b2 + f1 * d2 + f2]


def _is_page_scan(candidate: _Candidate) -> bool:
    if candidate.position is None or candidate.page_size is None:
        return False
    x0, y0, x1, y1 = candidate.position
    page_w, page_h = candidate.page_size
    if page_w <= 0 or page_h <= 0:
        return False
    return abs(x1 - x0) * abs(y1 - y0) / (page_w * page_h) > PAGE_SCAN_COVERAGE


# --- DOCX ---------------------------------------------------------------------

def _docx_candidates(path: Path) -> list[_Candidate]:
    from PIL import Image

    candidates = []
    with zipfile.ZipFile(path) as archive:
        names = [n for n in archive.namelist() if n.startswith("word/media/")]
        for name in sorted(names)[:MAX_DOCX_MEDIA]:
            try:
                data = archive.read(name)
                with Image.open(io.BytesIO(data)) as img:
                    img.seek(0)
                    image = _to_rgb(img)
            except Exception:  # noqa: BLE001 - not a raster image Pillow can read
                continue
            if min(image.size) >= MIN_SIDE_PX:
                candidates.append(_Candidate(image, 0))
    return candidates


# --- helpers ------------------------------------------------------------------

def _to_rgb(img):
    from PIL import Image

    if img.mode in ("RGBA", "LA", "PA") or (img.mode == "P" and "transparency" in img.info):
        rgba = img.convert("RGBA")
        background = Image.new("RGBA", rgba.size, "white")
        background.alpha_composite(rgba)
        return background.convert("RGB")
    return img.convert("RGB")


def _count_colours(image) -> int:
    from PIL import Image

    sample = image.copy()
    sample.thumbnail((COLOUR_SAMPLE, COLOUR_SAMPLE), Image.Resampling.NEAREST)
    colours = sample.getcolors(maxcolors=COLOUR_SAMPLE * COLOUR_SAMPLE)
    return len(colours) if colours is not None else COLOUR_SAMPLE * COLOUR_SAMPLE


def _area(image) -> int:
    return image.width * image.height


def _photo(image, source: str, page: int) -> Photo:
    from PIL import Image

    out = _to_rgb(image)
    if max(out.size) > MAX_OUTPUT_SIDE:
        out = out.copy()
        out.thumbnail((MAX_OUTPUT_SIDE, MAX_OUTPUT_SIDE), Image.Resampling.LANCZOS)
    buffer = io.BytesIO()
    out.save(buffer, format="PNG", optimize=True)
    return Photo(buffer.getvalue(), out.width, out.height, source, page)


_CASCADE_CACHE: list = []  # [classifier or None] once resolved
_YUNET_CACHE: list = []    # [True] when the YuNet model can be built, [False] when not
_YUNET_LOCK = None


def yunet_model_path() -> Path:
    """The bundled YuNet face detection model (MIT, OpenCV Zoo; see ATTRIBUTIONS.md)."""
    return Path(__file__).with_name("data") / YUNET_FILE


def _yunet_faces(image) -> list[Box] | None:
    """Face boxes with cv2.FaceDetectorYN; None when it cannot run here."""
    global _YUNET_LOCK
    if _YUNET_CACHE and not _YUNET_CACHE[0]:
        return None
    try:
        import threading

        import cv2
        import numpy as np

        if _YUNET_LOCK is None:
            _YUNET_LOCK = threading.Lock()
            try:  # OpenCV 5 prints a harmless backend warning on stderr
                cv2.utils.logging.setLogLevel(cv2.utils.logging.LOG_LEVEL_ERROR)
            except Exception:  # noqa: BLE001
                pass
        model = yunet_model_path()
        if not model.is_file() or not hasattr(cv2, "FaceDetectorYN"):
            _YUNET_CACHE[:] = [False]
            return None
        rgb = image.convert("RGB")
        scale = 1.0
        longest = max(rgb.size)
        if longest > 1024:
            scale = 1024 / longest
            rgb = rgb.resize((max(1, round(rgb.width * scale)), max(1, round(rgb.height * scale))))
        array = np.ascontiguousarray(np.asarray(rgb, dtype=np.uint8)[:, :, ::-1])  # BGR
        height, width = array.shape[:2]
        with _YUNET_LOCK:  # one detector per call: the input size changes with the image
            detector = cv2.FaceDetectorYN.create(str(model), "", (width, height), YUNET_SCORE, 0.3, 50)
            _ok, found = detector.detect(array)
        _YUNET_CACHE[:] = [True]
        boxes: list[Box] = []
        min_side = max(12, min(width, height) // 20)
        for row in ([] if found is None else found):
            x, y, w, h = (float(v) for v in row[:4])
            if w >= min_side and h >= min_side:
                boxes.append(tuple(int(round(v / scale)) for v in (x, y, w, h)))
        return boxes
    except Exception as exc:  # noqa: BLE001 - OpenCV missing, too old or broken
        log.debug("YuNet face detector unavailable (%s)", type(exc).__name__)
        _YUNET_CACHE[:] = [False]
        return None


def _cascade():
    if _CASCADE_CACHE:
        return _CASCADE_CACHE[0]
    classifier = None
    try:
        import cv2

        if not hasattr(cv2, "CascadeClassifier"):  # OpenCV 5 moved the cascades out
            raise ImportError("no CascadeClassifier in this OpenCV")

        folders = []
        data = getattr(cv2, "data", None)
        if data is not None and getattr(data, "haarcascades", None):
            folders.append(Path(data.haarcascades))
        folders.append(Path(cv2.__file__).parent)
        for folder in folders:
            direct = folder / CASCADE_FILE
            hits = [direct] if direct.is_file() else list(folder.rglob(CASCADE_FILE))[:1]
            if hits:
                loaded = cv2.CascadeClassifier(str(hits[0]))
                if not loaded.empty():
                    classifier = loaded
                    break
    except Exception as exc:  # noqa: BLE001 - OpenCV missing or broken
        log.debug("OpenCV face cascade unavailable (%s)", type(exc).__name__)
    if classifier is None:
        log.debug("no face cascade available, using heuristics only")
    _CASCADE_CACHE.append(classifier)
    return classifier

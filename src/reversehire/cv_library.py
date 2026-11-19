"""The user's CV library: one CV in several language VERSIONS ("My CV" tab).

English is meant to be the primary version, Italian the secondary; more can be
added (Ukrainian...). Each version is a COPY of the file under
`<data folder>/cv/<language>/`, so the library survives moving or deleting the
original; PDF, DOCX and images are accepted. The index is a JSON file,
`<data folder>/cv/library.json`, written atomically:

    {"version": 1, "versions": [{"id", "language", "label", "file_name", "stored",
      "sha256", "added", "updated", "primary", "photo"}]}

`stored` and `photo` are paths relative to the library folder. The photo of a
version (`cv/photo.py`) is kept next to its copy (never in logs, never sent).
No Qt here; every method is thread-safe. The data folder always comes from
`config.data_folder()`.

`compare_profiles` is the consistency check between two versions of the same
CV: it lists differences as (locale key, parameters) pairs.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)

FOLDER_NAME = "cv"
INDEX_NAME = "library.json"
INDEX_VERSION = 1
LANGUAGE_ORDER = ("en", "it", "uk")
PREFERRED_PRIMARY = "en"
MAX_LISTED = 8  # skills/periods listed in one difference


DELETE_RETRIES = 5        # a file still held for a moment (preview, antivirus): tried again
DELETE_RETRY_S = 0.1


class FileInUse(OSError):
    """A file of the library is held open by another program (or the app itself)."""


class LibraryError(Exception):
    """The file cannot be added (missing, unsupported format, copy failed)."""


@dataclass
class CVVersion:
    id: str
    language: str
    label: str
    file_name: str
    stored: str
    sha256: str = ""
    added: float = 0.0
    updated: float = 0.0
    primary: bool = False
    photo: str | None = None

    @classmethod
    def from_dict(cls, data: dict) -> "CVVersion":
        names = set(cls.__dataclass_fields__)
        return cls(**{k: v for k, v in data.items() if k in names})


def file_hash(path: str | Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_name(name: str) -> str:
    stem, suffix = os.path.splitext(name)
    stem = re.sub(r"[^\w\-. ]+", "_", stem).strip(" .") or "cv"
    return stem[:80] + suffix.lower()


def default_folder() -> Path:
    from .config import data_folder

    return data_folder() / FOLDER_NAME


class CVLibrary:
    """The versions of the user's CV, saved in the data folder."""

    def __init__(self, folder: str | Path | None = None):
        self.folder = Path(folder) if folder is not None else default_folder()
        self._lock = threading.RLock()
        self._versions: list[CVVersion] = []
        # Called with a file's path before the library deletes or moves it: the
        # viewers that keep it open (the My CV preview) close it.
        self.releasers: list = []
        self._load()

    # --- index -------------------------------------------------------------------

    @property
    def index_path(self) -> Path:
        return self.folder / INDEX_NAME

    def _load(self) -> None:
        try:
            data = json.loads(self.index_path.read_text(encoding="utf-8"))
            self._versions = [CVVersion.from_dict(v) for v in data.get("versions", []) if isinstance(v, dict)]
        except FileNotFoundError:
            self._versions = []
        except (OSError, ValueError, TypeError):
            log.warning("The CV library index cannot be read: starting empty", exc_info=True)
            self._versions = []

    def _save(self) -> None:
        self.folder.mkdir(parents=True, exist_ok=True)
        data = {"version": INDEX_VERSION, "versions": [asdict(v) for v in self._versions]}
        temporary = self.index_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(self.index_path)

    def reload(self) -> None:
        with self._lock:
            self._load()

    # --- reading -----------------------------------------------------------------

    def versions(self) -> list[CVVersion]:
        """Primary first, then by language (en, it, uk, others) and date added."""
        with self._lock:
            def order(v: CVVersion):
                rank = LANGUAGE_ORDER.index(v.language) if v.language in LANGUAGE_ORDER else len(LANGUAGE_ORDER)
                return (0 if v.primary else 1, rank, v.added)

            return sorted((CVVersion(**asdict(v)) for v in self._versions), key=order)

    def __len__(self) -> int:
        with self._lock:
            return len(self._versions)

    def get(self, version_id: str) -> CVVersion | None:
        with self._lock:
            found = next((v for v in self._versions if v.id == version_id), None)
            return CVVersion(**asdict(found)) if found else None

    def primary(self) -> CVVersion | None:
        versions = self.versions()
        return versions[0] if versions and versions[0].primary else (versions[0] if versions else None)

    def path_of(self, version: CVVersion | str) -> Path:
        version = self.get(version) if isinstance(version, str) else version
        if version is None:
            raise KeyError("Unknown CV version")
        return self.folder / version.stored

    def photo_path(self, version: CVVersion | str) -> Path | None:
        version = self.get(version) if isinstance(version, str) else version
        if version is None or not version.photo:
            return None
        path = self.folder / version.photo
        return path if path.is_file() else None

    def find_file(self, path: str | Path) -> CVVersion | None:
        """The version whose copy has the same content as `path` (or that IS `path`)."""
        try:
            target = Path(path).resolve()
        except OSError:
            return None
        for version in self.versions():
            try:
                if self.path_of(version).resolve() == target:
                    return version
            except OSError:
                pass
        try:
            digest = file_hash(path)
        except OSError:
            return None
        return next((v for v in self.versions() if v.sha256 == digest), None)

    # --- changes -----------------------------------------------------------------

    def _copy(self, source: Path, language: str) -> tuple[str, str]:
        """Copies `source` into <folder>/<language>/ (unique name); (relative path, sha256)."""
        from .cv.reading import SUPPORTED_FORMATS

        if not source.is_file():
            raise LibraryError("File not found")
        if source.suffix.lower() not in SUPPORTED_FORMATS:
            raise LibraryError("Unsupported format")
        target_folder = self.folder / language
        target_folder.mkdir(parents=True, exist_ok=True)
        name = _safe_name(source.name)
        target = target_folder / name
        stem, suffix = os.path.splitext(name)
        n = 2
        while target.exists():
            target = target_folder / f"{stem} ({n}){suffix}"
            n += 1
        try:
            shutil.copy2(source, target)
        except OSError as error:
            raise LibraryError("The file cannot be copied") from error
        return target.relative_to(self.folder).as_posix(), file_hash(target)

    def add(self, path: str | Path, language: str, label: str | None = None) -> CVVersion:
        """Adds a version (a copy of `path`). The first version becomes the primary
        one, and so does an English version when the primary is not English."""
        language = (language or "en").strip().lower()[:8] or "en"
        source = Path(path)
        with self._lock:
            stored, digest = self._copy(source, language)
            now = time.time()
            version = CVVersion(id=uuid.uuid4().hex[:12], language=language, label=(label or "").strip(),
                                file_name=source.name, stored=stored, sha256=digest, added=now, updated=now)
            current = next((v for v in self._versions if v.primary), None)
            if current is None or (language == PREFERRED_PRIMARY and current.language != PREFERRED_PRIMARY):
                for v in self._versions:
                    v.primary = False
                version.primary = True
            self._versions.append(version)
            self._save()
            return CVVersion(**asdict(version))

    def replace(self, version_id: str, path: str | Path) -> CVVersion:
        """Replaces the file of a version (the old copy and its photo are removed)."""
        with self._lock:
            version = next((v for v in self._versions if v.id == version_id), None)
            if version is None:
                raise KeyError("Unknown CV version")
            stored, digest = self._copy(Path(path), version.language)
            try:
                self._delete_files(version)
            except FileInUse as error:   # the index keeps the old file: drop the new copy
                self._unlink_quietly(stored)
                raise LibraryError("The old file is in use") from error
            version.stored, version.sha256, version.file_name = stored, digest, Path(path).name
            version.updated = time.time()
            version.photo = None
            self._save()
            return CVVersion(**asdict(version))

    def remove(self, version_id: str) -> None:
        """Removes a version and its copy; the primary goes to the next one (English first)."""
        with self._lock:
            version = next((v for v in self._versions if v.id == version_id), None)
            if version is None:
                return
            try:
                self._delete_files(version)   # first: a file in use leaves the index as it was
            except FileInUse as error:
                raise LibraryError("The file is in use") from error
            self._versions.remove(version)
            if version.primary and self._versions:
                successor = next((v for v in self._versions if v.language == PREFERRED_PRIMARY),
                                 self._versions[0])
                successor.primary = True
            self._save()

    def set_primary(self, version_id: str) -> None:
        with self._lock:
            if not any(v.id == version_id for v in self._versions):
                raise KeyError("Unknown CV version")
            for v in self._versions:
                v.primary = v.id == version_id
            self._save()

    def update(self, version_id: str, *, language: str | None = None, label: str | None = None) -> CVVersion:
        """Changes the label or the language (the copy moves to the language folder)."""
        with self._lock:
            version = next((v for v in self._versions if v.id == version_id), None)
            if version is None:
                raise KeyError("Unknown CV version")
            if label is not None:
                version.label = label.strip()
            if language and language != version.language:
                old = self.folder / version.stored
                stored, _digest = self._copy(old, language)
                try:
                    self._delete_files(version, keep_photo=True)
                except FileInUse as error:
                    self._unlink_quietly(stored)
                    raise LibraryError("The file is in use") from error
                version.stored, version.language = stored, language
            self._save()
            return CVVersion(**asdict(version))

    def save_photo(self, version_id: str, png: bytes) -> Path | None:
        """Keeps the photo of a version next to its copy (PNG)."""
        with self._lock:
            version = next((v for v in self._versions if v.id == version_id), None)
            if version is None or not png:
                return None
            relative = Path(version.stored).with_name(Path(version.stored).stem + ".photo.png").as_posix()
            target = self.folder / relative
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(png)
            except OSError:
                log.warning("The photo of a CV version cannot be saved", exc_info=True)
                return None
            version.photo = relative
            self._save()
            return target

    def _release(self, path: Path) -> None:
        for release in list(self.releasers):
            try:
                release(path)
            except Exception:  # noqa: BLE001 - a viewer that cannot close must not stop the change
                log.warning("A viewer did not release a CV file", exc_info=True)

    def _unlink(self, path: Path) -> None:
        """Deletes `path`, retrying briefly; FileInUse when it stays locked."""
        self._release(path)
        for attempt in range(DELETE_RETRIES):
            try:
                path.unlink(missing_ok=True)
                return
            except PermissionError as error:
                if attempt == DELETE_RETRIES - 1:
                    raise FileInUse(str(error)) from error
                time.sleep(DELETE_RETRY_S)

    def _unlink_quietly(self, relative: str) -> None:
        try:
            (self.folder / relative).unlink(missing_ok=True)
        except OSError:
            log.warning("A new copy in the CV library cannot be removed", exc_info=True)

    def _delete_files(self, version: CVVersion, keep_photo: bool = False) -> None:
        """Deletes the copy (FileInUse when it stays locked: nothing is changed then)
        and its photo (a photo that cannot be deleted is only logged)."""
        try:
            self._unlink(self.folder / version.stored)
        except FileInUse:
            # never the file name in the log: a CV file is often named after the person
            log.warning("A file of the CV library is in use and cannot be removed (%s file)",
                        Path(version.stored).suffix.lower() or "a")
            raise
        except OSError:
            log.warning("A file of the CV library cannot be removed", exc_info=True)
        if version.photo and not keep_photo:
            try:
                self._unlink(self.folder / version.photo)
            except OSError:
                log.warning("The photo of a CV version cannot be removed", exc_info=True)


# --- consistency between two versions -------------------------------------------


@dataclass
class Difference:
    """One difference: `key` is a locale key ("mycv.diff.*"), `params` its values."""

    key: str
    params: dict = field(default_factory=dict)


def _period(experience) -> str:
    start = (getattr(experience, "start", "") or "?")
    end = (getattr(experience, "end", "") or "?")
    return f"{start} – {end}"


def _names(items) -> dict[str, str]:
    """casefolded comparable name -> shown name (languages without the level)."""
    result = {}
    for item in items or []:
        value = getattr(item, "value", str(item))
        result.setdefault(value.casefold().strip(), value)
    return result


def _language_names(items) -> dict[str, str]:
    result = {}
    for item in items or []:
        value = getattr(item, "value", str(item))
        name = value.split("(")[0].strip()
        result.setdefault(name.casefold(), name)
    return result


def compare_profiles(a, b, label_a: str, label_b: str) -> list[Difference]:
    """Differences between the profiles of two versions of the same CV (both
    analysed with English values): jobs, date ranges, skills, languages, degrees.
    An empty list means the versions agree on all of these."""
    out: list[Difference] = []
    names = {"a": label_a, "b": label_b}

    jobs_a, jobs_b = list(getattr(a, "experiences", []) or []), list(getattr(b, "experiences", []) or [])
    if len(jobs_a) != len(jobs_b):
        out.append(Difference("mycv.diff.jobs", {**names, "count_a": len(jobs_a), "count_b": len(jobs_b)}))
    periods_a = {_period(e) for e in jobs_a}
    periods_b = {_period(e) for e in jobs_b}
    for only, here, there in ((sorted(periods_a - periods_b), "a", "b"), (sorted(periods_b - periods_a), "b", "a")):
        if only:
            out.append(Difference("mycv.diff.periods", {"version": names[here], "other": names[there],
                                                         "periods": ", ".join(only[:MAX_LISTED]),
                                                         "count": len(only)}))
    years_a, years_b = getattr(a, "years_experience", None), getattr(b, "years_experience", None)
    if years_a is not None and years_b is not None and abs(years_a - years_b) >= 1:
        out.append(Difference("mycv.diff.years", {**names, "years_a": f"{years_a:.1f}", "years_b": f"{years_b:.1f}"}))

    skills_a, skills_b = _names(getattr(a, "skills", [])), _names(getattr(b, "skills", []))
    if len(skills_a) != len(skills_b):
        out.append(Difference("mycv.diff.skills_count", {**names, "count_a": len(skills_a),
                                                          "count_b": len(skills_b)}))
    for only, missing_in in ((sorted(set(skills_a) - set(skills_b)), "b"), (sorted(set(skills_b) - set(skills_a)), "a")):
        if only:
            shown = [(skills_a if missing_in == "b" else skills_b)[k] for k in only]
            out.append(Difference("mycv.diff.skills_missing", {"version": names[missing_in],
                                                                "skills": ", ".join(shown[:MAX_LISTED]),
                                                                "count": len(shown)}))

    langs_a = _language_names(getattr(a, "spoken_languages", []))
    langs_b = _language_names(getattr(b, "spoken_languages", []))
    for only, missing_in in ((sorted(set(langs_a) - set(langs_b)), "b"), (sorted(set(langs_b) - set(langs_a)), "a")):
        if only:
            shown = [(langs_a if missing_in == "b" else langs_b)[k] for k in only]
            out.append(Difference("mycv.diff.languages_missing", {"version": names[missing_in],
                                                                   "languages": ", ".join(shown)}))
    levels_a = {getattr(i, "value", "").split("(")[0].strip().casefold(): getattr(i, "value", "")
                for i in getattr(a, "spoken_languages", []) or []}
    levels_b = {getattr(i, "value", "").split("(")[0].strip().casefold(): getattr(i, "value", "")
                for i in getattr(b, "spoken_languages", []) or []}
    for key in sorted(set(levels_a) & set(levels_b)):
        if levels_a[key].casefold() != levels_b[key].casefold():
            out.append(Difference("mycv.diff.language_level", {**names, "value_a": levels_a[key],
                                                                "value_b": levels_b[key]}))

    degrees_a, degrees_b = list(getattr(a, "degrees", []) or []), list(getattr(b, "degrees", []) or [])
    if len(degrees_a) != len(degrees_b):
        out.append(Difference("mycv.diff.degrees", {**names, "count_a": len(degrees_a),
                                                     "count_b": len(degrees_b)}))
    return out


def analyse_for_comparison(path: str | Path, store=None, settings=None):
    """The profile of a version (English values, no ESCO calls): blocking, worker only."""
    from .cv.extraction import profile_from_text
    from .cv.reading import read_document

    reading = read_document(path)
    return profile_from_text(reading.text, str(path), store, settings, use_esco=False)

"""Local (offline) machine translation of short CV texts into English.

Engine: OPUS-MT translation models (Helsinki-NLP, Marian architecture) exported
to ONNX and quantised (int8), run with `onnxruntime` and tokenised with the
`tokenizers` library: both are already dependencies of the application (local
embeddings and OCR), so translation adds no new package. CTranslate2 was tested
first and rejected: its compiled extension is unsigned and Windows Smart App
Control blocks it.

Nothing leaves the computer: the CV text is never sent to an online service.
The models are looked for, in order, in the folder passed to the functions, in
the folder named by the REVERSEHIRE_TRANSLATION_MODELS environment variable and
in `<data folder>/models/translation/`, one sub-folder per pair ("it-en",
"uk-en") with the files of `MODEL_FILES`. When a model is missing,
`translate` returns the texts unchanged and `is_available` is False: the
caller keeps the table-based translation and flags what stayed untranslated.
`download_models` (also `python -m reversehire.cv.translate download it uk`)
fetches the pinned files once; it is never called during a CV analysis.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
from collections.abc import Callable
from pathlib import Path

log = logging.getLogger(__name__)

ENV_FOLDER = "REVERSEHIRE_TRANSLATION_MODELS"
SUBFOLDER = Path("models") / "translation"
SUPPORTED = ("it", "uk")
MODEL_FILES = ("encoder_model_quantized.onnx", "decoder_model_quantized.onnx", "tokenizer.json",
               "config.json")
MAX_INPUT_TOKENS = 128
MAX_NEW_TOKENS = 96
BEAM_SIZE = 3
BATCH_SIZE = 1  # dynamic int8 quantisation scales over the whole batch: one text at a time keeps results stable

# Pinned sources (Hugging Face, Xenova ONNX exports of Helsinki-NLP/opus-mt-*):
# repository, revision and SHA-256 of every file.
SOURCES: dict[str, dict] = {
    "it": {
        "repo": "Xenova/opus-mt-it-en",
        "revision": "fd0b89b9c052adc1f2f64152f555aa17353728be",
        "files": {
            "onnx/encoder_model_quantized.onnx":
                "a025d2d85c90da23880116b67175e43e71921d8d2bf446b9f1eacdc86015aa4c",
            "onnx/decoder_model_quantized.onnx":
                "203bac7b98e438e65e1ddfcbec254ffb6811e6a8f4f71e93517ca9550323bf6e",
            "tokenizer.json": "c19e1dac8287ec5c5d47a0d81be5e0fd11755666b996c764ef4d13bb8117e3a2",
            "config.json": "4b82a23b7a91b213a934db70d0a33712a739fdd7c5686b02d89642d7dcfc7ac4",
        },
    },
    "uk": {
        "repo": "Xenova/opus-mt-uk-en",
        "revision": "b2ad6afa424aaeff8913ff350860b7a7b7c0a607",
        "files": {
            "onnx/encoder_model_quantized.onnx":
                "5e8c4c7e88742efa0a8e013f3897f17e29357d54b8c86a13472669546d6ed561",
            "onnx/decoder_model_quantized.onnx":
                "83b44c8e29a1667c010f9ce6a261c060f03d55f25b57715be7e148a090d8674e",
            "tokenizer.json": "48957126b17d9c51d0331a64da2761a83f359d5b1365d629bb1599ae64269530",
            "config.json": "40aceb41a0b2d5822100b08cf6b80f13a246e1104ec4bdf0904d62661b75063a",
        },
    },
}


class TranslationUnavailable(Exception):
    """The engine or the model of a language pair is missing or cannot be loaded."""


def pair_name(source_lang: str) -> str:
    return f"{source_lang}-en"


def models_folder(folder: str | Path | None = None) -> Path:
    """Folder that holds one sub-folder per language pair."""
    if folder:
        return Path(folder)
    explicit = os.environ.get(ENV_FOLDER)
    if explicit:
        return Path(explicit)
    from ..config import data_folder

    default = data_folder() / SUBFOLDER
    if not default.is_dir():
        # Same extra places as the scoring model: a models folder in a source
        # checkout or next to the installed program.
        try:
            from ..scoring.embedding import model_search_folders

            for models in model_search_folders():
                candidate = Path(models) / SUBFOLDER.name
                if candidate.is_dir():
                    return candidate
        except Exception:  # noqa: BLE001 - the default folder is always a valid answer
            pass
    return default


def model_path(source_lang: str, folder: str | Path | None = None) -> Path:
    return models_folder(folder) / pair_name(source_lang)


def is_installed(source_lang: str, folder: str | Path | None = None) -> bool:
    """True if all the model files of the pair are present (the engine is not loaded)."""
    if source_lang not in SUPPORTED:
        return False
    path = model_path(source_lang, folder)
    return all((path / name).is_file() for name in MODEL_FILES)


_lock = threading.Lock()
_engines: dict[tuple[str, str], "MarianOnnxTranslator"] = {}
_failed: set[tuple[str, str]] = set()


def get_engine(source_lang: str, folder: str | Path | None = None) -> "MarianOnnxTranslator":
    """The (shared, lazily loaded) engine of the pair; TranslationUnavailable if missing."""
    if source_lang not in SUPPORTED:
        raise TranslationUnavailable(f"No local translation model for '{source_lang}'.")
    path = model_path(source_lang, folder)
    key = (source_lang, str(path))
    with _lock:
        engine = _engines.get(key)
        if engine is not None:
            return engine
        if key in _failed:
            raise TranslationUnavailable(f"The translation model in {path} cannot be loaded.")
        if not is_installed(source_lang, folder):
            log.info("Local translation model %s not found in %s: fields that the built-in "
                     "tables do not cover stay in the CV language.", pair_name(source_lang), path)
            raise TranslationUnavailable(f"Translation model not found: {path}")
        try:
            log.info("Loading the local translation model %s from %s", pair_name(source_lang), path)
            engine = MarianOnnxTranslator(path)
        except Exception as e:  # noqa: BLE001 - a broken model must not stop the analysis
            _failed.add(key)
            log.warning("The local translation model %s cannot be loaded", path, exc_info=True)
            raise TranslationUnavailable(str(e)) from e
        _engines[key] = engine
        return engine


def is_available(source_lang: str, folder: str | Path | None = None) -> bool:
    """True if texts in `source_lang` can be translated locally right now."""
    try:
        get_engine(source_lang, folder)
    except TranslationUnavailable:
        return False
    return True


def translate(texts: list[str], source_lang: str, *, folder: str | Path | None = None) -> list[str]:
    """English translation of each text; the texts unchanged if the model is missing.

    English texts (or an unsupported language) are returned as they are. Blocking:
    about 0.1-0.3 s per short text on a laptop CPU; call it in a worker thread.
    """
    texts = list(texts)
    if not texts or source_lang == "en":
        return texts
    try:
        engine = get_engine(source_lang, folder)
    except TranslationUnavailable:
        return texts
    try:
        return engine.translate(texts)
    except Exception:  # noqa: BLE001 - never block the analysis
        log.warning("Local translation failed", exc_info=True)
        return texts


def reset() -> None:
    """Forget the loaded engines (tests, model folder changed)."""
    with _lock:
        _engines.clear()
        _failed.clear()


class MarianOnnxTranslator:
    """OPUS-MT (Marian) encoder-decoder in ONNX, beam search in numpy."""

    def __init__(self, path: Path, beam_size: int = BEAM_SIZE):
        import numpy as np
        import onnxruntime as ort
        from tokenizers import Tokenizer

        self._np = np
        config = json.loads((path / "config.json").read_text(encoding="utf-8"))
        self.eos = int(config.get("eos_token_id", 0))
        self.pad = int(config.get("pad_token_id", config.get("decoder_start_token_id", 0)))
        self.start = int(config.get("decoder_start_token_id", self.pad))
        self.beam_size = max(1, beam_size)
        self.tokenizer = Tokenizer.from_str(_compatible_tokenizer(path / "tokenizer.json"))
        options = ort.SessionOptions()
        options.intra_op_num_threads = min(4, os.cpu_count() or 1)
        options.log_severity_level = 3
        providers = ["CPUExecutionProvider"]
        self.encoder = ort.InferenceSession(str(path / "encoder_model_quantized.onnx"), options,
                                            providers=providers)
        self.decoder = ort.InferenceSession(str(path / "decoder_model_quantized.onnx"), options,
                                            providers=providers)
        self._decoder_inputs = {i.name for i in self.decoder.get_inputs()}
        self._run_lock = threading.Lock()

    def translate(self, texts: list[str]) -> list[str]:
        results = list(texts)
        todo = [(i, " ".join((t or "").split())) for i, t in enumerate(texts)]
        todo = [(i, t) for i, t in todo if t]
        with self._run_lock:
            for k in range(0, len(todo), BATCH_SIZE):
                chunk = todo[k:k + BATCH_SIZE]
                for (i, _), out in zip(chunk, self._translate_batch([t for _, t in chunk])):
                    results[i] = out
        return results

    def _ids(self, text: str) -> list[int]:
        ids = self.tokenizer.encode(text).ids[:MAX_INPUT_TOKENS]
        if not ids or ids[-1] != self.eos:
            ids = ids[: MAX_INPUT_TOKENS - 1] + [self.eos]
        return ids

    def _translate_batch(self, texts: list[str]) -> list[str]:
        """Beam search over several texts at once (all live beams grow by one token per step)."""
        np = self._np
        encoded = [self._ids(t) for t in texts]
        n, width = len(encoded), max(len(e) for e in encoded)
        input_ids = np.full((n, width), self.pad, dtype=np.int64)
        mask = np.zeros((n, width), dtype=np.int64)
        for i, ids in enumerate(encoded):
            input_ids[i, :len(ids)] = ids
            mask[i, :len(ids)] = 1
        hidden = self.encoder.run(None, {"input_ids": input_ids, "attention_mask": mask})[0]
        beams: list[list[tuple[list[int], float]]] = [[([self.start], 0.0)] for _ in range(n)]
        finished: list[list[tuple[list[int], float]]] = [[] for _ in range(n)]
        limits = [min(MAX_NEW_TOKENS, 2 * len(e) + 10) for e in encoded]
        active = list(range(n))
        step = 0
        while active:
            step += 1
            rows = [(i, b) for i in active for b in range(len(beams[i]))]
            index = [i for i, _ in rows]
            feed = {
                "input_ids": np.array([beams[i][b][0] for i, b in rows], dtype=np.int64),
                "encoder_hidden_states": hidden[index],
                "encoder_attention_mask": mask[index],
            }
            feed = {name: value for name, value in feed.items() if name in self._decoder_inputs}
            logits = self.decoder.run(["logits"], feed)[0][:, -1, :].astype(np.float64)
            logits[:, self.pad] = -np.inf  # never generate the padding token
            logits -= logits.max(axis=1, keepdims=True)
            logp = logits - np.log(np.exp(logits).sum(axis=1, keepdims=True))
            still = []
            for i in active:
                candidates: list[tuple[float, int, int]] = []
                for r, (row_text, b) in enumerate(rows):
                    if row_text != i:
                        continue
                    score = beams[i][b][1]
                    top = np.argpartition(-logp[r], self.beam_size)[: self.beam_size]
                    candidates.extend((score + float(logp[r, t]), b, int(t)) for t in top)
                candidates.sort(key=lambda c: -c[0])
                new_beams: list[tuple[list[int], float]] = []
                for score, b, token in candidates:
                    sequence = beams[i][b][0] + [token]
                    if token == self.eos:
                        finished[i].append((sequence, score / (len(sequence) - 1)))
                    else:
                        new_beams.append((sequence, score))
                    if len(new_beams) == self.beam_size:
                        break
                beams[i] = new_beams
                best_open = max((s / (len(q) - 1) for q, s in new_beams), default=-np.inf)
                done = (not new_beams or step >= limits[i]
                        or (len(finished[i]) >= self.beam_size and max(f[1] for f in finished[i]) >= best_open))
                if not done:
                    still.append(i)
            active = still
        results = []
        for i in range(n):
            pool = finished[i] or [(q, s / max(1, len(q) - 1)) for q, s in beams[i]]
            best = max(pool, key=lambda f: f[1])[0]
            tokens = [t for t in best[1:] if t not in (self.eos, self.pad)]
            results.append(" ".join(self.tokenizer.decode(tokens).split()))
        return results


def _compatible_tokenizer(path: Path) -> str:
    """tokenizer.json of the ONNX export, adapted to the `tokenizers` library.

    The export carries a "Precompiled" normaliser without its character map (it
    cannot be loaded) and the old Metaspace options: NFKC normalisation (what
    SentencePiece applies) and the current Metaspace options are equivalent.
    """
    data = json.loads(path.read_text(encoding="utf-8"))
    metaspace = {"type": "Metaspace", "replacement": "▁", "prepend_scheme": "always", "split": True}
    normalizer = data.get("normalizer") or {}
    if normalizer.get("type") == "Precompiled" and not normalizer.get("precompiled_charsmap"):
        data["normalizer"] = {"type": "NFKC"}
    pre = data.get("pre_tokenizer") or {}
    if pre.get("type") == "Sequence":
        pre["pretokenizers"] = [metaspace if p.get("type") == "Metaspace" else p
                                for p in pre.get("pretokenizers", [])]
    elif pre.get("type") == "Metaspace":
        data["pre_tokenizer"] = metaspace
    if (data.get("decoder") or {}).get("type") == "Metaspace":
        data["decoder"] = metaspace
    return json.dumps(data)


# --- download (explicit, never during an analysis) -------------------------------

Progress = Callable[[str, int, int], None]  # (file name, bytes done, bytes total)


def download_models(languages: list[str] | tuple[str, ...] = SUPPORTED, folder: str | Path | None = None,
                    progress: Progress | None = None, client=None) -> list[Path]:
    """Downloads the pinned model files of the pairs; returns the pair folders.

    Only model files are downloaded (no CV data is sent). Files already present
    are kept. A SHA-256 mismatch removes the file and raises ValueError.
    """
    import httpx

    from ..config import DEFAULT_USER_AGENT

    own_client = client is None
    client = client or httpx.Client(timeout=60, follow_redirects=True,
                                    headers={"User-Agent": DEFAULT_USER_AGENT})
    done = []
    try:
        for language in languages:
            source = SOURCES[language]
            target = model_path(language, folder)
            target.mkdir(parents=True, exist_ok=True)
            for remote, digest in source["files"].items():
                destination = target / Path(remote).name
                if destination.is_file():
                    continue
                url = f"https://huggingface.co/{source['repo']}/resolve/{source['revision']}/{remote}"
                log.info("Downloading the translation model file %s", url)
                partial = destination.with_suffix(destination.suffix + ".part")
                sha = hashlib.sha256()
                with client.stream("GET", url) as response:
                    response.raise_for_status()
                    total = int(response.headers.get("content-length") or 0)
                    received = 0
                    with partial.open("wb") as out:
                        for chunk in response.iter_bytes(1 << 20):
                            out.write(chunk)
                            sha.update(chunk)
                            received += len(chunk)
                            if progress:
                                progress(destination.name, received, total)
                if digest and sha.hexdigest() != digest:
                    partial.unlink(missing_ok=True)
                    raise ValueError(f"Checksum mismatch for {url}")
                partial.replace(destination)
            done.append(target)
    finally:
        if own_client:
            client.close()
    reset()
    return done


def _main(argv: list[str]) -> int:
    if len(argv) >= 1 and argv[0] == "download":
        languages = [a for a in argv[1:] if a in SUPPORTED] or list(SUPPORTED)
        logging.basicConfig(level=logging.INFO, format="%(message)s")
        for path in download_models(languages):
            print(path)
        return 0
    if len(argv) >= 2 and argv[0] == "test":
        print("\n".join(translate(argv[2:], argv[1])))
        return 0
    print("usage: python -m reversehire.cv.translate download [it] [uk] | test <it|uk> <text>...")
    return 2


if __name__ == "__main__":
    import sys

    raise SystemExit(_main(sys.argv[1:]))

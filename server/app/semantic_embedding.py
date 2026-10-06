from __future__ import annotations

import math
import os
import queue
import threading
import time
from dataclasses import asdict, dataclass
from typing import Literal, Protocol

from .foundation_utils import stable_hash
from .rag_chunking import hashed_embedding
from .settings import Settings, settings

EmbeddingMode = Literal["semantic_embedding", "hash_fallback", "lexical_only"]
_BACKEND_CACHE: dict[str, EmbeddingBackend] = {}
_BACKEND_CACHE_LOCK = threading.Lock()
_QUERY_CACHE: dict[tuple[str, str], EmbeddingBatch] = {}
_QUERY_CACHE_LOCK = threading.Lock()
_QUERY_INFLIGHT: dict[tuple[str, str], threading.Event] = {}
_EMBEDDING_SLOTS = threading.BoundedSemaphore(2)
EMBEDDING_SLOT_WAIT_SECONDS = max(0.0, float(os.environ.get("EMBEDDING_SLOT_WAIT_SECONDS", "5")))


@dataclass(frozen=True)
class EmbeddingConfig:
    provider: str
    model: str
    model_version: str
    dimensions: int
    normalization: bool
    batch_size: int
    device: str
    timeout_seconds: float
    fallback_policy: str
    query_prefix: str
    document_prefix: str

    @classmethod
    def from_settings(cls, source: Settings = settings) -> EmbeddingConfig:
        return cls(
            provider=source.embedding_provider,
            model=source.embedding_model,
            model_version=source.embedding_model_version,
            dimensions=source.embedding_dimensions,
            normalization=source.embedding_normalize,
            batch_size=source.embedding_batch_size,
            device=source.embedding_device,
            timeout_seconds=source.embedding_timeout_seconds,
            fallback_policy=source.embedding_fallback_policy,
            query_prefix=source.embedding_query_prefix,
            document_prefix=source.embedding_document_prefix,
        )

    @property
    def fingerprint(self) -> str:
        return stable_hash(asdict(self), length=24)


@dataclass(frozen=True)
class EmbeddingBatch:
    vectors: list[list[float]]
    mode: EmbeddingMode
    provider: str
    model: str
    model_version: str
    dimensions: int
    normalized: bool
    latency_ms: int
    fallback_reason: str = ""


class EmbeddingBackend(Protocol):
    def embed(self, texts: list[str], *, kind: Literal["query", "document"]) -> list[list[float]]: ...


class FastEmbedBackend:
    """Lazy local ONNX backend; importing the API never downloads a model."""

    def __init__(self, config: EmbeddingConfig) -> None:
        try:
            from fastembed import TextEmbedding
        except ImportError as exc:  # pragma: no cover - exercised through service fallback
            raise RuntimeError("fastembed_not_installed") from exc
        self._model = TextEmbedding(
            model_name=config.model,
            providers=None if config.device.lower() != "cpu" else ["CPUExecutionProvider"],
            threads=None,
            cuda=config.device.lower().startswith("cuda"),
        )
        self._inference_lock = threading.Lock()

    def embed(self, texts: list[str], *, kind: Literal["query", "document"]) -> list[list[float]]:
        del kind
        with self._inference_lock:
            return [list(map(float, vector)) for vector in self._model.embed(texts)]


class EmbeddingService:
    def __init__(self, config: EmbeddingConfig | None = None, backend: EmbeddingBackend | None = None) -> None:
        self.config = config or EmbeddingConfig.from_settings()
        self._backend = backend
        self._custom_backend = backend is not None
        self._backend_lock = threading.Lock()

    def encode_query(self, text: str) -> EmbeddingBatch:
        if self._custom_backend:
            return self._encode([text], kind="query")
        cache_key = (self.config.fingerprint, text)
        with _QUERY_CACHE_LOCK:
            cached = _QUERY_CACHE.get(cache_key)
            if cached is not None:
                return cached
            event = _QUERY_INFLIGHT.get(cache_key)
            owner = event is None
            if owner:
                event = threading.Event()
                _QUERY_INFLIGHT[cache_key] = event
        if not owner:
            event.wait(timeout=self.config.timeout_seconds)
            with _QUERY_CACHE_LOCK:
                cached = _QUERY_CACHE.get(cache_key)
            if cached is not None:
                return cached
            # The owner failed or timed out. A direct attempt will either use
            # an available backend slot or immediately take the configured
            # bounded fallback instead of joining a process-wide query queue.
            return self._encode([text], kind="query")
        try:
            batch = self._encode([text], kind="query")
            if batch.mode == "semantic_embedding":
                with _QUERY_CACHE_LOCK:
                    if len(_QUERY_CACHE) >= 256:
                        _QUERY_CACHE.pop(next(iter(_QUERY_CACHE)))
                    _QUERY_CACHE[cache_key] = batch
            return batch
        finally:
            with _QUERY_CACHE_LOCK:
                _QUERY_INFLIGHT.pop(cache_key, None)
                event.set()

    def encode_documents(self, texts: list[str]) -> EmbeddingBatch:
        return self._encode(texts, kind="document")

    def _encode(self, texts: list[str], *, kind: Literal["query", "document"]) -> EmbeddingBatch:
        if not texts:
            return self._batch([], "semantic_embedding", 0)
        prefix = self.config.query_prefix if kind == "query" else self.config.document_prefix
        prepared = [f"{prefix}{text}" for text in texts]
        started = time.monotonic()
        try:
            vectors = self._embed_with_timeout(prepared, kind=kind)
            vectors = [self._validate_and_normalize(vector) for vector in vectors]
            if len(vectors) != len(texts):
                raise RuntimeError("embedding_batch_size_mismatch")
            return self._batch(vectors, "semantic_embedding", started)
        except Exception as exc:
            reason = f"{type(exc).__name__}:{exc}"
            if self.config.fallback_policy == "lexical_only":
                return self._batch([[] for _ in texts], "lexical_only", started, reason)
            if self.config.fallback_policy != "hash_fallback":
                raise
            vectors = [_dense_hash(text, dimensions=self.config.dimensions) for text in texts]
            return self._batch(vectors, "hash_fallback", started, reason)

    def _embed_with_timeout(
        self,
        texts: list[str],
        *,
        kind: Literal["query", "document"],
    ) -> list[list[float]]:
        deadline = time.monotonic() + self.config.timeout_seconds
        if not _EMBEDDING_SLOTS.acquire(blocking=False):
            # Both slots busy usually means a concurrent question is already
            # mid-inference; wait briefly for a slot instead of degrading this
            # answer to hash_fallback. The wait stays well inside the deadline
            # and keeps the busy fallback for genuinely saturated backends.
            slot_wait = min(EMBEDDING_SLOT_WAIT_SECONDS, max(0.0, deadline - time.monotonic()))
            if not _EMBEDDING_SLOTS.acquire(timeout=slot_wait):
                raise RuntimeError("embedding_backend_busy")
        result_queue: queue.Queue[tuple[bool, object, float]] = queue.Queue(maxsize=1)

        def invoke() -> None:
            try:
                value: object = self._get_backend().embed(texts, kind=kind)
                ok = True
            except BaseException as exc:  # carried back to the request thread
                value = exc
                ok = False
            finally:
                completed_at = time.monotonic()
                result_queue.put((ok, value, completed_at))
                _EMBEDDING_SLOTS.release()

        worker = threading.Thread(
            target=invoke,
            name="semantic-embedding",
            daemon=True,
        )
        worker.start()
        try:
            remaining = max(0.0, deadline - time.monotonic())
            ok, value, completed_at = result_queue.get(timeout=remaining)
        except queue.Empty as exc:
            raise TimeoutError("embedding_timeout") from exc
        # Compare the worker's completion timestamp with the original deadline.
        # A busy caller can be descheduled between ``worker.start()`` and
        # ``Queue.get``; measuring only the wait would otherwise let an
        # over-deadline backend result appear successful.
        if completed_at > deadline:
            raise TimeoutError("embedding_timeout")
        if not ok:
            if isinstance(value, BaseException):
                raise value
            raise RuntimeError("embedding_backend_failed")
        return value  # type: ignore[return-value]

    def _get_backend(self) -> EmbeddingBackend:
        if self._backend is not None:
            return self._backend
        with self._backend_lock, _BACKEND_CACHE_LOCK:
            if self._backend is None:
                cached = _BACKEND_CACHE.get(self.config.fingerprint)
                if cached is not None:
                    self._backend = cached
                    return cached
                if self.config.provider != "fastembed":
                    raise RuntimeError(f"unsupported_embedding_provider:{self.config.provider}")
                self._backend = FastEmbedBackend(self.config)
                _BACKEND_CACHE[self.config.fingerprint] = self._backend
        return self._backend

    def _validate_and_normalize(self, vector: list[float]) -> list[float]:
        if len(vector) != self.config.dimensions:
            raise RuntimeError(f"embedding_dimension_mismatch:expected={self.config.dimensions},actual={len(vector)}")
        if not all(math.isfinite(value) for value in vector):
            raise RuntimeError("embedding_contains_non_finite_value")
        if not self.config.normalization:
            return vector
        norm = math.sqrt(sum(value * value for value in vector))
        if norm <= 0:
            raise RuntimeError("embedding_zero_norm")
        return [value / norm for value in vector]

    def _batch(
        self,
        vectors: list[list[float]],
        mode: EmbeddingMode,
        started: float | int,
        fallback_reason: str = "",
    ) -> EmbeddingBatch:
        latency_ms = 0 if not started else int((time.monotonic() - float(started)) * 1000)
        return EmbeddingBatch(
            vectors=vectors,
            mode=mode,
            provider=self.config.provider,
            model=self.config.model,
            model_version=self.config.model_version,
            dimensions=self.config.dimensions,
            normalized=self.config.normalization,
            latency_ms=latency_ms,
            fallback_reason=fallback_reason,
        )


def cosine_dense(left: list[float], right: list[float]) -> float:
    if not left or not right or len(left) != len(right):
        return 0.0
    return sum(a * b for a, b in zip(left, right, strict=True))


def _dense_hash(text: str, *, dimensions: int) -> list[float]:
    sparse = hashed_embedding(text, dimensions=dimensions)
    return [float(sparse.get(str(index), 0.0)) for index in range(dimensions)]

"""Lazily connected artifact storage for the station delivery path."""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any

from .base import ArtifactStore


class DeferredArtifactStore:
    """Connect and validate storage only when background delivery needs it."""

    def __init__(
        self,
        *,
        bucket: str,
        store_factory: Callable[[], ArtifactStore],
    ) -> None:
        self.bucket = bucket
        self.store_factory = store_factory
        self._store: ArtifactStore | None = None
        self._lock = threading.Lock()

    def _get_store(self) -> ArtifactStore:
        with self._lock:
            if self._store is None:
                candidate = self.store_factory()
                try:
                    candidate.validate_bucket(self.bucket)
                except Exception:
                    _close(candidate)
                    raise
                self._store = candidate
            return self._store

    def _call(self, method: str, *args: Any, **kwargs: Any) -> Any:
        store = self._get_store()
        try:
            return getattr(store, method)(*args, **kwargs)
        except Exception:
            with self._lock:
                if self._store is store:
                    self._store = None
            _close(store)
            raise

    def ensure_bucket(self, bucket: str) -> None:
        self._call("ensure_bucket", bucket)

    def validate_bucket(self, bucket: str) -> None:
        self._call("validate_bucket", bucket)

    def head(self, bucket: str, key: str):
        return self._call("head", bucket, key)

    def put_if_absent(self, reference, content: bytes):
        return self._call("put_if_absent", reference, content)

    def get_verified(self, reference, *, max_bytes: int | None = None) -> bytes:
        return self._call("get_verified", reference, max_bytes=max_bytes)


def _close(store: object) -> None:
    close = getattr(store, "close", None)
    if callable(close):
        try:
            close()
        except Exception:  # noqa: BLE001,S110 - best-effort SDK cleanup
            pass

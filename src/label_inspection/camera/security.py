"""Sanitize camera URLs before they enter logs or user-facing errors."""

from __future__ import annotations

from urllib.parse import urlsplit, urlunsplit


def resolve_camera_source(cli_source: str | None, configured_source: str | None) -> str:
    """Use an explicit CLI source first, then the environment-backed config."""

    source = (cli_source or configured_source or "").strip()
    if not source:
        raise ValueError("RTSP source is required via --source or VISION_RTSP_URL")
    return source


def mask_url_credentials(url: str) -> str:
    """Replace an embedded password while preserving the connection URL shape."""

    try:
        parsed = urlsplit(url)
    except ValueError:
        return "<invalid-camera-url>"
    if parsed.username is None or parsed.password is None:
        return url
    host = parsed.hostname or ""
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    if parsed.port is not None:
        host = f"{host}:{parsed.port}"
    netloc = f"{parsed.username}:***@{host}"
    return urlunsplit((parsed.scheme, netloc, parsed.path, parsed.query, parsed.fragment))

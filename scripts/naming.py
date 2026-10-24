"""Deterministic, ASCII-only names shared by every script."""
import re
import unicodedata


def slug(text):
    """'Dendrocopos major' -> 'dendrocopos-major' (lowercase ASCII, hyphens)."""
    s = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-") or "unknown"

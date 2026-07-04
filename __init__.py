try:
    from .adapter import register
except ImportError:  # pragma: no cover - direct pytest/import fallback
    from adapter import register

__all__ = ["register"]

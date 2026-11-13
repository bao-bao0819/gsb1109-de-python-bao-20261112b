"""Tools for mapping an existing Markdown sectioning onto a revised HTML manual."""

__all__ = ["Section", "extract_sections", "match_sections", "build_report"]


def __getattr__(name):
    if name in __all__:
        from . import remap
        return getattr(remap, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

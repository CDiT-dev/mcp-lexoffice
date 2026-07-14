"""mcp-lexoffice."""

from importlib.metadata import PackageNotFoundError, version as _pkg_version

try:
    # Single source of truth: the installed package version from pyproject.toml.
    # Avoids drift between this constant and the real release version.
    __version__ = _pkg_version("mcp-lexoffice")
except PackageNotFoundError:  # not installed (e.g. running from a raw checkout)
    __version__ = "0.0.0+unknown"

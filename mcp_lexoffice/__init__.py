"""mcp-lexoffice."""

import os
from importlib.metadata import PackageNotFoundError, version as _pkg_version

try:
    # Releases are git tags; the image carries the tag as APP_VERSION.
    # Outside an image, fall back to the installed package version.
    __version__ = os.environ.get("APP_VERSION") or _pkg_version("mcp-lexoffice")
except PackageNotFoundError:  # not installed (e.g. running from a raw checkout)
    __version__ = "0.0.0+unknown"

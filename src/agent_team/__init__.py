"""agent-team: the agentic engineering OS CLI."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("agent-team")
except PackageNotFoundError:  # running from a source tree without an install
    __version__ = "0.0.0+unknown"

__all__ = ["__version__"]

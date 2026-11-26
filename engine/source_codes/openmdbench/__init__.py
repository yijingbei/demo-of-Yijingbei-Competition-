"""Public package entry point for OpenMDBench."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("openmdbench")
except PackageNotFoundError:  # pragma: no cover - source tree without installation
    __version__ = "0.1.0"

__all__ = ["__version__"]

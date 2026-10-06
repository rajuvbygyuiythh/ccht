"""Project-root resolution for modules that were moved into subfolders.

``paths.py`` lives in ``entry/``; the project root is its parent directory.
External data (config.json, urls.txt, input/, output/, account_sessions/)
lives under ``<root>/data/``.
"""

from pathlib import Path


def project_root() -> Path:
    """Return the absolute path of the project root (parent of entry/)."""
    return Path(__file__).resolve().parent.parent


def data_dir() -> Path:
    """Return the absolute path of the external-data folder (<root>/data/)."""
    return project_root() / "data"
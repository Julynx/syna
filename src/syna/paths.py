"""Filesystem layout: bundled assets, repo root, and the user data home.

Installed from PyPI, Syna has no project folder: read-only assets ship
inside the package (`src/syna/assets/`) and are loaded through
`importlib.resources`; everything the user writes — `.env`,
`config.json`, logs, session dumps — lives in the Syna data home
(`~/.syna`, overridable with `SYNA_HOME` for tests and isolation).

Inside a repository checkout the repo root is used as the data home
fallback so development workflows (a `.env` in the repo root, a writable
tree) keep working unchanged.
"""

import os
import shutil
from functools import lru_cache
from importlib import resources
from pathlib import Path

ASSETS_PACKAGE = "syna.assets"

SEEDED_DATA_FILES = ("config.json",)


def get_project_root() -> Path:
    """Return the absolute path to the repository root.

    Only meaningful in a repository checkout; installed packages have no
    project folder and must use `get_data_home` / asset accessors.
    """
    return Path(__file__).resolve().parent.parent.parent


@lru_cache(maxsize=1)
def get_data_home() -> Path:
    """Return the Syna data home, creating and seeding it when needed.

    `$SYNA_HOME` wins when set; otherwise a repository checkout uses the
    repo root and an installed package uses `~/.syna`. Missing seeded
    files (`config.json`) are copied from the bundled defaults on first
    run; `.env` is left for the user or the setup wizard to create.
    """
    override = os.environ.get("SYNA_HOME")
    if override:
        home = Path(override)
    elif Path(get_project_root() / "pyproject.toml").is_file():
        home = get_project_root()
    else:
        home = Path.home() / ".syna"
    home.mkdir(parents=True, exist_ok=True)
    for file_name in SEEDED_DATA_FILES:
        target = home / file_name
        if not target.exists():
            shutil.copyfile(asset_path(file_name), target)
    return home


@lru_cache(maxsize=None)
def asset_path(name: str) -> Path:
    """Return the filesystem path of one bundled asset file or folder.

    For a package on disk this is the real path. For a zipped package the
    path is a temporary extraction that must outlive the caller, so its
    context is entered and kept open for the process lifetime; the
    interpreter cleans it up at exit. Results are cached per name: assets
    never change at runtime, and repeated requests must not re-extract
    or accumulate context managers.
    """
    traversable = resources.files(ASSETS_PACKAGE)
    for part in Path(name).parts:
        traversable = traversable / part
    context = resources.as_file(traversable)
    path = context.__enter__()
    _KEEP_ALIVE.append(context)
    return path


_KEEP_ALIVE: list = []


def read_asset_text(name: str) -> str:
    """Return the text of one bundled asset file."""
    traversable = resources.files(ASSETS_PACKAGE)
    for part in Path(name).parts:
        traversable = traversable / part
    return traversable.read_text(encoding="utf-8")

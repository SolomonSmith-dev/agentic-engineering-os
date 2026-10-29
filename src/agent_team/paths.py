"""Where the canonical files and the model registry live for this install.

`agent-team` is installed as a tool (`uv tool install`) and run from the target repository, so
its own `canonical/` and `routing/registry.yaml` cannot be found relative to the working
directory. The wheel carries a copy under the package; a source checkout uses the repository's.
"""

from importlib.resources import files
from pathlib import Path

CANONICAL_DIRNAME = "canonical"
REGISTRY_RELATIVE = Path("routing") / "registry.yaml"


def _source_checkout() -> Path | None:
    """The repository root when agent_team runs from its own source tree (editable install, tests)."""
    root = Path(__file__).resolve().parents[2]
    return root if (root / CANONICAL_DIRNAME).is_dir() and (root / REGISTRY_RELATIVE).is_file() else None


def bundled_root() -> Path | None:
    """The copy packed into the wheel under `agent_team/_bundled/`, when this is a built install."""
    try:
        bundled = Path(str(files("agent_team").joinpath("_bundled")))
    except (ModuleNotFoundError, TypeError):
        return None
    return bundled if (bundled / CANONICAL_DIRNAME).is_dir() else None


def default_canonical_dir() -> Path:
    """`./canonical` when the working directory holds one (a checkout of this repository), else the
    install's own copy. The caller may still pass `--canonical`."""
    cwd = Path.cwd() / CANONICAL_DIRNAME
    if cwd.is_dir():
        return cwd
    for root in (_source_checkout(), bundled_root()):
        if root is not None:
            return root / CANONICAL_DIRNAME
    return cwd  # does not exist; the caller's validation names it


def default_registry_for(canonical_dir: Path) -> Path:
    return canonical_dir.parent / REGISTRY_RELATIVE

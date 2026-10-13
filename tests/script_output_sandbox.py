"""Keep script defaults and derived aliases inside a test-owned directory."""

from contextlib import ExitStack, contextmanager
from pathlib import Path
from unittest.mock import patch


@contextmanager
def isolated_script_paths(root: Path, *modules):
    """Patch roots and precomputed path constants together, restoring on errors."""
    with ExitStack() as stack:
        for module in modules:
            original_root = module.PROJECT_ROOT
            for name, value in tuple(vars(module).items()):
                if name.isupper() and isinstance(value, Path):
                    relative_path = value.relative_to(original_root)
                    stack.enter_context(patch.object(module, name, root / relative_path))
        yield root

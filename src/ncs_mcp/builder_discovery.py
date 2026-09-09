"""Find the existing MCP deployment link without searching unrelated projects."""
from pathlib import Path
import json

from .builder_release import project_configuration


def discover_project(root: Path, state: Path) -> dict | None:
    candidates = []
    try:
        saved = json.loads((state / 'deployed.json').read_text(encoding='utf-8'))
        if isinstance(saved, dict) and saved.get('deploy_root'):
            candidates.append(Path(saved['deploy_root']))
    except (OSError, ValueError):
        pass
    candidates.append(root / 'deploy/vercel_mcp_app')
    for folder in candidates:
        try:
            config = project_configuration(folder)
            if not (folder / 'vercel.json').is_file():
                continue
            return config
        except (OSError, ValueError, RuntimeError, AttributeError):
            continue
    return None

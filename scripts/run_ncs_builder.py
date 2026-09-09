"""Windows desktop entry point for the versioned NCS data Builder."""
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from ncs_mcp.builder_desktop import main

if __name__ == "__main__":
    main()

"""Run from the checkout, independent of installed wheels and Codex."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from life_proactive.cli import main

if __name__ == "__main__":
    sys.exit(main())

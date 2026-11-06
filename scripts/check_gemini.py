"""Kept for backwards compatibility - use scripts/check_llm.py (works for every provider)."""
import runpy
from pathlib import Path

runpy.run_path(str(Path(__file__).with_name("check_llm.py")), run_name="__main__")

"""Developer server: separate data folder (.devdata) and port 8600, no tunnel.

Dev-only admin PIN used in .devdata: 482915 (delete .devdata to start fresh).
"""

import os
import sys
from pathlib import Path

here = Path(__file__).resolve().parent
os.environ.setdefault("QRPRINT_DATA_DIR", str(here / ".devdata"))
os.environ.setdefault("QRPRINT_PORT", "8600")
os.environ.setdefault("QRPRINT_VIRTUAL_DIR", str(here / ".devdata" / "printed"))
sys.argv += ["--no-browser", "--no-tunnel"]

import run  # noqa: E402

run.main()

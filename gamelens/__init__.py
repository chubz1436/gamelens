"""GameLens -- low-latency window capture and input harness for AI agents."""

from __future__ import annotations

# Ordering matters: DPI awareness must be established -- and verified -- before
# anything else in the package touches a Win32 window API. See gamelens/dpi.py.
from gamelens.dpi import DpiState, ensure_dpi_aware, require_trustworthy_dpi

DPI: DpiState = ensure_dpi_aware()

__version__ = "0.1.0"
__all__ = ["DPI", "DpiState", "ensure_dpi_aware", "require_trustworthy_dpi", "__version__"]

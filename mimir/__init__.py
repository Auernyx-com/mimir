"""Mimir — memory framework.

One schema, one API, one set of layer/consolidation/provenance mechanics.
Every consumer instantiates its own isolated instance from this package —
see SPEC.md. This package is never itself a running shared service.
"""

__version__ = "0.1.0"

LAYERS = ("root", "trunk", "branch", "leaf")

# Layers that require authorized_by + confirmation=True on write.
# See SPEC.md §3 (Layer mechanics) and §5 (API surface).
GATED_LAYERS = ("root", "trunk")

# Reconfirmations a leaf node needs before promotion to branch.
# See SPEC.md §10, decision 4 — matches Feneris's circuit-breaker convention.
DEFAULT_PROMOTION_THRESHOLD = 3

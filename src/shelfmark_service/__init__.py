"""Shelfmark's service-layer package.

The organizer remains in :mod:`main` for CLI compatibility.  Service modules
use it as a library while the parser and file-operation code are incrementally
split into smaller modules.
"""

__all__ = ["__version__"]
__version__ = "0.1.0"

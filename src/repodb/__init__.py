"""repodb - keep a SQLite database of git project clone URLs, and clone from it.

See `repodb.core` for the data model and command-line interface.
"""

from repodb.core import GitRepoDB, main, owner_of

__all__ = ["GitRepoDB", "main", "owner_of"]
__version__ = "0.1.1"

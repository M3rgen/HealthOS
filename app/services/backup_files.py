"""Deprecated since 0.2.2.1; remove in 0.2.4, after the 0.2.3 release."""

import sys

from app.services.operations import backup_files as _implementation

sys.modules[__name__] = _implementation

if __name__ == "__main__":
    sys.exit(_implementation.main(sys.argv[1:]))

import sys

from ._compat import check_python

# Must run before importing anything that uses 3.10+ syntax.
check_python()

from .main import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())

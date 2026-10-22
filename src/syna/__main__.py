"""Entrypoint for `python -m syna`."""

import sys

from . import server

if __name__ == "__main__":
    sys.exit(server.main())

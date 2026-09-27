"""Entry point of the PyInstaller build (see build.py); `sindex` itself is sindex.cli:main."""

import multiprocessing
import sys

if __name__ == "__main__":
    # the parsing workers are started with multiprocessing's "spawn": in the executable that starts this
    # program again with special arguments, which freeze_support() answers before anything else runs
    multiprocessing.freeze_support()
    from sindex.cli import main

    sys.exit(main())

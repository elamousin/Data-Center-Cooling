"""Launcher for the DAQ platform.

Equivalent to `python -m daq`; kept so the project can be started by running
this file directly from an editor.
"""

from daq.__main__ import main

if __name__ == "__main__":
    raise SystemExit(main())

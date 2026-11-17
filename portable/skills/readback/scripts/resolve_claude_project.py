#!/usr/bin/env python3
"""Read-only Claude adapter for the Readback project resolver."""

from project_registry import main

if __name__ == "__main__":
    raise SystemExit(main(fixed_host="claude"))

#!/usr/bin/env python3
"""Root entry point for the unified DataGenX validation CLI."""

import runpy


if __name__ == "__main__":
    runpy.run_module("datagenx.validation.validate", run_name="__main__")

#!/usr/bin/env python3
"""Root entry point for the DataGenX generation workflow."""

import runpy


if __name__ == "__main__":
    runpy.run_module("datagenx.orchestration.MasterRun", run_name="__main__")

#!/usr/bin/env python3
"""Root entry point for the DataGenX validation HTML report."""

import runpy


if __name__ == "__main__":
    runpy.run_module("datagenx.validation.validation_report", run_name="__main__")

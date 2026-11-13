#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Command line interface for polib's translation impact analysis.

Usage::

    po_impact before.po after.po mapping.csv
    po_impact before.po after.po mapping.csv --format json
"""

import sys

from polib import main


if __name__ == '__main__':
    sys.exit(main())

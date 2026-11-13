"""
pyexcel_io
~~~~~~~~~~~~~~~~~~~

Uniform interface for reading/writing different excel file formats

:copyright: (c) 2014-2026 by C Wang
:license: New BSD License, see LICENSE for more details
"""

import logging

import pyexcel_io.plugins as plugins

from .io import get_data, iget_data, save_data  # noqa
from .reconcile import (  # noqa
    ROW_CHANGED,
    ROW_CONFLICTED,
    ROW_MIGRATED,
    ROW_MISSING,
    ROW_NEW,
    ROW_UNCHANGED,
    ROW_UNTOUCHED,
    ReconcileResult,
    RowRecord,
    RowSource,
    extract_rows,
    reconcile_rows,
)
from ._compact import NullHandler

logging.getLogger(__name__).addHandler(NullHandler())  # noqa


BLACK_LIST = [__name__, "pyexcel_webio", "pyexcel_text"]
WHITE_LIST = [
    "pyexcel_io.readers",
    "pyexcel_io.writers",
    "pyexcel_io.database",
]
PREFIX_PATTERN = "^pyexcel_.*$"

plugins.load_plugins(
    PREFIX_PATTERN, __path__, BLACK_LIST, WHITE_LIST  # noqa: F821
)

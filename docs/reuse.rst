mrjob.reuse - reuse of intermediate step output
================================================

.. automodule:: mrjob.reuse

.. currentmodule:: mrjob.reuse

When a multi-step offline job is rerun, the first step -- reading and
splitting the original input -- is usually the most expensive. A
*reuse store* lets subsequent runs skip steps whose intermediate output
is still valid.

Enable it by passing ``reuse_store=<directory>`` to the ``inline`` or
``local`` runner. The directory persists across runs; mrjob never cleans
it up automatically. Each step gets its own sub-directory holding a
``manifest.json``, a ``shards.json``, and a private copy of every output
shard.

A step's cached output is reused only when the stored manifest matches
the current run on every dimension:

* the input manifest (the same input files, in the same order, each with
  the same size and SHA-256 checksum),
* the side tables the step reads (same working-directory names *and*
  content -- a side table with the same name but new content forces a
  recompute),
* the output protocol,
* the rendered step description, and
* a source fingerprint of the step's mapper/reducer/combiner functions
  (so two different functions that render identically are still told
  apart, while changing an unrelated downstream step leaves the upstream
  output reusable).

In addition, every shard recorded in ``shards.json`` must exist with
matching content. A partially completed step directory can never be
reused as if it were a complete input.

Per-step decisions are available programmatically from the runner and
from the command line (see :ref:`reuse-check-tool`).

Store and decisions
-------------------

.. autoclass:: ReuseStore
   :members:
.. autoclass:: StepReuseDecision
   :members:

Fingerprints
------------

.. autofunction:: fingerprint_file
.. autofunction:: fingerprint_side_input
.. autofunction:: fingerprint_directory
.. autofunction:: fingerprint_callable
.. autofunction:: fingerprint_step_logic
.. autofunction:: load_mrjob_class

Reason codes
------------

.. autodata:: REUSE
.. autodata:: RERUN
.. autodata:: REASON_NO_STORE
.. autodata:: REASON_MANIFEST_MISSING
.. autodata:: REASON_MANIFEST_CORRUPT
.. autodata:: REASON_INPUT_MISMATCH
.. autodata:: REASON_SIDE_INPUT_MISMATCH
.. autodata:: REASON_PROTOCOL_MISMATCH
.. autodata:: REASON_STEP_MISMATCH
.. autodata:: REASON_STEP_LOGIC_MISMATCH
.. autodata:: REASON_MISSING_SHARDS
.. autodata:: REASON_PREVIOUS_STEP_RERUN

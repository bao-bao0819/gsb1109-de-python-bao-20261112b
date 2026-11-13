mrjob.protocol - input and output
=================================
.. automodule:: mrjob.protocol


Strings
-------
.. py:class:: RawValueProtocol

    Just output ``value`` (a ``str``), and discard
    ``key`` (``key`` is read in as ``None``).

    **This is the default protocol used by jobs to read input.**

    This is an alias for :py:class:`RawValueProtocol` on Python 2 and
    :py:class:`TextValueProtocol` on Python 3.

.. autoclass:: BytesValueProtocol
.. autoclass:: TextValueProtocol

.. py:class:: RawProtocol

    Output ``key`` (``str``) and ``value`` (``str``),
    separated by a tab character.

    This is an alias for :py:class:`BytesProtocol` on Python 2 and
    :py:class:`TextProtocol` on Python 3.

.. autoclass:: BytesProtocol
.. autoclass:: TextProtocol


JSON
----
.. py:class:: JSONProtocol

   Encode ``(key, value)`` as two JSONs separated by a tab.

   **This is the default protocol used by jobs to write output and communicate
   between steps.**

   This is an alias for the first one of :py:class:`UltraJSONProtocol`,
   :py:class:`RapidJSONProtocol`, :py:class:`SimpleJSONProtocol`,
   or :py:class:`StandardJSONProtocol` for which the underlying library is
   available.

.. autoclass:: UltraJSONProtocol
.. autoclass:: RapidJSONProtocol
.. autoclass:: SimpleJSONProtocol
.. autoclass:: StandardJSONProtocol

.. py:class:: JSONValueProtocol

   Encode ``value`` as a JSON and discard ``key`` (``key`` is read in as
   ``None``).

   This is an alias for the first one of :py:class:`UltraJSONValueProtocol`,
   :py:class:`RapidJSONValueProtocol`, :py:class:`SimpleJSONValueProtocol`,
   or :py:class:`StandardJSONValueProtocol` for which the underlying library is
   available.

.. autoclass:: UltraJSONValueProtocol
.. autoclass:: RapidJSONValueProtocol
.. autoclass:: SimpleJSONValueProtocol
.. autoclass:: StandardJSONValueProtocol

Canonical JSON
--------------
Use these when mappers written in more than one language feed the same
reducer. Plain JSON keys group on raw bytes, so ``1`` and ``1.0``, or an
object with reordered members, land in different groups even though they
mean the same thing. The canonical protocols normalize semantically equal
keys to the same bytes before the shuffle.

The normalization contract (shared by every mapper language) is:

* object members are unordered; they are emitted sorted by Unicode
  code point
* arrays are ordered, so ``[1, 2]`` and ``[2, 1]`` stay separate
* a missing field is simply absent; objects with the same present
  fields group together
* integers and whole-valued floats merge (``1`` == ``1.0``)
* booleans are their own type: ``true`` never groups with ``1``
* strings are RFC 8259 JSON text encoded as UTF-8

Keys that cannot be normalized safely raise an error instead of grouping
wrong: duplicate object members (parsers disagree on which wins) and
numbers beyond source precision (distinct sources that rounding would
collapse). See :py:mod:`mrjob.canonical` for the quarantine helpers that
route such records back to their source partition.

.. autoclass:: CanonicalJSONProtocol
.. autoclass:: CanonicalJSONValueProtocol

Repr
----
.. autoclass:: ReprProtocol
.. autoclass:: ReprValueProtocol

Pickle
------
.. autoclass:: PickleProtocol
.. autoclass:: PickleValueProtocol

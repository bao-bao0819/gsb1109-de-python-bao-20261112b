mrjob.framing - length-delimited records and legacy migration
=============================================================

.. automodule:: mrjob.framing

Frames
------
.. autoclass:: Frame
    :members: key, value, raw

.. autofunction:: encode_frame
.. autofunction:: decode_frame
.. autofunction:: frame_length
.. autofunction:: split_frames
.. autofunction:: iter_frames
.. autofunction:: read_frames

.. autoclass:: FrameDecoder
    :members:

.. autoclass:: IncompleteFrameError
.. autoclass:: CorruptFrameError

Legacy protocols
----------------
.. autodata:: KEY_VALUE
.. autodata:: VALUE_ONLY

.. autofunction:: normalize_legacy_protocol
.. autofunction:: decode_legacy_record
.. autofunction:: encode_legacy_record

.. autoclass:: Calibration
.. autoclass:: LegacyCandidate
.. autoclass:: PartitionSpec

.. autofunction:: split_legacy_records

Migration
---------
.. autoclass:: MigrationItem
    :members:

.. autoclass:: MigrationResult
    :members:

.. autofunction:: migrate_partition
.. autofunction:: migrate_partition_stream
.. autofunction:: migrate_partitions

"""Stage one of the salted left join: gather per-key byte/row statistics.

Input files are plain text, one row per line, with the join key and the
row payload separated by a tab::

    <join_key>\\t<payload>

Which side (catalog or detail) a file belongs to is taken from its name:
files whose basename contains ``catalog`` are catalog input, everything
else is detail input. This mirrors how Hadoop Streaming exposes the
current input file through ``mapreduce.map.input.file``.

For every row the mapper emits one stat record, tagged with the exact
number of bytes the row will take when it is shuffled in stage two (the
salted join). Reducers sum rows and bytes per (side, key).
"""
from mrjob.compat import jobconf_from_env
from mrjob.job import MRJob
from mrjob.protocol import RawValueProtocol

from mrjob.examples.salted_join_plan import record_byte_size
from mrjob.examples.salted_join_plan import side_from_input_file


class MRSaltedJoinStats(MRJob):

    # input is "<key>\\t<payload>" text, not JSON
    INPUT_PROTOCOL = RawValueProtocol

    def mapper(self, _, line):
        if not line:
            return

        if isinstance(line, bytes):
            line = line.decode('utf_8')

        key, payload = line.split('\t', 1)
        side = side_from_input_file(
            jobconf_from_env('mapreduce.map.input.file'))

        # stage two shuffles (key, bucket) -> (side, payload); bucket 0
        # is used here only to get a representative per-row byte count
        byte_count = record_byte_size((key, 0), (side, payload))

        yield (side, key), (1, byte_count)

    def reducer(self, side_key, stats):
        side, key = side_key
        row_count = 0
        byte_count = 0

        for rows, bytes_for_rows in stats:
            row_count += rows
            byte_count += bytes_for_rows

        yield side_key, (row_count, byte_count)


if __name__ == '__main__':
    MRSaltedJoinStats.run()

"""Two-step user sessionization of page-view logs with mrjob.

Input lines are TSV:

    user_id<TAB>epoch_seconds<TAB>page<TAB>source_seq

Step 1 groups events by (user, UTC day) and merges them into *session
fragments*. The mapper emits one fragment per event, so the combiner and
reducer share a single associative merge operation and intermediate
results can be merged repeatedly (and recombined arbitrarily often on
any runner). Each fragment keeps the first/last page (ties on the same
second broken by source sequence number), the time span, and the set of
distinct pages seen.

Step 2 re-keys fragments by user and merges fragments across day
boundaries: two adjacent fragments belong to the same session iff the
gap between them does not exceed --session-gap seconds, so activity
spanning midnight stays in one session.

Duplicate boundary events (e.g. repeated in adjacent input shards) are
exact duplicates and collapse during the merge, and empty input
partitions simply contribute no fragments, so the result matches a
single pass over the full, globally sorted event stream.

Final output (JSON): user_id -> session summary with start/end
timestamps, first/last page, duration in seconds, and the deduplicated
visit (distinct page) count.
"""

from datetime import datetime, timezone

from mrjob.job import MRJob
from mrjob.protocol import JSONProtocol, RawValueProtocol
from mrjob.step import MRStep

DEFAULT_SESSION_GAP = 1800  # seconds
SECONDS_PER_DAY = 86400


def make_fragment(ts, seq, page):
    return {
        'start': ts,
        'end': ts,
        'first_page': page,
        'last_page': page,
        'first_seq': seq,
        'last_seq': seq,
        'pages': [page],
    }


def merge_fragments(fragments, gap):
    """Merge session fragments into sessions.

    Associative and idempotent: fragments may overlap (duplicated
    boundary events) and may be merged in any grouping, repeatedly.
    """
    merged = []
    for frag in sorted(fragments,
                       key=lambda f: (f['start'], f['first_seq'])):
        if merged and frag['start'] - merged[-1]['end'] <= gap:
            cur = merged[-1]
            if (frag['end'], frag['last_seq']) > (cur['end'],
                                                  cur['last_seq']):
                cur['end'] = frag['end']
                cur['last_page'] = frag['last_page']
                cur['last_seq'] = frag['last_seq']
            cur['pages'] = sorted(set(cur['pages']) | set(frag['pages']))
        else:
            merged.append({
                'start': frag['start'],
                'end': frag['end'],
                'first_page': frag['first_page'],
                'last_page': frag['last_page'],
                'first_seq': frag['first_seq'],
                'last_seq': frag['last_seq'],
                'pages': sorted(set(frag['pages'])),
            })
    return merged


def summarize(fragment):
    return {
        'start': fragment['start'],
        'end': fragment['end'],
        'first_page': fragment['first_page'],
        'last_page': fragment['last_page'],
        'duration': fragment['end'] - fragment['start'],
        'visits': len(fragment['pages']),
    }


class MRUserSessions(MRJob):

    INPUT_PROTOCOL = RawValueProtocol
    INTERNAL_PROTOCOL = JSONProtocol
    OUTPUT_PROTOCOL = JSONProtocol

    def configure_args(self):
        super(MRUserSessions, self).configure_args()
        self.add_passthru_arg(
            '--session-gap', type=int, default=DEFAULT_SESSION_GAP,
            help='Max idle seconds between adjacent events in one session'
                 ' (default: %d)' % DEFAULT_SESSION_GAP)

    def steps(self):
        return [
            MRStep(mapper=self.mapper_by_day,
                   combiner=self.combiner_by_day,
                   reducer=self.reducer_by_day),
            MRStep(mapper=self.mapper_by_user,
                   reducer=self.reducer_by_user),
        ]

    # Step 1: per-(user, day) session fragments

    def mapper_by_day(self, _, line):
        line = line.strip()
        if not line:
            return
        try:
            user, ts, page, seq = line.split('\t')
            ts = int(ts)
            seq = int(seq)
        except ValueError:
            self.increment_counter('mr_user_sessions', 'bad_lines', 1)
            return
        day = datetime.fromtimestamp(ts, tz=timezone.utc).strftime('%Y-%m-%d')
        yield (user, day), make_fragment(ts, seq, page)

    def combiner_by_day(self, key, fragments):
        for frag in merge_fragments(fragments, self.options.session_gap):
            yield key, frag

    def reducer_by_day(self, key, fragments):
        for frag in merge_fragments(fragments, self.options.session_gap):
            yield key, frag

    # Step 2: merge per-day fragments into user sessions

    def mapper_by_user(self, key, fragment):
        user, _day = key
        yield user, fragment

    def reducer_by_user(self, user, fragments):
        sessions = merge_fragments(fragments, self.options.session_gap)
        for session in sessions:
            yield user, summarize(session)


if __name__ == '__main__':
    MRUserSessions.run()

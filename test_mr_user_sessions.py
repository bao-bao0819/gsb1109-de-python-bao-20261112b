"""Tests for mr_user_sessions: sharded two-step results must match a
single pass over the full, globally sorted event stream."""

import random
from collections import defaultdict

import pytest

from mr_user_sessions import MRUserSessions

GAP = 1800


def reference_sessions(events, gap):
    """Plain one-pass sessionization over all events at once."""
    by_user = defaultdict(set)
    for user, ts, page, seq in events:
        by_user[user].add((ts, seq, page))
    result = {}
    for user, evs in by_user.items():
        sessions = []
        for ts, seq, page in sorted(evs):
            if sessions and ts - sessions[-1]['end'] <= gap:
                sessions[-1]['end'] = ts
                sessions[-1]['last_page'] = page
                sessions[-1]['pages'].add(page)
            else:
                sessions.append({'start': ts, 'end': ts,
                                 'first_page': page, 'last_page': page,
                                 'pages': {page}})
        result[user] = [
            {'start': s['start'], 'end': s['end'],
             'first_page': s['first_page'], 'last_page': s['last_page'],
             'duration': s['end'] - s['start'], 'visits': len(s['pages'])}
            for s in sessions
        ]
    return result


def run_job(paths, gap=GAP):
    job = MRUserSessions(['-r', 'inline', '--no-conf',
                          '--session-gap', str(gap)] + [str(p) for p in paths])
    with job.make_runner() as runner:
        runner.run()
        output = defaultdict(list)
        for user, summary in job.parse_output(runner.cat_output()):
            output[user].append(summary)
    return dict(output)


def write_shards(tmp_path, lines, num_shards, dup_boundary=2,
                 empty_shard_at=None):
    """Split sorted lines into shards, duplicating boundary lines between
    adjacent shards (as overlapping input splits would), optionally
    inserting an empty shard in the middle."""
    # contiguous chunks preserve time ordering like real splits
    size = (len(lines) + num_shards - 1) // num_shards
    chunks = [lines[i * size:(i + 1) * size] for i in range(num_shards)]
    shards = []
    for i, chunk in enumerate(chunks):
        if i > 0 and chunks[i - 1]:
            chunk = chunks[i - 1][-dup_boundary:] + chunk
        shards.append(chunk)
    if empty_shard_at is not None:
        shards.insert(empty_shard_at, [])
    paths = []
    for i, shard in enumerate(shards):
        path = tmp_path / ('part-%05d' % i)
        path.write_text(''.join(
            '%s\t%d\t%s\t%d\n' % e for e in shard))
        paths.append(path)
    return paths


def test_midnight_session_stays_together(tmp_path):
    midnight = (1600000000 // 86400 + 1) * 86400  # a UTC day boundary
    events = [
        ('u1', midnight - 60, '/home', 1),
        ('u1', midnight + 30, '/next-day', 2),
    ]
    paths = write_shards(tmp_path, events, num_shards=2)
    assert run_job(paths) == {'u1': [{
        'start': midnight - 60, 'end': midnight + 30,
        'first_page': '/home', 'last_page': '/next-day',
        'duration': 90, 'visits': 2,
    }]}


def test_gap_over_threshold_splits_session(tmp_path):
    events = [
        ('u1', 1000, '/a', 1),
        ('u1', 1000 + GAP, '/b', 2),      # exactly at threshold: same session
        ('u1', 1000 + GAP + GAP + 1, '/c', 3),  # over threshold: new session
    ]
    paths = write_shards(tmp_path, events, num_shards=1)
    sessions = run_job(paths)['u1']
    assert len(sessions) == 2
    assert sessions[0]['duration'] == GAP
    assert sessions[0]['last_page'] == '/b'
    assert sessions[1] == {'start': 1000 + 2 * GAP + 1,
                           'end': 1000 + 2 * GAP + 1,
                           'first_page': '/c', 'last_page': '/c',
                           'duration': 0, 'visits': 1}


def test_same_second_events_ordered_by_source_seq(tmp_path):
    events = [
        ('u1', 5000, '/second', 2),
        ('u1', 5000, '/first', 1),
    ]
    paths = write_shards(tmp_path, events, num_shards=2)
    sessions = run_job(paths)['u1']
    assert len(sessions) == 1
    assert sessions[0]['first_page'] == '/first'
    assert sessions[0]['last_page'] == '/second'
    assert sessions[0]['visits'] == 2


def test_empty_input(tmp_path):
    path = tmp_path / 'empty'
    path.write_text('')
    assert run_job([path]) == {}


@pytest.mark.parametrize('seed', range(5))
def test_sharded_matches_single_pass(tmp_path, seed):
    rng = random.Random(seed)
    base = 1600000000
    events = []
    for user in ('u1', 'u2', 'u3'):
        ts = base + rng.randrange(0, 3600)
        for seq in range(rng.randrange(5, 40)):
            ts += rng.randrange(0, 4 * GAP)  # mix of in-session and breaks
            page = '/p%d' % rng.randrange(5)  # revisits -> dedup visits
            events.append((user, ts, page, seq))
    events.sort(key=lambda e: (e[1], e[0], e[3]))

    paths = write_shards(tmp_path, events, num_shards=4,
                         dup_boundary=3, empty_shard_at=2)
    assert run_job(paths) == reference_sessions(events, GAP)

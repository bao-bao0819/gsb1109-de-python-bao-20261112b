"""End-to-end driver for the two-stage salted left join.

This wires together the statistics job, the bucket planner and the salted
join job, and reports both the chosen plan (expected bucket weights and
broadcast volume) and the actual shuffle bytes measured from counters.

It is a plain Python helper rather than an :py:class:`~mrjob.job.MRJob`
because the second stage needs a plan file that can only be built after
the first stage has run; on a real cluster the two jobs would be launched
from an orchestration script (or a scheduler such as Airflow) in the same
way.
"""
from mrjob.examples.mr_salted_join_stats import MRSaltedJoinStats
from mrjob.examples.mr_salted_left_join import MRSaltedLeftJoin
from mrjob.examples.salted_join_plan import aggregate_stats
from mrjob.examples.salted_join_plan import choose_buckets
from mrjob.examples.salted_join_plan import summarize_actual_buckets
from mrjob.examples.salted_join_plan import write_plan


def _collect_output(job, runner):
    return list(job.parse_output(runner.cat_output()))


def run_salted_left_join(input_paths, plan_path, max_buckets,
                         replication_budget=None, runner_alias='inline',
                         extra_args=None):
    """Run both stages and return the join output plus bucket reports.

    *input_paths* must include every catalog and detail input file; files
    with ``catalog`` in their name are treated as catalog input.
    *plan_path* is where the JSON bucket plan is written between stages.

    Returns a dict with:

    * ``join_output``: list of ``(key, (detail_payload, catalog_payload))``
      tuples (catalog payload is ``None`` for unmatched detail rows);
    * ``plan``: the chosen bucket plan, including expected bucket weights;
    * ``actual``: actual per-bucket bytes and broadcast volume.
    """
    extra_args = list(extra_args or [])

    stats_job = MRSaltedJoinStats(
        ['-r', runner_alias] + extra_args + list(input_paths))

    with stats_job.make_runner() as stats_runner:
        stats_runner.run()
        stat_records = _collect_output(stats_job, stats_runner)

    catalog_stats, detail_stats = aggregate_stats(stat_records)
    plan = choose_buckets(
        catalog_stats, detail_stats, max_buckets, replication_budget)
    write_plan(plan, plan_path)

    join_job = MRSaltedLeftJoin(
        ['-r', runner_alias, '--plan-file', plan_path] +
        extra_args + list(input_paths))

    with join_job.make_runner() as join_runner:
        join_runner.run()
        join_output = _collect_output(join_job, join_runner)
        actual = summarize_actual_buckets(join_runner.counters()[0])

    return {
        'join_output': join_output,
        'plan': plan,
        'actual': actual,
    }

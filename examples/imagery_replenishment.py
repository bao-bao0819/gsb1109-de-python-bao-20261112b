"""
Imagery replenishment planning along a corridor.

The imagery catalogue is collected per orbit segment (voyage), while the
business report checks coverage by mileage along the corridor. This example
builds a replayable replenishment plan that orders only what is still
missing, excludes cloud-obscured mileage and archived voyages, and reports
endpoints that no order can ever cover.

Run it like this:

    .. code:: console

            $ luigi --module examples.imagery_replenishment examples.ImageryReplenishmentExample --local-scheduler

Or call the planner directly:

    .. code:: console

            $ python -m examples.imagery_replenishment
"""

import json
import os
import tempfile

import luigi
from luigi.contrib.imagery_replenishment import ImageryReplenishmentTask, plan_replenishment

# Mileage intervals the report still considers uncovered.
REQUIRED_SEGMENTS = [
    [0.0, 40.0],
    [70.0, 120.0],
]

# Candidate voyages from the catalogue. ``cloud_gaps`` are mileage stretches
# inside the voyage footprint where clouds make the imagery unusable.
CANDIDATES = [
    {
        "voyage_id": "ORBIT-2026-01",
        "start": 0.0,
        "end": 80.0,
        "price": 120.0,
        "archived": False,
        "cloud_gaps": [[25.0, 35.0]],
    },
    {
        "voyage_id": "ORBIT-2026-02",
        "start": 20.0,
        "end": 120.0,
        "price": 150.0,
        "archived": False,
        "cloud_gaps": [],
    },
    {
        "voyage_id": "ORBIT-2019-LEGACY",
        "start": 0.0,
        "end": 120.0,
        "price": 30.0,
        "archived": True,
        "cloud_gaps": [],
    },
]


class ImageryReplenishmentExample(ImageryReplenishmentTask):
    task_namespace = "examples"

    required_segments = luigi.ListParameter(default=REQUIRED_SEGMENTS)
    candidates = luigi.ListParameter(default=CANDIDATES)
    output_path = luigi.PathParameter(default=os.path.join(tempfile.gettempdir(), "imagery_replenishment_plan.json"))


if __name__ == "__main__":
    plan = plan_replenishment(REQUIRED_SEGMENTS, CANDIDATES)
    print(json.dumps(plan.to_dict(), indent=2, sort_keys=True))
    luigi.run(["examples.ImageryReplenishmentExample", "--workers", "1", "--local-scheduler"])

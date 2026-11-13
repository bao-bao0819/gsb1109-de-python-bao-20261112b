# Copyright (c) 2026 Spotify AB
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may not
# use this file except in compliance with the License. You may obtain a copy of
# the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations under
# the License.

import json
import os
import tempfile
import unittest

from luigi.contrib.imagery_replenishment import (
    CandidateAnnotation,
    CandidateVoyage,
    ImageryReplenishmentTask,
    plan_replenishment,
)


def voyage(voyage_id, start, end, price, archived=False, cloud_gaps=()):
    return {
        "voyage_id": voyage_id,
        "start": start,
        "end": end,
        "price": price,
        "archived": archived,
        "cloud_gaps": list(cloud_gaps),
    }


class PlanReplenishmentTest(unittest.TestCase):
    def test_cloud_gap_is_not_coverage_and_becomes_unreachable(self):
        candidates = [
            voyage("V1", 0.0, 100.0, 50.0, cloud_gaps=[(40.0, 60.0)]),
        ]
        plan = plan_replenishment([(0.0, 100.0)], candidates)

        self.assertEqual(plan.orders[0].covered_segments, ((0.0, 40.0), (60.0, 100.0)))
        self.assertEqual(plan.total_price, 50.0)
        self.assertEqual(plan.unreachable_segments, ((40.0, 60.0),))
        self.assertFalse(plan.feasible)

    def test_archived_voyages_are_excluded(self):
        candidates = [
            voyage("sealed", 0.0, 100.0, 10.0, archived=True),
        ]
        plan = plan_replenishment([(0.0, 100.0)], candidates)

        self.assertEqual(plan.orders, ())
        self.assertEqual(plan.total_price, 0.0)
        self.assertEqual(plan.excluded_archived, ("sealed",))
        self.assertEqual(plan.unreachable_segments, ((0.0, 100.0),))
        self.assertEqual(
            plan.candidates,
            (CandidateAnnotation("sealed", 10.0, True, ((0.0, 100.0),)),),
        )

    def test_every_candidate_is_annotated_with_footprint_price_and_archive_state(self):
        candidates = [
            voyage("cloudy", 0.0, 100.0, 50.0, cloud_gaps=[(40.0, 60.0)]),
            voyage("sealed", 0.0, 100.0, 10.0, archived=True),
        ]
        plan = plan_replenishment([(0.0, 100.0)], candidates)

        by_id = {annotation.voyage_id: annotation for annotation in plan.candidates}
        self.assertEqual(
            by_id["cloudy"].coverage_segments,
            ((0.0, 40.0), (60.0, 100.0)),
        )
        self.assertEqual(by_id["cloudy"].price, 50.0)
        self.assertFalse(by_id["cloudy"].archived)
        self.assertTrue(by_id["sealed"].archived)
        self.assertEqual(by_id["sealed"].coverage_segments, ((0.0, 100.0),))

    def test_cheapest_combination_wins(self):
        candidates = [
            voyage("expensive", 0.0, 100.0, 90.0),
            voyage("left", 0.0, 60.0, 30.0),
            voyage("right", 50.0, 100.0, 40.0),
        ]
        plan = plan_replenishment([(0.0, 100.0)], candidates)

        self.assertEqual([order.voyage_id for order in plan.orders], ["left", "right"])
        self.assertEqual(plan.total_price, 70.0)
        self.assertEqual(plan.unreachable_segments, ())
        self.assertTrue(plan.feasible)

    def test_single_voyage_fills_several_gaps_but_is_ordered_once(self):
        candidates = [
            voyage("whole", 0.0, 100.0, 60.0),
            voyage("piece-a", 0.0, 30.0, 40.0),
            voyage("piece-b", 70.0, 100.0, 40.0),
        ]
        plan = plan_replenishment([(0.0, 30.0), (70.0, 100.0)], candidates)

        self.assertEqual([order.voyage_id for order in plan.orders], ["whole"])
        self.assertEqual(plan.orders[0].covered_segments, ((0.0, 30.0), (70.0, 100.0)))
        self.assertEqual(plan.total_price, 60.0)

    def test_dominated_voyage_is_skipped_without_losing_optimality(self):
        candidates = [
            voyage("narrow-pricey", 10.0, 40.0, 80.0),
            voyage("wide-cheap", 0.0, 100.0, 70.0),
        ]
        plan = plan_replenishment([(0.0, 100.0)], candidates)

        self.assertEqual([order.voyage_id for order in plan.orders], ["wide-cheap"])
        self.assertEqual(plan.total_price, 70.0)

    def test_partial_feasibility_reports_only_missing_endpoints(self):
        candidates = [
            voyage("V1", 0.0, 60.0, 25.0),
        ]
        plan = plan_replenishment([(0.0, 100.0)], candidates)

        self.assertEqual([order.voyage_id for order in plan.orders], ["V1"])
        self.assertEqual(plan.orders[0].covered_segments, ((0.0, 60.0),))
        self.assertEqual(plan.unreachable_segments, ((60.0, 100.0),))

    def test_adjacent_coverages_merge_and_overlap_is_free(self):
        candidates = [
            voyage("V1", 0.0, 55.0, 30.0),
            voyage("V2", 45.0, 100.0, 35.0),
        ]
        plan = plan_replenishment([(0.0, 100.0)], candidates)

        self.assertEqual(plan.total_price, 65.0)
        self.assertEqual(plan.unreachable_segments, ())
        self.assertEqual(
            [order.covered_segments for order in plan.orders],
            [((0.0, 55.0),), ((45.0, 100.0),)],
        )

    def test_dataclass_inputs_are_accepted(self):
        candidates = [
            CandidateVoyage("V1", 0.0, 50.0, 20.0),
            CandidateVoyage("V2", 50.0, 100.0, 25.0, cloud_gaps=((80.0, 90.0),)),
        ]
        plan = plan_replenishment(((0.0, 100.0),), candidates)

        self.assertEqual([order.voyage_id for order in plan.orders], ["V1", "V2"])
        self.assertEqual(plan.unreachable_segments, ((80.0, 90.0),))

    def test_invalid_inputs_are_rejected(self):
        with self.assertRaises(ValueError):
            plan_replenishment([(0.0, 10.0)], [voyage("V1", 0.0, 10.0, -1.0)])
        with self.assertRaises(ValueError):
            plan_replenishment(
                [(0.0, 10.0)],
                [voyage("V1", 0.0, 10.0, 5.0), voyage("V1", 0.0, 10.0, 5.0)],
            )
        with self.assertRaises(ValueError):
            plan_replenishment([(0.0, 10.0)], [voyage("V1", 0.0, 10.0, 5.0, cloud_gaps=[(5.0, 20.0)])])
        with self.assertRaises(TypeError):
            plan_replenishment([(0.0, 10.0)], [("not", "a", "mapping")])


class ImageryReplenishmentTaskTest(unittest.TestCase):
    def _build_task(self, output_path):
        return ImageryReplenishmentTask(
            required_segments=((0.0, 30.0), (70.0, 100.0)),
            candidates=(
                voyage("whole", 0.0, 100.0, 60.0),
                voyage("sealed", 0.0, 100.0, 5.0, archived=True),
                voyage("cloudy", 0.0, 100.0, 10.0, cloud_gaps=[(80.0, 85.0)]),
            ),
            output_path=output_path,
        )

    def test_run_writes_replayable_plan(self):
        with tempfile.TemporaryDirectory() as directory:
            output_path = os.path.join(directory, "plan.json")
            task = self._build_task(output_path)

            self.assertFalse(task.complete())
            task.run()
            self.assertTrue(task.complete())

            with open(output_path) as handle:
                first = handle.read()
            payload = json.loads(first)

            self.assertEqual([order["voyage_id"] for order in payload["orders"]], ["whole"])
            self.assertEqual(payload["total_price"], 60.0)
            self.assertEqual(payload["excluded_archived"], ["sealed"])
            self.assertEqual(payload["unreachable_segments"], [])
            self.assertEqual(
                {candidate["voyage_id"]: candidate["archived"] for candidate in payload["candidates"]},
                {"whole": False, "sealed": True, "cloudy": False},
            )
            cloudy = next(candidate for candidate in payload["candidates"] if candidate["voyage_id"] == "cloudy")
            self.assertEqual(cloudy["coverage_segments"], [[0.0, 80.0], [85.0, 100.0]])

            replay = self._build_task(output_path)
            replay.run()
            with open(output_path) as handle:
                second = handle.read()
            self.assertEqual(first, second)

    def test_run_records_unreachable_endpoints(self):
        with tempfile.TemporaryDirectory() as directory:
            output_path = os.path.join(directory, "plan.json")
            task = ImageryReplenishmentTask(
                required_segments=((0.0, 100.0),),
                candidates=(voyage("V1", 0.0, 40.0, 15.0, cloud_gaps=[(10.0, 20.0)]),),
                output_path=output_path,
            )
            task.run()

            with open(output_path) as handle:
                payload = json.load(handle)
            self.assertEqual(payload["unreachable_segments"], [[10.0, 20.0], [40.0, 100.0]])
            self.assertEqual(payload["orders"][0]["covered_segments"], [[0.0, 10.0], [20.0, 40.0]])
            self.assertFalse(payload["feasible"])


if __name__ == "__main__":
    unittest.main()

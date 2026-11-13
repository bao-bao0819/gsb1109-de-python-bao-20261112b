import pytest

from pyexcel_io.customer_remap import (
    Contract,
    Customer,
    CustomerMappingPair,
    CustomerRegistry,
    CustomerRemapService,
    build_change_groups,
    fingerprint_pairs,
)
from pyexcel_io.exceptions import (
    CustomerMappingError,
    CustomerNumberOccupiedError,
    DuplicateCustomerMappingError,
)

from .nose_tools import eq_, raises


def build_registry():
    registry = CustomerRegistry()
    registry.add_customer(Customer("c1", "A"))
    registry.add_customer(Customer("c2", "B"))
    registry.add_customer(Customer("c3", "C"))
    registry.add_contract(Contract("k1", "c1", "A"))
    registry.add_contract(Contract("k2", "c1", "A"))
    registry.add_contract(Contract("k3", "c2", "B"))
    registry.add_contract(Contract("k4", "c3", "C"))
    return registry


def test_swap_two_numbers_and_contracts_follow_customer():
    registry = build_registry()
    service = CustomerRemapService(registry)

    # a real exchange needs both directions so each customer is a
    # mapping subject; a lone A->B would treat c2 as an outside occupier
    pairs, subjects = service.parse_rows([("A", "B"), ("B", "A")])
    receipt = service.import_pairs(pairs, subjects)

    eq_(registry.get_customer("c1").number, "B")
    eq_(registry.get_customer("c2").number, "A")
    contracts = registry.contracts_of("c1")
    eq_(sorted(c.contract_id for c in contracts), ["k1", "k2"])
    eq_(set(c.number for c in contracts), {"B"})
    eq_(registry.get_contract_number("k3"), "A")
    eq_(receipt.status, "applied")
    eq_(registry.all_receipts(), [receipt])


def test_lone_directional_row_onto_occupied_target_is_blocked():
    registry = build_registry()
    service = CustomerRemapService(registry)
    pairs, subjects = service.parse_rows([("A", "B")])

    plan = service.plan_import(pairs, subjects)
    eq_(plan.is_fully_committable, False)
    blocked = plan.blocked_changes()
    eq_(blocked[0]["members"], ["A", "B"])
    assert "outside" in blocked[0]["reasons"][0]

    with pytest.raises(CustomerNumberOccupiedError):
        service.import_pairs(pairs, subjects)

    # nothing moved and no receipt was written
    eq_(registry.get_customer("c1").number, "A")
    eq_(registry.get_customer("c2").number, "B")
    eq_(registry.all_receipts(), [])


def test_bidirectional_rows_collapse_into_one_swap():
    registry = build_registry()
    service = CustomerRemapService(registry)

    pairs, subjects = service.parse_rows(
        [("A", "B"), ("B", "A")]
    )
    eq_(len(pairs), 1)
    eq_(subjects, {"A", "B"})

    service.import_pairs(pairs, subjects)

    eq_(registry.get_customer("c1").number, "B")
    eq_(registry.get_customer("c2").number, "A")


def test_pair_equality_is_order_independent():
    eq_(
        CustomerMappingPair("A", "B"),
        CustomerMappingPair("B", "A"),
    )
    eq_(
        CustomerMappingPair("A", "B").signature(),
        "A=B",
    )


def test_chained_rows_are_rejected_not_collapsed_into_a_to_c():
    registry = build_registry()
    service = CustomerRemapService(registry)
    pairs, subjects = service.parse_rows([("A", "B"), ("B", "C")])

    with pytest.raises(DuplicateCustomerMappingError) as info:
        service.import_pairs(pairs, subjects)

    eq_(info.value.number, "B")
    eq_(len(info.value.pairs), 2)
    # nothing was applied: customers keep their original numbers
    eq_(registry.get_customer("c1").number, "A")
    eq_(registry.get_customer("c2").number, "B")
    eq_(registry.get_customer("c3").number, "C")
    eq_(registry.all_receipts(), [])


def test_build_change_groups_rejects_shared_number_directly():
    pairs = [
        CustomerMappingPair("A", "B", 0),
        CustomerMappingPair("B", "C", 1),
    ]
    with pytest.raises(DuplicateCustomerMappingError):
        build_change_groups(pairs)


def test_independent_groups_commit_while_blocked_group_is_listed():
    registry = build_registry()
    service = CustomerRemapService(registry)
    # A->B would steal B from c2, who is never remapped; C->D is free.
    pairs, subjects = service.parse_rows([("A", "B"), ("C", "D")])

    plan = service.plan_import(pairs, subjects)
    eq_(plan.is_fully_committable, False)
    blocked = plan.blocked_changes()
    eq_(len(blocked), 1)
    eq_(blocked[0]["members"], ["A", "B"])
    assert blocked[0]["reasons"]

    with pytest.raises(CustomerNumberOccupiedError) as info:
        service.import_pairs(pairs, subjects)

    # the independent group C->D was committed anyway
    eq_(registry.get_customer("c3").number, "D")
    eq_(registry.get_contract_number("k4"), "D")
    # the blocked group is untouched
    eq_(registry.get_customer("c1").number, "A")
    eq_(registry.get_customer("c2").number, "B")

    receipt = registry.all_receipts()[0]
    eq_(receipt.status, "partial")
    eq_(len(receipt.applied_groups), 1)
    eq_(len(receipt.blocked_groups), 1)
    eq_(
        info.value.plan.blocked_changes(),
        plan.blocked_changes(),
    )


def test_rename_onto_free_number_moves_contracts():
    registry = build_registry()
    service = CustomerRemapService(registry)

    pairs, subjects = service.parse_rows([("A", "X")])
    service.import_pairs(pairs, subjects)

    eq_(registry.get_customer("c1").number, "X")
    eq_(registry.get_contract_number("k1"), "X")
    eq_(registry.get_contract_number("k2"), "X")


def test_replaying_same_import_does_not_swap_again():
    registry = build_registry()
    service = CustomerRemapService(registry)
    pairs, subjects = service.parse_rows([("A", "B"), ("B", "A")])
    service.import_pairs(pairs, subjects)

    replay, replay_subjects = service.parse_rows(
        [("A", "B"), ("B", "A")]
    )
    with pytest.raises(CustomerMappingError):
        service.import_pairs(replay, replay_subjects)

    # still exactly one swap, not swapped back
    eq_(registry.get_customer("c1").number, "B")
    eq_(registry.get_customer("c2").number, "A")
    eq_(len(registry.all_receipts()), 1)


def test_retry_after_partial_skips_applied_and_resolves_blocked():
    registry = build_registry()
    service = CustomerRemapService(registry)

    # first upload: A->B blocked (c2 never remapped), C->D applied
    first_pairs, first_subjects = service.parse_rows(
        [("A", "B"), ("C", "D")]
    )
    with pytest.raises(CustomerNumberOccupiedError):
        service.import_pairs(first_pairs, first_subjects)
    eq_(registry.get_customer("c3").number, "D")

    # corrected upload names both sides of A/B and repeats C/D
    retry_pairs, retry_subjects = service.parse_rows(
        [("A", "B"), ("B", "A"), ("C", "D")]
    )
    receipt = service.import_pairs(retry_pairs, retry_subjects)

    # A/B now swap; C/D is skipped and never swapped a second time
    eq_(registry.get_customer("c1").number, "B")
    eq_(registry.get_customer("c2").number, "A")
    eq_(registry.get_customer("c3").number, "D")
    eq_(receipt.status, "applied")
    eq_(receipt.describe()["skipped_pairs"], [("C", "D")])
    eq_(len(registry.all_receipts()), 2)


def test_fingerprint_ignores_row_order_and_direction():
    forward = [CustomerMappingPair("A", "B"), CustomerMappingPair("C", "D")]
    reversed_rows = [
        CustomerMappingPair("D", "C"),
        CustomerMappingPair("B", "A"),
    ]
    eq_(
        fingerprint_pairs(forward),
        fingerprint_pairs(reversed_rows),
    )


def test_receipt_reconciles_against_live_customers_and_contracts():
    registry = build_registry()
    service = CustomerRemapService(registry)
    pairs, subjects = service.parse_rows([("A", "B"), ("B", "A")])
    receipt = service.import_pairs(pairs, subjects)

    findings = service.reconcile_receipt(receipt)
    eq_(len(findings), 2)
    assert all(item["number_matches"] for item in findings)
    assert all(item["contracts_match"] for item in findings)

    # simulate drift after import: the receipt must expose it
    registry.get_customer("c1").number = "Z"
    findings = service.reconcile_receipt(receipt)
    drifted = [
        item for item in findings if item["customer_id"] == "c1"
    ][0]
    eq_(drifted["number_matches"], False)
    eq_(drifted["contracts_match"], True)


def test_receipt_cross_checks_customer_contracts_and_fingerprint():
    registry = build_registry()
    service = CustomerRemapService(registry)
    pairs, subjects = service.parse_rows([("A", "B"), ("B", "A")])
    receipt = service.import_pairs(pairs, subjects)

    stored = registry.get_receipt(receipt.fingerprint)
    eq_(stored, receipt)
    description = receipt.describe()
    eq_(description["fingerprint"], receipt.fingerprint)
    eq_(description["row_count"], 1)
    changed_customers = {
        change["customer_id"]
        for group in description["applied_groups"]
        for change in group["customers"]
    }
    eq_(changed_customers, {"c1", "c2"})
    assert registry.is_imported(receipt.fingerprint)


def test_corrected_import_can_swap_once_blocker_is_remapped():
    registry = build_registry()
    service = CustomerRemapService(registry)

    # both directions name both customers, so B is no outsider
    pairs, subjects = service.parse_rows([("A", "B"), ("B", "A")])
    receipt = service.import_pairs(pairs, subjects)

    eq_(receipt.status, "applied")
    eq_(registry.get_customer("c1").number, "B")
    eq_(registry.get_customer("c2").number, "A")


@raises(ValueError)
def test_self_mapping_is_rejected():
    CustomerMappingPair("A", "A")


@raises(ValueError)
def test_empty_number_is_rejected():
    CustomerMappingPair("A", "")


@raises(ValueError)
def test_parse_rows_requires_two_columns():
    service = CustomerRemapService(CustomerRegistry())
    service.parse_rows([["A"]])


def test_contract_must_reference_known_customer():
    registry = CustomerRegistry()
    with pytest.raises(ValueError):
        registry.add_contract(Contract("k9", "missing", "A"))


def test_customer_numbers_keep_leading_zeros_as_text():
    pair = CustomerMappingPair("001", "002")
    eq_(pair.old_number, "001")
    eq_(pair.new_number, "002")

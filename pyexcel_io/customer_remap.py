"""
pyexcel_io.customer_remap
~~~~~~~~~~~~~~~~~~~~~~~~~~

Import the cross reference between old and new customer numbers.

The spreadsheet does not carry customer profiles. Each row is one
mapping pair of two customer numbers and the two numbers are
interchangeable: the rows ``A->B`` and ``B->A`` describe the very same
swap. Contracts are bound to the stable customer identity, so they move
with the customer instead of staying on the old number.

Design notes:

* A pair only touches the two numbers it names. Rows such as ``A->B``
  and ``B->C`` share a number and are rejected instead of being chained
  into ``A->C``; the two rows must stay independent pairs.
* Pairs that do not share a number are independent change groups. A
  target number held by a customer that the import never remaps blocks
  only its own group; the other groups can still be committed.
* Every import produces a receipt whose fingerprint identifies the
  exact pair set, row order and direction aside. Replaying the same
  spreadsheet is detected so an already swapped customer is never
  swapped a second time.

:copyright: (c) 2014-2026 by C Wang
:license: New BSD License, see LICENSE for more details
"""

import hashlib

from pyexcel_io import exceptions


def normalize_number(number):
    """customer numbers are compared as text, e.g. leading zero kept"""
    if number is None:
        raise ValueError("customer number must not be empty")
    return str(number).strip()


class CustomerMappingPair(object):
    """one mapping row linking two interchangeable customer numbers"""

    def __init__(self, old_number, new_number, row_index=None):
        old_number = normalize_number(old_number)
        new_number = normalize_number(new_number)
        if not old_number or not new_number:
            raise ValueError(
                "both customer numbers of a mapping pair must be "
                "non-empty (row %s)" % row_index
            )
        if old_number == new_number:
            raise ValueError(
                "customer number %s cannot be mapped to itself (row %s)"
                % (old_number, row_index)
            )
        self.old_number = old_number
        self.new_number = new_number
        self.row_index = row_index

    @property
    def numbers(self):
        return (self.old_number, self.new_number)

    def as_tuple(self):
        return (self.old_number, self.new_number)

    def touches(self, number):
        return number in self.numbers

    def partner_of(self, number):
        """the other number of the pair, whichever side is given"""
        if number == self.old_number:
            return self.new_number
        if number == self.new_number:
            return self.old_number
        raise KeyError(number)

    def signature(self):
        """order independent, because the two numbers interchange"""
        return "=".join(sorted(self.numbers))

    def __eq__(self, other):
        if not isinstance(other, CustomerMappingPair):
            return NotImplemented
        return self.signature() == other.signature()

    def __ne__(self, other):
        result = self.__eq__(other)
        if result is NotImplemented:
            return result
        return not result

    def __hash__(self):
        return hash(self.signature())

    def __repr__(self):
        return "CustomerMappingPair(%r, %r, row_index=%r)" % (
            self.old_number,
            self.new_number,
            self.row_index,
        )


class ChangeGroup(object):
    """one independently committable set of mapping pairs

    Pairs sharing a number are rejected while the groups are built, so a
    group normally holds a single pair. The class still groups pairs so
    blocked and applied changes can be reported in one uniform shape.
    """

    def __init__(self, pairs, reasons=None):
        self.pairs = tuple(pairs)
        self.reasons = list(reasons or [])
        members = set()
        for pair in self.pairs:
            members.update(pair.numbers)
        self.members = frozenset(members)

    @property
    def member_numbers(self):
        return sorted(self.members)

    def describe(self):
        """audit view of the group, stable across runs"""
        description = {
            "members": self.member_numbers,
            "pairs": [pair.as_tuple() for pair in self.pairs],
        }
        if self.reasons:
            description["reasons"] = list(self.reasons)
        return description

    def __repr__(self):
        return "ChangeGroup(%s)" % self.member_numbers


def build_change_groups(pairs):
    """turn pairs into independent groups without transitive chaining

    A number that occurs in two distinct pairs would force rows to be
    chained (e.g. ``A->B`` plus ``B->C``). The mapping must stay pair
    based, so that situation is rejected rather than silently becoming
    ``A->C``.
    """
    seen_numbers = {}
    for pair in pairs:
        offenders = []
        for number in pair.numbers:
            if number in seen_numbers:
                offenders.append(seen_numbers[number])
        if offenders:
            conflicting = list(offenders)
            conflicting.append(pair)
            shared = next(
                number
                for number in pair.numbers
                if number in seen_numbers
            )
            raise exceptions.DuplicateCustomerMappingError(
                shared, conflicting
            )
        for number in pair.numbers:
            seen_numbers[number] = pair

    return [ChangeGroup([pair]) for pair in pairs]


class Customer(object):
    """a customer account that currently holds one customer number"""

    def __init__(self, customer_id, number):
        self.customer_id = customer_id
        self.number = normalize_number(number)

    def __repr__(self):
        return "Customer(%r, %r)" % (self.customer_id, self.number)


class Contract(object):
    """a contract that must always follow its original customer"""

    def __init__(self, contract_id, customer_id, number=None):
        self.contract_id = contract_id
        self.customer_id = customer_id
        if number is not None:
            self.number = normalize_number(number)
        else:
            self.number = None

    def __repr__(self):
        return "Contract(%r, %r, %r)" % (
            self.contract_id,
            self.customer_id,
            self.number,
        )


class CustomerRegistry(object):
    """in-memory customers, contracts and import receipts

    Customers and contracts are keyed by stable identity rather than by
    the number that happens to be current. A swap only rewrites the
    number label and never moves a contract to another customer.
    """

    def __init__(self):
        self._customers_by_id = {}
        self._contracts_by_id = {}
        self._receipts = []
        self._receipt_fingerprints = set()
        self._applied_pair_signatures = set()

    def add_customer(self, customer):
        if customer.customer_id in self._customers_by_id:
            raise ValueError(
                "duplicate customer id %s" % customer.customer_id
            )
        self._customers_by_id[customer.customer_id] = customer

    def add_contract(self, contract):
        if contract.contract_id in self._contracts_by_id:
            raise ValueError(
                "duplicate contract id %s" % contract.contract_id
            )
        if contract.customer_id not in self._customers_by_id:
            raise ValueError(
                "contract %s references unknown customer %s"
                % (contract.contract_id, contract.customer_id)
            )
        self._contracts_by_id[contract.contract_id] = contract

    def get_customer(self, customer_id):
        return self._customers_by_id[customer_id]

    def get_contract(self, contract_id):
        return self._contracts_by_id[contract_id]

    def get_contract_number(self, contract_id):
        return self.get_contract(contract_id).number

    def get_customer_by_number(self, number):
        number = normalize_number(number)
        for customer in self._customers_by_id.values():
            if customer.number == number:
                return customer
        return None

    def contracts_of(self, customer_id):
        return [
            contract
            for contract in self._contracts_by_id.values()
            if contract.customer_id == customer_id
        ]

    def all_customers(self):
        return list(self._customers_by_id.values())

    def all_contracts(self):
        return list(self._contracts_by_id.values())

    def is_imported(self, fingerprint):
        return fingerprint in self._receipt_fingerprints

    def mark_pair_applied(self, pair):
        """remember that one pair has already been swapped once"""
        self._applied_pair_signatures.add(pair.signature())

    def is_pair_applied(self, pair):
        return pair.signature() in self._applied_pair_signatures

    def applied_pair_signatures(self):
        return set(self._applied_pair_signatures)

    def save_receipt(self, receipt):
        """append a receipt; retries may repeat an earlier fingerprint"""
        self._receipts.append(receipt)
        self._receipt_fingerprints.add(receipt.fingerprint)

    def get_receipt(self, fingerprint):
        for receipt in reversed(self._receipts):
            if receipt.fingerprint == fingerprint:
                return receipt
        return None

    def all_receipts(self):
        return list(self._receipts)

    def rename_customer(self, customer, new_number):
        """move one customer onto a currently free number"""
        customer.number = normalize_number(new_number)
        self._refresh_contract_numbers(customer)

    def swap_pair(self, pair):
        """exchange the two numbers; contracts stay with customer id"""
        left = self.get_customer_by_number(pair.old_number)
        right = self.get_customer_by_number(pair.new_number)
        if left is None or right is None:
            raise KeyError(
                "swap requires a customer on both %s and %s"
                % pair.numbers
            )
        left.number, right.number = right.number, left.number
        self._refresh_contract_numbers(left)
        self._refresh_contract_numbers(right)

    def _refresh_contract_numbers(self, customer):
        for contract in self.contracts_of(customer.customer_id):
            contract.number = customer.number


class ImportReceipt(object):
    """replay proof and cross check record of one import"""

    def __init__(
        self,
        pairs,
        applied_groups,
        blocked_groups,
        applied_details,
        skipped_pairs,
        status,
    ):
        self.pairs = tuple(pairs)
        self.applied_groups = tuple(applied_groups)
        self.blocked_groups = tuple(blocked_groups)
        self.applied_details = tuple(applied_details)
        self.skipped_pairs = tuple(skipped_pairs)
        self.status = status
        self.fingerprint = fingerprint_pairs(self.pairs)
        self.row_count = len(self.pairs)

    def describe(self):
        return {
            "fingerprint": self.fingerprint,
            "status": self.status,
            "row_count": self.row_count,
            "applied_groups": [
                detail for detail in self.applied_details
            ],
            "blocked_groups": [
                group.describe() for group in self.blocked_groups
            ],
            "skipped_pairs": [
                pair.as_tuple() for pair in self.skipped_pairs
            ],
        }

    def __repr__(self):
        return "ImportReceipt(%r, %r)" % (
            self.fingerprint,
            self.status,
        )


def fingerprint_pairs(pairs):
    """fingerprint of the exact pair set, independent of row order"""
    body = "|".join(
        sorted(pair.signature() for pair in pairs)
    )
    digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
    return "crmap-%s" % digest[:16]


class RemapPlan(object):
    """the staged result before anything is written"""

    def __init__(self, pairs, applied_groups, blocked_groups):
        self.pairs = tuple(pairs)
        self.applied_groups = tuple(applied_groups)
        self.blocked_groups = tuple(blocked_groups)

    @property
    def is_fully_committable(self):
        return not self.blocked_groups

    def blocked_changes(self):
        """the affected change sets, ready to be reported back"""
        return [group.describe() for group in self.blocked_groups]

    def describe(self):
        return {
            "applied_groups": [
                group.describe() for group in self.applied_groups
            ],
            "blocked_groups": self.blocked_changes(),
        }

    def __repr__(self):
        return "RemapPlan(applied=%d, blocked=%d)" % (
            len(self.applied_groups),
            len(self.blocked_groups),
        )


class CustomerRemapService(object):
    """validate, stage and commit a customer number cross reference"""

    def __init__(self, registry):
        self.registry = registry

    def parse_rows(self, rows):
        """read (old_number, new_number) rows as fed by a spreadsheet

        Returns the deduplicated pairs and the customer numbers named on
        the old side. ``A->B`` and ``B->A`` collapse into one pair
        because the two numbers interchange.
        """
        pairs_by_signature = {}
        ordered_pairs = []
        subject_numbers = set()
        for row_index, row in enumerate(rows):
            if row is None or len(row) < 2:
                raise ValueError(
                    "row %s must contain old and new customer number"
                    % row_index
                )
            pair = CustomerMappingPair(row[0], row[1], row_index)
            subject_numbers.add(pair.old_number)
            if pair.signature() not in pairs_by_signature:
                pairs_by_signature[pair.signature()] = pair
                ordered_pairs.append(pair)
        return ordered_pairs, subject_numbers

    def plan_import(self, pairs, subject_numbers):
        """split groups into committable ones and blocked ones

        The old side of every row names a customer that this import
        remaps. A target number held by a customer that is never named
        on the old side is an outside occupier: taking that number would
        overwrite an unrelated account, so the group is blocked. Only
        that group is held back; every independent group still commits.
        """
        groups = build_change_groups(pairs)
        applied = []
        blocked = []
        for group in groups:
            reasons = []
            for pair in group.pairs:
                old_holder = self.registry.get_customer_by_number(
                    pair.old_number
                )
                if old_holder is None:
                    reasons.append(
                        "no customer currently holds old number %s"
                        % pair.old_number
                    )
                target_holder = self.registry.get_customer_by_number(
                    pair.new_number
                )
                if (
                    target_holder is not None
                    and pair.new_number not in subject_numbers
                ):
                    reasons.append(
                        "target number %s is occupied by customer %s "
                        "outside this import"
                        % (
                            pair.new_number,
                            target_holder.customer_id,
                        )
                    )
            if reasons:
                blocked.append(ChangeGroup(group.pairs, reasons))
            else:
                applied.append(group)
        return RemapPlan(pairs, applied, blocked)

    def import_pairs(self, pairs, subject_numbers=None):
        """commit the independent groups and return a receipt

        Replay protection works at pair level: a pair that was already
        swapped once is skipped rather than swapping the same customers
        a second time, and an upload where every pair was already applied
        raises. When groups are blocked by outside occupiers the
        committable groups are still applied and the blocked groups are
        listed on the receipt and on the raised exception.
        """
        if subject_numbers is None:
            subject_numbers = {pair.old_number for pair in pairs}

        skipped_pairs = [
            pair
            for pair in pairs
            if self.registry.is_pair_applied(pair)
        ]
        fresh_pairs = [
            pair
            for pair in pairs
            if not self.registry.is_pair_applied(pair)
        ]
        if not fresh_pairs:
            raise exceptions.CustomerMappingError(
                "every mapping pair of this cross reference was "
                "already imported; replaying it would swap the same "
                "customers a second time"
            )

        plan = self.plan_import(fresh_pairs, subject_numbers)
        applied_details = []
        for group in plan.applied_groups:
            applied_details.append(
                self._snapshot_group(group)
            )
            for pair in group.pairs:
                self._apply_pair(pair)
                self.registry.mark_pair_applied(pair)

        # a fully blocked import changes nothing, so it must not leave a
        # receipt behind: the same spreadsheet has to be retryable once
        # the outside occupier is resolved.
        if not plan.applied_groups:
            raise exceptions.CustomerNumberOccupiedError(plan)

        status = "applied" if not plan.blocked_groups else "partial"
        receipt = ImportReceipt(
            pairs,
            plan.applied_groups,
            plan.blocked_groups,
            applied_details,
            skipped_pairs,
            status,
        )
        self.registry.save_receipt(receipt)

        if plan.blocked_groups:
            raise exceptions.CustomerNumberOccupiedError(plan)
        return receipt

    def _apply_pair(self, pair):
        target_holder = self.registry.get_customer_by_number(
            pair.new_number
        )
        if target_holder is None:
            customer = self.registry.get_customer_by_number(
                pair.old_number
            )
            self.registry.rename_customer(
                customer, pair.new_number
            )
        else:
            self.registry.swap_pair(pair)

    def _snapshot_group(self, group):
        customers = []
        for number in group.member_numbers:
            customer = self.registry.get_customer_by_number(number)
            if customer is None:
                continue
            contract_ids = sorted(
                contract.contract_id
                for contract in self.registry.contracts_of(
                    customer.customer_id
                )
            )
            to_number = None
            for pair in group.pairs:
                if pair.touches(number):
                    to_number = pair.partner_of(number)
                    break
            customers.append(
                {
                    "customer_id": customer.customer_id,
                    "from_number": number,
                    "to_number": to_number,
                    "contract_ids": contract_ids,
                }
            )
        detail = group.describe()
        detail["customers"] = customers
        return detail

    def reconcile_receipt(self, receipt):
        """cross check a receipt against live customers and contracts

        For every customer named on the receipt it confirms that the
        customer now holds the expected number and still owns exactly
        the contracts it had at import time. Blocked groups are not part
        of the applied details, so they never appear here.
        """
        findings = []
        for detail in receipt.applied_details:
            for change in detail["customers"]:
                customer = self.registry.get_customer(
                    change["customer_id"]
                )
                actual_contracts = sorted(
                    contract.contract_id
                    for contract in self.registry.contracts_of(
                        customer.customer_id
                    )
                )
                findings.append(
                    {
                        "customer_id": customer.customer_id,
                        "expected_number": change["to_number"],
                        "actual_number": customer.number,
                        "number_matches": (
                            customer.number == change["to_number"]
                        ),
                        "expected_contracts": change["contract_ids"],
                        "actual_contracts": actual_contracts,
                        "contracts_match": (
                            actual_contracts == change["contract_ids"]
                        ),
                    }
                )
        return findings

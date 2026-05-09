from dataclasses import dataclass
from decimal import Decimal
from typing import Iterable, Optional

from transactions.models import CategorizationRule
from transactions.rules.matchers import matches_condition


OPERATOR_WEIGHT = {
    "equals": 3,
    "contains": 2,
    "regex": 1,
}


@dataclass
class MatchResult:
    rule: CategorizationRule
    specificity: int


def compute_specificity(rule: CategorizationRule) -> int:
    conditions = rule.conditions or []
    base = len(conditions)
    weighted = sum(OPERATOR_WEIGHT.get((condition or {}).get("operator"), 0) for condition in conditions)
    return base + weighted


def rule_matches_transaction(rule: CategorizationRule, imported_tx) -> bool:
    if rule.action_sign != CategorizationRule.ActionSign.ANY and imported_tx.direction != rule.action_sign:
        return False

    conditions = rule.conditions or []
    for condition in conditions:
        field = condition.get("field")
        operator = condition.get("operator")
        expected = condition.get("value")
        actual = getattr(imported_tx, field, "")
        if not matches_condition(actual, operator, expected):
            return False
    return True


def _select_best_match(imported_tx, rules: Iterable[CategorizationRule]) -> Optional[MatchResult]:
    candidates = []
    for rule in rules:
        if rule_matches_transaction(rule, imported_tx):
            candidates.append(MatchResult(rule=rule, specificity=compute_specificity(rule)))

    if not candidates:
        return None

    candidates.sort(
        key=lambda item: (
            -int(item.rule.priority),
            -Decimal(item.rule.confidence),
            -int(item.specificity),
            int(item.rule.id),
        )
    )
    return candidates[0]


def match_transaction(imported_tx, rules: Iterable[CategorizationRule]) -> Optional[MatchResult]:
    return _select_best_match(imported_tx, rules)


def apply_pipeline(imported_tx, system_rules: Iterable[CategorizationRule], user_rules: Iterable[CategorizationRule]):
    system_match = _select_best_match(imported_tx, system_rules)
    if system_match is not None:
        return system_match
    return _select_best_match(imported_tx, user_rules)

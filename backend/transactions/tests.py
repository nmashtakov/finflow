from decimal import Decimal

from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import TestCase
from django.utils import timezone

from core.models import Category, ExpenseLink, Project, Transaction
from .models import CategorizationRule, ImportedTransaction, ImportSource, TransactionImportSession
from .normalizers.registry import get_normalizer, normalize_source_code
from .rules.engine import apply_pipeline, compute_specificity
from .services.import_pipeline import (
    apply_manual_label,
    categorize_session,
    create_or_update_user_rule,
    finalize_session,
    normalize_rows,
)


class TransactionsModelsTest(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="rule_tester", password="password")
        self.source = ImportSource.objects.create(code="tinkoff", name="Tinkoff")
        self.project = Project.objects.create(user=self.user, name="Test project", status="active")
        self.category = Category.objects.create(user=self.user, name="Test category", status="active")
        self.expense_link = ExpenseLink.objects.create(
            user=self.user,
            project=self.project,
            category=self.category,
            status="active",
        )
        self.session = TransactionImportSession.objects.create(
            user=self.user,
            source=self.source,
            original_name="import.csv",
            columns=["date", "amount"],
            sample_rows=[],
            rows=[],
            metadata={},
        )

    def test_rule_conditions_valid(self):
        rule = CategorizationRule(
            source=self.source,
            name="Coffee contains",
            priority=10,
            confidence=Decimal("0.8000"),
            conditions=[
                {"field": "merchant_norm", "operator": "contains", "value": "coffee"},
                {"field": "direction", "operator": "equals", "value": "expense"},
            ],
            action_expense_link=self.expense_link,
        )
        rule.full_clean()

    def test_rule_conditions_invalid_operator(self):
        rule = CategorizationRule(
            source=self.source,
            name="Bad operator",
            conditions=[{"field": "merchant_norm", "operator": "startswith", "value": "x"}],
            action_expense_link=self.expense_link,
        )
        with self.assertRaises(ValidationError):
            rule.full_clean()

    def test_rule_conditions_invalid_field(self):
        rule = CategorizationRule(
            source=self.source,
            name="Bad field",
            conditions=[{"field": "amount_abs", "operator": "equals", "value": "100"}],
            action_expense_link=self.expense_link,
        )
        with self.assertRaises(ValidationError):
            rule.full_clean()

    def test_rule_conditions_disallow_equals_empty(self):
        rule = CategorizationRule(
            source=self.source,
            name="Equals empty",
            conditions=[{"field": "source_category_norm", "operator": "equals", "value": ""}],
            action_expense_link=self.expense_link,
        )
        with self.assertRaises(ValidationError):
            rule.full_clean()

    def test_unique_fingerprint_within_session(self):
        ImportedTransaction.objects.create(
            session=self.session,
            user=self.user,
            fingerprint="abc123",
        )
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                ImportedTransaction.objects.create(
                    session=self.session,
                    user=self.user,
                    fingerprint="abc123",
                )

    def test_direction_amount_sign_invariant_in_test_data(self):
        income_tx = ImportedTransaction.objects.create(
            session=self.session,
            user=self.user,
            direction=ImportedTransaction.Direction.INCOME,
            amount=Decimal("1500.00"),
            amount_abs=Decimal("1500.00"),
            fingerprint="income-1",
        )
        expense_tx = ImportedTransaction.objects.create(
            session=self.session,
            user=self.user,
            direction=ImportedTransaction.Direction.EXPENSE,
            amount=Decimal("-100.50"),
            amount_abs=Decimal("100.50"),
            fingerprint="expense-1",
        )

        self.assertGreater(income_tx.amount, 0)
        self.assertLess(expense_tx.amount, 0)


class NormalizationPipelineTest(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="normalize_tester", password="password")
        self.source = ImportSource.objects.create(code="generic", name="Generic")
        self.session = TransactionImportSession.objects.create(
            user=self.user,
            source=self.source,
            original_name="sample.csv",
            columns=["Date", "Amount", "Comment", "Category", "Type"],
            sample_rows=[],
            rows=[
                {"Date": "2026-02-08", "Amount": "1500", "Comment": "Salary", "Category": "Income", "Type": "income"},
                {"Date": "2026-02-09", "Amount": "100", "Comment": "Taxi", "Category": "Transport", "Type": "expense"},
                {"Date": "2026-02-09", "Amount": "100", "Comment": "Taxi", "Category": "Transport", "Type": "expense"},
            ],
            metadata={},
        )

    def test_registry_resolves_generic(self):
        self.assertEqual(normalize_source_code("other"), "generic")
        self.assertEqual(normalize_source_code("tinkoff"), "tinkoff")
        self.assertEqual(get_normalizer("unknown").code, "generic")

    def test_normalize_rows_creates_staging_transactions(self):
        mapping = {
            "column_date": "Date",
            "column_amount": "Amount",
            "column_currency": "",
            "column_comment": "Comment",
            "column_type": "Type",
            "column_category": "Category",
            "column_subcategory": "",
            "default_currency": "rub",
            "income_markers": "income",
            "expense_markers": "expense",
        }

        result = normalize_rows(self.user, self.session, mapping)

        self.assertEqual(result["normalized"], 2)
        self.assertEqual(result["duplicates"], 1)
        self.assertEqual(len(result["errors"]), 0)

        self.session.refresh_from_db()
        self.assertEqual(self.session.status, TransactionImportSession.Status.NORMALIZED)
        self.assertEqual(self.session.normalizer_version, "generic_v1")
        self.assertEqual(self.session.needs_review_rows, 2)

        tx_income = ImportedTransaction.objects.filter(session=self.session, direction="income").first()
        tx_expense = ImportedTransaction.objects.filter(session=self.session, direction="expense").first()
        self.assertIsNotNone(tx_income)
        self.assertIsNotNone(tx_expense)
        self.assertGreater(tx_income.amount, 0)
        self.assertLess(tx_expense.amount, 0)
        self.assertEqual(tx_expense.amount_abs, Decimal("100.00"))

    def test_tinkoff_invest_rounding_creates_extra_staging_transaction(self):
        source = ImportSource.objects.create(code="tinkoff", name="Tinkoff")
        session = TransactionImportSession.objects.create(
            user=self.user,
            source=source,
            original_name="tinkoff.xlsx",
            columns=[
                "Дата операции",
                "Сумма операции",
                "Валюта операции",
                "Категория",
                "Описание",
                "Округление на инвесткопилку",
            ],
            sample_rows=[],
            rows=[
                {
                    "Дата операции": "31.03.2026 21:45",
                    "Сумма операции": "-1570.00",
                    "Валюта операции": "RUB",
                    "Категория": "Рестораны",
                    "Описание": "Bar El Mancho",
                    "Округление на инвесткопилку": "30.00",
                }
            ],
            metadata={"include_tinkoff_invest_rounding": True},
        )
        mapping = {
            "column_date": "Дата операции",
            "column_amount": "Сумма операции",
            "column_currency": "Валюта операции",
            "column_comment": "Описание",
            "column_type": "",
            "column_category": "Категория",
            "column_subcategory": "",
            "default_currency": "RUB",
            "income_markers": "",
            "expense_markers": "",
        }

        result = normalize_rows(self.user, session, mapping)

        self.assertEqual(result["normalized"], 2)
        invest_tx = ImportedTransaction.objects.get(
            session=session,
            normalized_payload__generated_kind="tinkoff_invest_rounding",
        )
        self.assertEqual(invest_tx.amount, Decimal("-30.00"))
        self.assertEqual(invest_tx.currency, "RUB")
        self.assertEqual(invest_tx.source_category_norm, "инвесткопилка")


class RuleEngineAndCategorizationTest(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="categorizer", password="password")
        self.source = ImportSource.objects.create(code="tinkoff", name="Tinkoff")
        self.project = Project.objects.create(user=self.user, name="Default", status="active")
        self.category_food = Category.objects.create(user=self.user, name="Food", status="active")
        self.category_transport = Category.objects.create(user=self.user, name="Transport", status="active")
        self.link_food = ExpenseLink.objects.create(
            user=self.user, project=self.project, category=self.category_food, status="active"
        )
        self.link_transport = ExpenseLink.objects.create(
            user=self.user, project=self.project, category=self.category_transport, status="active"
        )
        self.session = TransactionImportSession.objects.create(
            user=self.user,
            source=self.source,
            original_name="categorized.csv",
            columns=[],
            sample_rows=[],
            rows=[],
            metadata={},
        )

    def _make_tx(self, fingerprint, description, direction="expense", locked=False):
        amount = Decimal("-100.00") if direction == "expense" else Decimal("100.00")
        amount_abs = abs(amount)
        return ImportedTransaction.objects.create(
            session=self.session,
            user=self.user,
            fingerprint=fingerprint,
            amount=amount,
            amount_abs=amount_abs,
            direction=direction,
            currency="RUB",
            merchant_norm=description.lower(),
            description_norm=description.lower(),
            source_category_norm="",
            locked=locked,
        )

    def test_compute_specificity_prefers_more_specific_and_stronger_operators(self):
        rule_a = CategorizationRule.objects.create(
            source=self.source,
            name="contains-only",
            priority=1,
            confidence=Decimal("0.1000"),
            conditions=[{"field": "description_norm", "operator": "contains", "value": "coffee"}],
            action_expense_link=self.link_food,
        )
        rule_b = CategorizationRule.objects.create(
            source=self.source,
            name="two-conditions",
            priority=1,
            confidence=Decimal("0.1000"),
            conditions=[
                {"field": "description_norm", "operator": "contains", "value": "coffee"},
                {"field": "direction", "operator": "equals", "value": "expense"},
            ],
            action_expense_link=self.link_food,
        )
        self.assertGreater(compute_specificity(rule_b), compute_specificity(rule_a))

    def test_apply_pipeline_system_rules_have_priority_over_user_rules(self):
        tx = self._make_tx("fp-system", "coffee shop")
        system_rule = CategorizationRule.objects.create(
            source=self.source,
            user=None,
            name="system-coffee",
            priority=1,
            confidence=Decimal("0.3000"),
            conditions=[{"field": "description_norm", "operator": "contains", "value": "coffee"}],
            action_expense_link=self.link_food,
        )
        user_rule = CategorizationRule.objects.create(
            source=self.source,
            user=self.user,
            name="user-coffee",
            priority=99,
            confidence=Decimal("0.9999"),
            conditions=[{"field": "description_norm", "operator": "contains", "value": "coffee"}],
            action_expense_link=self.link_transport,
        )

        match = apply_pipeline(tx, [system_rule], [user_rule])
        self.assertIsNotNone(match)
        self.assertEqual(match.rule.id, system_rule.id)

    def test_apply_pipeline_tie_breaker_is_rule_id_ascending(self):
        tx = self._make_tx("fp-tie", "coffee")
        first_rule = CategorizationRule.objects.create(
            source=self.source,
            user=self.user,
            name="first",
            priority=10,
            confidence=Decimal("0.5000"),
            conditions=[{"field": "description_norm", "operator": "contains", "value": "coffee"}],
            action_expense_link=self.link_food,
        )
        second_rule = CategorizationRule.objects.create(
            source=self.source,
            user=self.user,
            name="second",
            priority=10,
            confidence=Decimal("0.5000"),
            conditions=[{"field": "description_norm", "operator": "contains", "value": "coffee"}],
            action_expense_link=self.link_transport,
        )

        match = apply_pipeline(tx, [], [second_rule, first_rule])
        self.assertIsNotNone(match)
        self.assertEqual(match.rule.id, first_rule.id)

    def test_categorize_session_skips_locked_transactions(self):
        locked_tx = self._make_tx("fp-locked", "locked coffee", locked=True)
        locked_tx.categorization_status = ImportedTransaction.CategorizationStatus.MANUAL
        locked_tx.resolved_expense_link = self.link_transport
        locked_tx.save(update_fields=["categorization_status", "resolved_expense_link"])

        unlocked_tx = self._make_tx("fp-open", "coffee open", locked=False)

        rule = CategorizationRule.objects.create(
            source=self.source,
            user=self.user,
            name="coffee",
            priority=5,
            confidence=Decimal("0.6000"),
            conditions=[{"field": "description_norm", "operator": "contains", "value": "coffee"}],
            action_expense_link=self.link_food,
        )

        result = categorize_session(self.user, self.session)
        self.assertEqual(result["categorized"], 1)

        locked_tx.refresh_from_db()
        unlocked_tx.refresh_from_db()
        self.assertEqual(locked_tx.categorization_status, ImportedTransaction.CategorizationStatus.MANUAL)
        self.assertEqual(locked_tx.resolved_expense_link_id, self.link_transport.id)
        self.assertEqual(unlocked_tx.categorization_status, ImportedTransaction.CategorizationStatus.AUTO)
        self.assertEqual(unlocked_tx.matched_rule_id, rule.id)

    def test_categorize_session_sets_review_status_when_unmatched_exist(self):
        self._make_tx("fp-match", "coffee")
        self._make_tx("fp-unmatched", "unknown vendor")
        CategorizationRule.objects.create(
            source=self.source,
            user=self.user,
            name="coffee",
            priority=10,
            confidence=Decimal("0.8000"),
            conditions=[{"field": "description_norm", "operator": "contains", "value": "coffee"}],
            action_expense_link=self.link_food,
        )

        result = categorize_session(self.user, self.session)
        self.assertEqual(result["matched"], 1)
        self.assertEqual(result["needs_review"], 1)

        self.session.refresh_from_db()
        self.assertEqual(self.session.status, TransactionImportSession.Status.REVIEW)


class ReviewAndFinalizeFlowTest(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="reviewer", password="password")
        self.source = ImportSource.objects.create(code="tinkoff", name="Tinkoff")
        self.project = Project.objects.create(user=self.user, name="Main", status="active")
        self.category_food = Category.objects.create(user=self.user, name="Food", status="active")
        self.category_other = Category.objects.create(user=self.user, name="Other", status="active")
        self.link_food = ExpenseLink.objects.create(
            user=self.user, project=self.project, category=self.category_food, status="active"
        )
        self.link_other = ExpenseLink.objects.create(
            user=self.user, project=self.project, category=self.category_other, status="active"
        )
        self.session = TransactionImportSession.objects.create(
            user=self.user,
            source=self.source,
            original_name="review.csv",
            columns=[],
            sample_rows=[],
            rows=[],
            metadata={"last_mapping": {"default_account_name": "Импортный счет"}},
        )

    def _make_tx(self, fingerprint, description, status=ImportedTransaction.CategorizationStatus.NEEDS_REVIEW):
        return ImportedTransaction.objects.create(
            session=self.session,
            user=self.user,
            fingerprint=fingerprint,
            amount=Decimal("-100.00"),
            amount_abs=Decimal("100.00"),
            direction=ImportedTransaction.Direction.EXPENSE,
            currency="RUB",
            merchant_norm=description,
            description_norm=description,
            source_category_norm="",
            categorization_status=status,
            occurred_at=timezone.now(),
        )

    def test_manual_label_does_not_create_rule_implicitly(self):
        tx = self._make_tx("rv-1", "coffee")
        result = apply_manual_label(
            self.user,
            self.session,
            transaction_ids=[tx.id],
            expense_link=self.link_food,
            created_rule=None,
        )
        self.assertEqual(result["updated"], 1)
        self.assertEqual(CategorizationRule.objects.filter(user=self.user).count(), 0)
        tx.refresh_from_db()
        self.assertEqual(tx.categorization_status, ImportedTransaction.CategorizationStatus.MANUAL)
        self.assertTrue(tx.locked)
        self.assertIsNone(tx.matched_rule)

    def test_create_rule_explicitly_and_apply_to_remaining(self):
        tx_manual = self._make_tx("rv-2", "coffee star")
        tx_remaining = self._make_tx("rv-3", "coffee star")

        rule = create_or_update_user_rule(
            self.user,
            self.session,
            expense_link=self.link_food,
            filters={"merchant_norm": "coffee", "description_norm": "coffee"},
            rule_name="Coffee rule",
        )
        self.assertIsNotNone(rule.id)

        apply_manual_label(
            self.user,
            self.session,
            transaction_ids=[tx_manual.id],
            expense_link=self.link_food,
            created_rule=rule,
        )

        tx_remaining.refresh_from_db()
        self.assertEqual(tx_remaining.categorization_status, ImportedTransaction.CategorizationStatus.AUTO)
        self.assertEqual(tx_remaining.matched_rule_id, rule.id)
        self.assertEqual(tx_remaining.resolved_expense_link_id, self.link_food.id)

    def test_finalize_creates_only_auto_and_manual_transactions(self):
        tx_auto = self._make_tx("rv-4", "auto", status=ImportedTransaction.CategorizationStatus.AUTO)
        tx_auto.resolved_expense_link = self.link_food
        tx_auto.save(update_fields=["resolved_expense_link"])

        tx_manual = self._make_tx("rv-5", "manual", status=ImportedTransaction.CategorizationStatus.MANUAL)
        tx_manual.resolved_expense_link = self.link_other
        tx_manual.locked = True
        tx_manual.save(update_fields=["resolved_expense_link", "locked"])

        tx_review = self._make_tx("rv-6", "review", status=ImportedTransaction.CategorizationStatus.NEEDS_REVIEW)

        result = finalize_session(self.user, self.session)
        self.assertEqual(result["created"], 2)
        self.assertEqual(result["remaining_review"], 1)

        tx_auto.refresh_from_db()
        tx_manual.refresh_from_db()
        tx_review.refresh_from_db()
        self.assertIsNotNone(tx_auto.final_transaction_id)
        self.assertIsNotNone(tx_manual.final_transaction_id)
        self.assertIsNone(tx_review.final_transaction_id)

        self.assertEqual(Transaction.objects.count(), 2)
        self.assertEqual(CategorizationRule.objects.count(), 0)

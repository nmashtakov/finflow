from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any, Dict, List, Optional

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from core.models import Account, ExpenseLink, Project, Transaction
from transactions.models import CategorizationFeedback, CategorizationRule, ImportedTransaction, TransactionImportSession
from transactions.normalizers.base import BaseNormalizer, NormalizationContext, NormalizationError, NormalizedTx
from transactions.normalizers.registry import get_normalizer
from transactions.rules.engine import apply_pipeline


def _split_markers(markers: Optional[str]) -> set:
    return {item.strip().lower() for item in (markers or "").split(",") if item.strip()}


def _build_context(mapping_data: Dict[str, Any]) -> NormalizationContext:
    return NormalizationContext(
        column_date=mapping_data["column_date"],
        column_amount=mapping_data["column_amount"],
        column_currency=mapping_data.get("column_currency") or None,
        column_comment=mapping_data.get("column_comment") or None,
        column_type=mapping_data.get("column_type") or None,
        column_category=mapping_data.get("column_category") or None,
        column_subcategory=mapping_data.get("column_subcategory") or None,
        default_currency=(mapping_data.get("default_currency") or "RUB").strip().upper(),
        income_markers=_split_markers(mapping_data.get("income_markers")),
        expense_markers=_split_markers(mapping_data.get("expense_markers")),
    )


def _is_empty_row(row: Dict[str, Any]) -> bool:
    for value in row.values():
        if value is None:
            continue
        if str(value).strip():
            return False
    return True


def _build_normalized_payload(row: Dict[str, Any], normalized, normalizer, session):
    return {
        "source_code": session.source.code,
        "normalizer_version": normalizer.version,
        "occurred_at": normalized.occurred_at.isoformat(),
        "amount": str(normalized.amount),
        "amount_abs": str(normalized.amount_abs),
        "currency": normalized.currency,
        "direction": normalized.direction,
        "merchant_norm": normalized.merchant_norm,
        "description_norm": normalized.description_norm,
        "source_category_norm": normalized.source_category_norm,
        "external_id": normalized.external_id,
        "original_description": normalized.original_description,
        "raw_columns": row,
    }


def _should_skip_normalized(session: TransactionImportSession, row: Dict[str, Any], normalized=None) -> bool:
    if session.source.code == "tinkoff":
        status_raw = (
            BaseNormalizer.normalize_string(row.get("Статус"))
            or BaseNormalizer.normalize_string(row.get("status"))
            or BaseNormalizer.normalize_string(row.get("Status"))
        )
        status = status_raw.strip().lower()
        if status and any(marker in status for marker in ("failed", "faild", "fail")):
            return True
    return False


def _find_row_value(row: Dict[str, Any], aliases: List[str]) -> Any:
    normalized_aliases = {BaseNormalizer.normalize_text(alias) for alias in aliases}
    for key, value in row.items():
        if BaseNormalizer.normalize_text(key) in normalized_aliases:
            return value
    return None


def _build_tinkoff_invest_rounding_tx(row: Dict[str, Any], base_normalized: NormalizedTx) -> Optional[NormalizedTx]:
    rounding_raw = _find_row_value(
        row,
        [
            "Округление на инвесткопилку",
            "Округление на Инвесткопилку",
            "Округление",
            "invest_round",
        ],
    )
    if rounding_raw in (None, ""):
        return None
    rounding_amount = BaseNormalizer.parse_decimal(rounding_raw)
    if rounding_amount == Decimal("0"):
        return None
    amount = -abs(rounding_amount)
    return NormalizedTx(
        occurred_at=base_normalized.occurred_at,
        amount=amount,
        amount_abs=abs(amount),
        currency=base_normalized.currency,
        direction=ImportedTransaction.Direction.EXPENSE,
        merchant_norm="инвесткопилка",
        description_norm="инвесткопилка",
        source_category_norm="инвесткопилка",
        external_id=None,
        original_description="Инвесткопилка",
    )


def _should_create_tinkoff_invest_rounding(session: TransactionImportSession) -> bool:
    return (
        session.source.code == "tinkoff"
        and isinstance(session.metadata, dict)
        and bool(session.metadata.get("include_tinkoff_invest_rounding"))
    )


def _build_imported_transaction(
    *,
    session: TransactionImportSession,
    user,
    row: Dict[str, Any],
    normalized: NormalizedTx,
    normalizer,
    fingerprint: str,
    generated_kind: str = "",
) -> ImportedTransaction:
    normalized_payload = _build_normalized_payload(row, normalized, normalizer, session)
    if generated_kind:
        normalized_payload["generated_kind"] = generated_kind
    return ImportedTransaction(
        session=session,
        user=user,
        raw_payload=row,
        normalized_payload=normalized_payload,
        occurred_at=normalized.occurred_at,
        amount=normalized.amount,
        amount_abs=normalized.amount_abs,
        currency=normalized.currency,
        direction=normalized.direction,
        merchant_norm=normalized.merchant_norm,
        description_norm=normalized.description_norm,
        source_category_norm=normalized.source_category_norm,
        external_id=normalized.external_id,
        original_description=normalized.original_description,
        fingerprint=fingerprint,
        categorization_status=ImportedTransaction.CategorizationStatus.NEEDS_REVIEW,
    )


def _serialize_duplicate_candidate(
    *,
    index: int,
    row: Dict[str, Any],
    normalized: NormalizedTx,
    fingerprint: str,
    generated_kind: str = "",
) -> Dict[str, Any]:
    candidate = {
        "index": index,
        "row": row,
        "fingerprint": fingerprint,
        "occurred_at": normalized.occurred_at.isoformat(),
        "amount": str(normalized.amount),
        "amount_abs": str(normalized.amount_abs),
        "currency": normalized.currency,
        "direction": normalized.direction,
        "merchant_norm": normalized.merchant_norm,
        "description_norm": normalized.description_norm,
        "source_category_norm": normalized.source_category_norm,
        "external_id": normalized.external_id,
        "original_description": normalized.original_description,
    }
    if generated_kind:
        candidate["generated_kind"] = generated_kind
    return candidate


def normalize_rows(user, session: TransactionImportSession, mapping_data: Dict[str, Any]) -> Dict[str, Any]:
    normalizer = get_normalizer(session.source.code)
    context = _build_context(mapping_data)

    result = {
        "normalized": 0,
        "duplicates": 0,
        "duplicate_candidates": [],
        "skipped": 0,
        "errors": [],
    }

    rows = session.rows or []
    normalized_objects = []
    seen_fingerprints = set()

    with transaction.atomic():
        ImportedTransaction.objects.filter(session=session).delete()

        for index, row in enumerate(rows, start=1):
            try:
                if _is_empty_row(row):
                    continue

                if _should_skip_normalized(session, row):
                    result["skipped"] += 1
                    continue
                normalized = normalizer.normalize_row(row, context)
                raw_amount_value = BaseNormalizer.normalize_string(row.get(context.column_amount))
                raw_type_value = (
                    BaseNormalizer.normalize_string(row.get(context.column_type))
                    if context.column_type
                    else ""
                )
                fingerprint = BaseNormalizer.make_fingerprint(
                    user.id,
                    session.source.code,
                    normalized.external_id or "",
                    normalized.occurred_at.isoformat(),
                    normalized.amount,
                    normalized.currency,
                    normalized.direction,
                    normalized.merchant_norm,
                    normalized.description_norm,
                    raw_amount_value,
                    raw_type_value,
                )

                if fingerprint in seen_fingerprints:
                    result["duplicates"] += 1
                    result["duplicate_candidates"].append(
                        _serialize_duplicate_candidate(
                            index=index,
                            row=row,
                            normalized=normalized,
                            fingerprint=fingerprint,
                        )
                    )
                    continue
                seen_fingerprints.add(fingerprint)

                normalized_objects.append(
                    _build_imported_transaction(
                        session=session,
                        user=user,
                        row=row,
                        normalized=normalized,
                        normalizer=normalizer,
                        fingerprint=fingerprint,
                    )
                )

                if _should_create_tinkoff_invest_rounding(session):
                    invest_normalized = _build_tinkoff_invest_rounding_tx(row, normalized)
                    if invest_normalized is not None:
                        invest_fingerprint = BaseNormalizer.make_fingerprint(
                            user.id,
                            session.source.code,
                            "tinkoff_invest_rounding",
                            normalized.external_id or "",
                            invest_normalized.occurred_at.isoformat(),
                            invest_normalized.amount,
                            invest_normalized.currency,
                            invest_normalized.direction,
                            invest_normalized.merchant_norm,
                            invest_normalized.description_norm,
                            raw_amount_value,
                            raw_type_value,
                        )
                        if invest_fingerprint in seen_fingerprints:
                            result["duplicates"] += 1
                            result["duplicate_candidates"].append(
                                _serialize_duplicate_candidate(
                                    index=index,
                                    row=row,
                                    normalized=invest_normalized,
                                    fingerprint=invest_fingerprint,
                                    generated_kind="tinkoff_invest_rounding",
                                )
                            )
                        else:
                            seen_fingerprints.add(invest_fingerprint)
                            invest_row = dict(row)
                            invest_row["__finflow_generated"] = "tinkoff_invest_rounding"
                            normalized_objects.append(
                                _build_imported_transaction(
                                    session=session,
                                    user=user,
                                    row=invest_row,
                                    normalized=invest_normalized,
                                    normalizer=normalizer,
                                    fingerprint=invest_fingerprint,
                                    generated_kind="tinkoff_invest_rounding",
                                )
                            )
            except NormalizationError as exc:
                result["errors"].append({"row": index, "message": str(exc), "row_data": row})
            except Exception as exc:  # pragma: no cover
                result["errors"].append({"row": index, "message": f"Неожиданная ошибка: {exc}", "row_data": row})

        if normalized_objects:
            ImportedTransaction.objects.bulk_create(normalized_objects, batch_size=500)

        result["normalized"] = len(normalized_objects)
        session.total_rows = len(rows)
        session.matched_rows = 0
        session.needs_review_rows = result["normalized"]
        session.normalizer_version = normalizer.version
        metadata = session.metadata if isinstance(session.metadata, dict) else {}
        metadata["duplicate_candidates"] = result["duplicate_candidates"]
        metadata["confirmed_duplicate_indices"] = []
        session.metadata = metadata
        session.status = (
            TransactionImportSession.Status.NORMALIZED
            if result["normalized"] > 0
            else TransactionImportSession.Status.FAILED
        )
        session.save(
            update_fields=[
                "total_rows",
                "matched_rows",
                "needs_review_rows",
                "normalizer_version",
                "metadata",
                "status",
            ]
        )

    return result


def _resolve_link_by_dictionary(
    user,
    imported_tx: ImportedTransaction,
    mapping_data: Dict[str, Any],
    link_cache: Dict[tuple, Optional[ExpenseLink]],
) -> Optional[ExpenseLink]:
    column_project = mapping_data.get("column_project")
    column_category = mapping_data.get("column_category")
    column_subcategory = mapping_data.get("column_subcategory")

    project_name = ""
    category_name = ""
    subcategory_name = ""

    if column_project:
        project_name = BaseNormalizer.normalize_string(imported_tx.raw_payload.get(column_project))
    if not project_name:
        default_project_id = mapping_data.get("default_project_id")
        if default_project_id:
            project = Project.objects.filter(pk=default_project_id, user=user, status="active").first()
            if project:
                project_name = project.name

    if column_category:
        category_name = BaseNormalizer.normalize_string(imported_tx.raw_payload.get(column_category))
    if column_subcategory:
        subcategory_name = BaseNormalizer.normalize_string(imported_tx.raw_payload.get(column_subcategory))

    if not project_name or not category_name or not subcategory_name:
        return None

    cache_key = (
        project_name.strip().lower(),
        category_name.strip().lower(),
        subcategory_name.strip().lower(),
    )
    if cache_key in link_cache:
        return link_cache[cache_key]

    link = (
        ExpenseLink.objects.filter(
            user=user,
            status="active",
            project__status="active",
            category__status="active",
            subcategory__status="active",
            project__name__iexact=project_name,
            category__name__iexact=category_name,
            subcategory__name__iexact=subcategory_name,
        )
        .select_related("project", "category", "subcategory")
        .first()
    )
    link_cache[cache_key] = link
    return link


def _resolve_special_generated_link(user, imported_tx: ImportedTransaction) -> Optional[ExpenseLink]:
    payload = imported_tx.normalized_payload or {}
    if payload.get("generated_kind") != "tinkoff_invest_rounding":
        return None
    return (
        ExpenseLink.objects.filter(
            Q(category__name__iexact="Инвесткопилка")
            | Q(subcategory__name__iexact="Инвесткопилка", subcategory__status="active"),
            user=user,
            status="active",
            project__status="active",
            category__status="active",
        )
        .select_related("project", "category", "subcategory")
        .first()
    )


def categorize_session(user, session: TransactionImportSession, mapping_data: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    mapping_data = mapping_data or (session.metadata.get("last_mapping", {}) if isinstance(session.metadata, dict) else {})
    system_rules = list(
        CategorizationRule.objects.filter(
            source=session.source,
            user__isnull=True,
            is_active=True,
        ).select_related("action_expense_link")
    )
    user_rules = list(
        CategorizationRule.objects.filter(
            source=session.source,
            user=user,
            is_active=True,
        ).select_related("action_expense_link")
    )

    unlocked_qs = ImportedTransaction.objects.filter(session=session, user=user, locked=False)
    unlocked_qs = unlocked_qs.filter(is_split_parent=False)
    transactions_to_update = []
    dictionary_matched_count = 0
    link_cache: Dict[tuple, Optional[ExpenseLink]] = {}

    for imported_tx in unlocked_qs:
        special_link = _resolve_special_generated_link(user, imported_tx)
        if special_link is not None:
            imported_tx.categorization_status = ImportedTransaction.CategorizationStatus.AUTO
            imported_tx.matched_rule = None
            imported_tx.match_confidence = None
            imported_tx.resolved_expense_link = special_link
            dictionary_matched_count += 1
            transactions_to_update.append(imported_tx)
            continue

        dictionary_link = _resolve_link_by_dictionary(
            user=user,
            imported_tx=imported_tx,
            mapping_data=mapping_data,
            link_cache=link_cache,
        )
        if dictionary_link is not None:
            imported_tx.categorization_status = ImportedTransaction.CategorizationStatus.AUTO
            imported_tx.matched_rule = None
            imported_tx.match_confidence = None
            imported_tx.resolved_expense_link = dictionary_link
            dictionary_matched_count += 1
            transactions_to_update.append(imported_tx)
            continue

        match = apply_pipeline(imported_tx, system_rules=system_rules, user_rules=user_rules)
        if match is None:
            imported_tx.categorization_status = ImportedTransaction.CategorizationStatus.NEEDS_REVIEW
            imported_tx.matched_rule = None
            imported_tx.match_confidence = None
            imported_tx.resolved_expense_link = None
        else:
            imported_tx.categorization_status = ImportedTransaction.CategorizationStatus.AUTO
            imported_tx.matched_rule = match.rule
            imported_tx.match_confidence = match.rule.confidence
            imported_tx.resolved_expense_link = match.rule.action_expense_link
        transactions_to_update.append(imported_tx)

    if transactions_to_update:
        ImportedTransaction.objects.bulk_update(
            transactions_to_update,
            fields=[
                "categorization_status",
                "matched_rule",
                "match_confidence",
                "resolved_expense_link",
            ],
        )

    matched_rows = ImportedTransaction.objects.filter(
        session=session,
        user=user,
        categorization_status=ImportedTransaction.CategorizationStatus.AUTO,
        is_split_parent=False,
    ).count()
    needs_review_rows = ImportedTransaction.objects.filter(
        session=session,
        user=user,
        categorization_status=ImportedTransaction.CategorizationStatus.NEEDS_REVIEW,
        is_split_parent=False,
    ).count()

    session.matched_rows = matched_rows
    session.needs_review_rows = needs_review_rows
    session.status = (
        TransactionImportSession.Status.CATEGORIZED
        if needs_review_rows == 0
        else TransactionImportSession.Status.REVIEW
    )
    session.save(update_fields=["matched_rows", "needs_review_rows", "status"])

    return {
        "categorized": len(transactions_to_update),
        "matched": matched_rows,
        "needs_review": needs_review_rows,
        "dictionary_matched": dictionary_matched_count,
    }


def refresh_session_review_counters(user, session: TransactionImportSession) -> Dict[str, int]:
    matched_rows = ImportedTransaction.objects.filter(
        session=session,
        user=user,
        categorization_status=ImportedTransaction.CategorizationStatus.AUTO,
        is_split_parent=False,
    ).count()
    needs_review_rows = ImportedTransaction.objects.filter(
        session=session,
        user=user,
        categorization_status=ImportedTransaction.CategorizationStatus.NEEDS_REVIEW,
        is_split_parent=False,
    ).count()
    session.matched_rows = matched_rows
    session.needs_review_rows = needs_review_rows
    session.status = (
        TransactionImportSession.Status.CATEGORIZED
        if needs_review_rows == 0
        else TransactionImportSession.Status.REVIEW
    )
    session.save(update_fields=["matched_rows", "needs_review_rows", "status"])
    return {"matched": matched_rows, "needs_review": needs_review_rows}


def restore_duplicate_candidates(
    user,
    session: TransactionImportSession,
    candidate_indices: List[int],
) -> Dict[str, int]:
    metadata = session.metadata if isinstance(session.metadata, dict) else {}
    candidates = metadata.get("duplicate_candidates") or []
    confirmed = set(metadata.get("confirmed_duplicate_indices") or [])
    requested = {int(index) for index in candidate_indices}

    objects = []
    for candidate in candidates:
        index = int(candidate.get("index") or 0)
        if not index or index not in requested or index in confirmed:
            continue
        occurred_raw = candidate.get("occurred_at")
        occurred_at = datetime.fromisoformat(occurred_raw) if occurred_raw else timezone.now()
        if timezone.is_naive(occurred_at):
            occurred_at = timezone.make_aware(occurred_at, timezone.get_current_timezone())
        amount = Decimal(str(candidate.get("amount") or "0"))
        normalized = NormalizedTx(
            occurred_at=occurred_at,
            amount=amount,
            amount_abs=abs(amount),
            currency=(candidate.get("currency") or "").strip().upper(),
            direction=candidate.get("direction") or (
                ImportedTransaction.Direction.INCOME if amount > 0 else ImportedTransaction.Direction.EXPENSE
            ),
            merchant_norm=candidate.get("merchant_norm") or "",
            description_norm=candidate.get("description_norm") or "",
            source_category_norm=candidate.get("source_category_norm") or "",
            external_id=candidate.get("external_id") or None,
            original_description=candidate.get("original_description") or None,
        )
        fingerprint = BaseNormalizer.make_fingerprint(
            candidate.get("fingerprint") or "",
            "confirmed_duplicate",
            index,
        )
        if ImportedTransaction.objects.filter(session=session, fingerprint=fingerprint).exists():
            confirmed.add(index)
            continue
        objects.append(
            _build_imported_transaction(
                session=session,
                user=user,
                row=candidate.get("row") or {},
                normalized=normalized,
                normalizer=get_normalizer(session.source.code),
                fingerprint=fingerprint,
                generated_kind=candidate.get("generated_kind") or "confirmed_duplicate",
            )
        )
        confirmed.add(index)

    if objects:
        with transaction.atomic():
            ImportedTransaction.objects.bulk_create(objects, batch_size=100)
            metadata["confirmed_duplicate_indices"] = sorted(confirmed)
            session.metadata = metadata
            session.save(update_fields=["metadata"])
    else:
        metadata["confirmed_duplicate_indices"] = sorted(confirmed)
        session.metadata = metadata
        session.save(update_fields=["metadata"])

    counters = refresh_session_review_counters(user, session)
    return {"created": len(objects), **counters}


def _build_rule_conditions(filters: Dict[str, str]) -> List[Dict[str, str]]:
    conditions = []
    merchant = (filters.get("merchant_norm") or "").strip().lower()
    description = (filters.get("description_norm") or "").strip().lower()
    source_category = (filters.get("source_category_norm") or "").strip().lower()
    direction = (filters.get("direction") or "").strip().lower()
    currency = (filters.get("currency") or "").strip().upper()

    if merchant:
        conditions.append({"field": "merchant_norm", "operator": "contains", "value": merchant})
    if description:
        conditions.append({"field": "description_norm", "operator": "contains", "value": description})
    if source_category:
        conditions.append({"field": "source_category_norm", "operator": "contains", "value": source_category})
    if direction in {"income", "expense"}:
        conditions.append({"field": "direction", "operator": "equals", "value": direction})
    if currency:
        conditions.append({"field": "currency", "operator": "equals", "value": currency})
    return conditions


def create_or_update_user_rule(
    user,
    session: TransactionImportSession,
    expense_link: ExpenseLink,
    filters: Dict[str, str],
    rule_name: str = "",
) -> CategorizationRule:
    conditions = _build_rule_conditions(filters)
    if not conditions:
        raise ValueError("Нельзя создать правило без условий.")
    has_text_condition = any(
        condition.get("field") in {"merchant_norm", "description_norm", "source_category_norm"}
        for condition in conditions
    )
    if not has_text_condition:
        raise ValueError("Для правила укажите merchant, описание или категорию источника.")

    direction = (filters.get("direction") or "").strip().lower()
    action_sign = CategorizationRule.ActionSign.ANY
    if direction in {CategorizationRule.ActionSign.INCOME, CategorizationRule.ActionSign.EXPENSE}:
        action_sign = direction

    existing = CategorizationRule.objects.filter(
        user=user,
        source=session.source,
        action_expense_link=expense_link,
        action_sign=action_sign,
        conditions=conditions,
    ).first()
    if existing:
        if not existing.is_active:
            existing.is_active = True
            existing.save(update_fields=["is_active", "updated_at"])
        return existing

    final_name = (rule_name or "").strip()
    if not final_name:
        final_name = f"Rule: {expense_link.category.name}"

    rule = CategorizationRule(
        user=user,
        source=session.source,
        name=final_name,
        priority=100,
        confidence="0.7000",
        conditions=conditions,
        action_expense_link=expense_link,
        action_sign=action_sign,
        created_from_feedback=True,
        is_active=True,
    )
    rule.full_clean()
    rule.save()
    return rule


def apply_manual_label(
    user,
    session: TransactionImportSession,
    transaction_ids: List[int],
    expense_link: ExpenseLink,
    created_rule: Optional[CategorizationRule] = None,
) -> Dict[str, int]:
    target_qs = ImportedTransaction.objects.filter(
        id__in=transaction_ids,
        session=session,
        user=user,
        locked=False,
        is_split_parent=False,
    ).select_related("matched_rule")

    to_update = []
    feedback_items = []
    for imported_tx in target_qs:
        previous_rule = imported_tx.matched_rule
        imported_tx.resolved_expense_link = expense_link
        imported_tx.categorization_status = ImportedTransaction.CategorizationStatus.MANUAL
        imported_tx.matched_rule = None
        imported_tx.match_confidence = None
        imported_tx.locked = True
        to_update.append(imported_tx)
        feedback_items.append(
            CategorizationFeedback(
                imported_transaction=imported_tx,
                user=user,
                previous_rule=previous_rule,
                new_expense_link=expense_link,
                created_or_updated_rule=created_rule,
            )
        )

    if to_update:
        ImportedTransaction.objects.bulk_update(
            to_update,
            fields=[
                "resolved_expense_link",
                "categorization_status",
                "matched_rule",
                "match_confidence",
                "locked",
            ],
        )
        CategorizationFeedback.objects.bulk_create(feedback_items, batch_size=500)

    refresh_session_review_counters(user, session)
    return {"updated": len(to_update)}


def _resolve_account_for_finalize(user, imported_tx: ImportedTransaction, mapping_data: Dict[str, Any]) -> Account:
    default_account_id = mapping_data.get("default_account_id")
    if default_account_id:
        account = Account.objects.filter(pk=default_account_id, user=user, status="active").first()
        if account:
            return account

    account_name = ""
    column_account = mapping_data.get("column_account")
    if column_account:
        account_name = BaseNormalizer.normalize_string(imported_tx.raw_payload.get(column_account))
    if not account_name:
        account_name = BaseNormalizer.normalize_string(mapping_data.get("default_account_name"))
    if not account_name:
        account_name = f"Импорт {imported_tx.currency or 'RUB'}"

    existing = Account.objects.filter(user=user, name__iexact=account_name).first()
    if existing:
        return existing
    return Account.objects.create(
        user=user,
        name=account_name,
        currency=(imported_tx.currency or "RUB"),
        status="active",
    )


def finalize_session(user, session: TransactionImportSession) -> Dict[str, int]:
    mapping_data = session.metadata.get("last_mapping", {}) if isinstance(session.metadata, dict) else {}
    ready_qs = ImportedTransaction.objects.filter(
        session=session,
        user=user,
        final_transaction__isnull=True,
        is_split_parent=False,
        resolved_expense_link__isnull=False,
        categorization_status__in=[
            ImportedTransaction.CategorizationStatus.AUTO,
            ImportedTransaction.CategorizationStatus.MANUAL,
        ],
    ).select_related("resolved_expense_link")

    imported_items = list(ready_qs.order_by("id"))
    pending_transactions = []
    for imported_tx in imported_items:
        account = _resolve_account_for_finalize(user, imported_tx, mapping_data)
        amount = imported_tx.amount or 0
        pending_transactions.append(
            Transaction(
                account=account,
                expense_link=imported_tx.resolved_expense_link,
                amount=amount,
                currency=imported_tx.currency or account.currency,
                date=imported_tx.occurred_at or timezone.now(),
                transaction_type="income" if amount > 0 else "expense",
                comment=imported_tx.original_description or None,
            )
        )

    created_transactions = []
    if pending_transactions:
        created_transactions = Transaction.objects.bulk_create(pending_transactions, batch_size=500)
        for imported_tx, final_tx in zip(imported_items, created_transactions):
            imported_tx.final_transaction = final_tx
        ImportedTransaction.objects.bulk_update(imported_items, fields=["final_transaction"])

    remaining_review = ImportedTransaction.objects.filter(
        session=session,
        user=user,
        categorization_status=ImportedTransaction.CategorizationStatus.NEEDS_REVIEW,
        is_split_parent=False,
    ).count()

    has_unfinalized = ImportedTransaction.objects.filter(
        session=session,
        user=user,
        final_transaction__isnull=True,
        is_split_parent=False,
        categorization_status__in=[
            ImportedTransaction.CategorizationStatus.AUTO,
            ImportedTransaction.CategorizationStatus.MANUAL,
        ],
    ).exists()

    session.status = (
        TransactionImportSession.Status.COMPLETED
        if remaining_review == 0 and not has_unfinalized
        else TransactionImportSession.Status.REVIEW
    )
    session.save(update_fields=["status"])

    return {
        "created": len(created_transactions),
        "remaining_review": remaining_review,
    }

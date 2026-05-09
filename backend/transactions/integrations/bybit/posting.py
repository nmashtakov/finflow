from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from typing import Iterable, Optional

from django.db import transaction as db_transaction
from django.utils import timezone

from core.models import (
    Account,
    Category,
    ExpenseLink,
    Project,
    Subcategory,
    Transaction,
    TransactionLinkGroup,
    TransactionLinkItem,
)
from transactions.models import BybitConnection, BybitExternalEvent

MONEY_Q = Decimal("0.01")

ACCOUNT_FUND = "Bybit (FUND)"
ACCOUNT_UNIFIED = "Bybit (UNIFIED)"
ACCOUNT_CARD = "Bybit Card"

PROJECT_PERSONAL = "Личные финансы"
PROJECT_CRYPTO = "Crypto"

CATEGORY_TRANSFER = "Переводы"
SUBCATEGORY_TRANSFER_BETWEEN = "Перевод между счетами"

CATEGORY_INVEST = "Инвестиции"
SUBCATEGORY_TOKENS = "Токены"
SUBCATEGORY_EXCHANGE_FEE = "Комиссия обмена"


@dataclass
class PostingContext:
    transfer_link: ExpenseLink
    invest_link: ExpenseLink
    exchange_fee_link: ExpenseLink
    fund_account: Account
    unified_account: Account
    card_account: Account


def _quantize_amount(value: Decimal) -> Decimal:
    return value.quantize(MONEY_Q, rounding=ROUND_HALF_UP)


def _ensure_project(user, name: str) -> Project:
    obj = Project.objects.filter(user=user, name=name, status="active").first()
    if obj:
        return obj
    return Project.objects.create(user=user, name=name, status="active")


def _ensure_category(user, name: str) -> Category:
    obj = Category.objects.filter(user=user, name=name, status="active").first()
    if obj:
        return obj
    return Category.objects.create(user=user, name=name, status="active")


def _ensure_subcategory(user, name: str) -> Subcategory:
    obj = Subcategory.objects.filter(user=user, name=name, status="active").first()
    if obj:
        return obj
    return Subcategory.objects.create(user=user, name=name, status="active")


def _ensure_expense_link(user, project_name: str, category_name: str, subcategory_name: str) -> ExpenseLink:
    project = _ensure_project(user, project_name)
    category = _ensure_category(user, category_name)
    subcategory = _ensure_subcategory(user, subcategory_name)
    link, _ = ExpenseLink.objects.get_or_create(
        user=user,
        project=project,
        category=category,
        subcategory=subcategory,
        defaults={"status": "active"},
    )
    if link.status != "active":
        link.status = "active"
        link.save(update_fields=["status"])
    return link


def _ensure_account(user, name: str, currency: str = "USDT") -> Account:
    obj = Account.objects.filter(user=user, name=name, status="active").first()
    if obj:
        return obj
    return Account.objects.create(
        user=user,
        name=name,
        status="active",
        account_type="normal",
        currency=currency,
    )


def _build_context(connection: BybitConnection) -> PostingContext:
    user = connection.user
    return PostingContext(
        transfer_link=_ensure_expense_link(user, PROJECT_PERSONAL, CATEGORY_TRANSFER, SUBCATEGORY_TRANSFER_BETWEEN),
        invest_link=_ensure_expense_link(user, PROJECT_CRYPTO, CATEGORY_INVEST, SUBCATEGORY_TOKENS),
        exchange_fee_link=_ensure_expense_link(user, PROJECT_CRYPTO, CATEGORY_INVEST, SUBCATEGORY_EXCHANGE_FEE),
        fund_account=_ensure_account(user, ACCOUNT_FUND, currency="USDT"),
        unified_account=_ensure_account(user, ACCOUNT_UNIFIED, currency="USDT"),
        card_account=_ensure_account(user, ACCOUNT_CARD, currency="USD"),
    )


def _is_internal_transfer_event(event: BybitExternalEvent) -> bool:
    desc = (event.description or "").lower()
    if event.stream == "uta_translog" and "transfer" in desc:
        return True
    if event.stream == "funding_history" and "transfer" in desc:
        return True
    return False


def _is_exchange_purchase_event(event: BybitExternalEvent) -> bool:
    if event.stream != "funding_history":
        return False
    desc = (event.description or "").strip().lower()
    return desc in {"purchase", "coin purchase"}


def _pick_account(context: PostingContext, event: BybitExternalEvent) -> Account:
    if event.stream == "uta_translog":
        return context.unified_account
    if event.stream == "funding_history":
        busi = str(event.raw_payload.get("showBusiTypeEn") or event.raw_payload.get("showBusiType") or "").lower()
        if "card" in busi:
            return context.card_account
        return context.fund_account
    return context.unified_account


def _pick_expense_link(context: PostingContext, event: BybitExternalEvent) -> ExpenseLink:
    if _is_internal_transfer_event(event) or _is_exchange_purchase_event(event):
        return context.transfer_link
    return context.invest_link


def _pick_transaction_type(event: BybitExternalEvent) -> str:
    if _is_internal_transfer_event(event) or _is_exchange_purchase_event(event):
        return "transfer"
    return "income" if (event.amount or Decimal("0")) > 0 else "expense"


def _event_marker(event_id: int) -> str:
    return f"[BYBIT_EVENT:{event_id}]"


def _fee_marker(out_event_id: int, in_event_id: int) -> str:
    return f"[BYBIT_EXCHANGE_FEE:{out_event_id}:{in_event_id}]"


def _upsert_transaction_for_event(event: BybitExternalEvent, context: PostingContext) -> tuple[Optional[Transaction], str]:
    if event.amount is None:
        return None, "skipped"

    amount = _quantize_amount(Decimal(event.amount))
    if amount == 0:
        return None, "skipped"

    tx_date = event.occurred_at or timezone.now()
    account = _pick_account(context, event)
    expense_link = _pick_expense_link(context, event)
    tx_type = _pick_transaction_type(event)
    currency = (event.asset or "USDT").upper()
    marker = _event_marker(event.id)
    comment_parts = [part for part in [event.description, marker] if part]
    comment = " | ".join(comment_parts)

    transaction = event.posted_transaction
    created = False
    if transaction is None:
        transaction = Transaction.objects.create(
            account=account,
            expense_link=expense_link,
            amount=amount,
            currency=currency,
            date=tx_date,
            transaction_type=tx_type,
            comment=comment,
        )
        event.posted_transaction = transaction
        event.save(update_fields=["posted_transaction", "updated_at"])
        created = True
    else:
        changed = (
            transaction.account_id != account.id
            or transaction.expense_link_id != expense_link.id
            or transaction.amount != amount
            or transaction.currency != currency
            or transaction.date != tx_date
            or transaction.transaction_type != tx_type
            or (transaction.comment or "") != comment
        )
        if changed:
            transaction.account = account
            transaction.expense_link = expense_link
            transaction.amount = amount
            transaction.currency = currency
            transaction.date = tx_date
            transaction.transaction_type = tx_type
            transaction.comment = comment
            transaction.save(
                update_fields=[
                    "account",
                    "expense_link",
                    "amount",
                    "currency",
                    "date",
                    "transaction_type",
                    "comment",
                ]
            )

    return transaction, "created" if created else "updated"


def _create_pair_link(user, link_type: str, outgoing: Transaction, incoming: Transaction, note: str = "") -> bool:
    if outgoing.id == incoming.id:
        return False
    if outgoing.amount >= 0 or incoming.amount <= 0:
        return False
    if TransactionLinkItem.objects.filter(transaction_id__in=[outgoing.id, incoming.id]).exists():
        return False

    group = TransactionLinkGroup.objects.create(
        user=user,
        link_type=link_type,
        note=note,
    )
    TransactionLinkItem.objects.bulk_create(
        [
            TransactionLinkItem(group=group, transaction=outgoing, role=TransactionLinkItem.Role.OUTGOING),
            TransactionLinkItem(group=group, transaction=incoming, role=TransactionLinkItem.Role.INCOMING),
        ]
    )
    return True


def _auto_link_internal_transfers(connection: BybitConnection, events: Iterable[BybitExternalEvent]) -> int:
    transfer_events = [e for e in events if _is_internal_transfer_event(e) and e.posted_transaction_id]
    if not transfer_events:
        return 0

    outgoing = sorted(
        [e for e in transfer_events if e.posted_transaction and e.posted_transaction.amount < 0],
        key=lambda x: (x.occurred_at or timezone.now(), x.id),
    )
    incoming = sorted(
        [e for e in transfer_events if e.posted_transaction and e.posted_transaction.amount > 0],
        key=lambda x: (x.occurred_at or timezone.now(), x.id),
    )

    used_incoming = set()
    linked = 0
    for out_event in outgoing:
        out_tx = out_event.posted_transaction
        if not out_tx:
            continue
        out_abs = abs(out_tx.amount)
        out_time = out_tx.date
        for in_event in incoming:
            if in_event.id in used_incoming:
                continue
            in_tx = in_event.posted_transaction
            if not in_tx:
                continue
            if abs(in_tx.amount) != out_abs:
                continue
            if in_tx.currency != out_tx.currency:
                continue
            if abs((in_tx.date - out_time).total_seconds()) > 600:
                continue
            if _create_pair_link(connection.user, TransactionLinkGroup.LinkType.TRANSFER_PAIR, out_tx, in_tx, note="Bybit auto"):
                used_incoming.add(in_event.id)
                linked += 1
                break

    return linked


def _upsert_exchange_fee_tx(
    connection: BybitConnection,
    context: PostingContext,
    out_event: BybitExternalEvent,
    in_event: BybitExternalEvent,
) -> str:
    out_tx = out_event.posted_transaction
    in_tx = in_event.posted_transaction
    if not out_tx or not in_tx:
        return "skipped"

    fee_value = _quantize_amount(abs(out_tx.amount) - abs(in_tx.amount))
    if fee_value <= 0:
        return "skipped"

    marker = _fee_marker(out_event.id, in_event.id)
    comment = f"Комиссия обмена | {marker}"
    fee_tx = Transaction.objects.filter(
        account__user=connection.user,
        comment__icontains=marker,
    ).first()

    payload = {
        "account": out_tx.account,
        "expense_link": context.exchange_fee_link,
        "amount": -fee_value,
        "currency": out_tx.currency,
        "date": max(out_tx.date, in_tx.date),
        "transaction_type": "expense",
        "comment": comment,
    }

    if fee_tx is None:
        Transaction.objects.create(**payload)
        return "created"

    changed = any(
        getattr(fee_tx, key) != value
        for key, value in payload.items()
    )
    if changed:
        for key, value in payload.items():
            setattr(fee_tx, key, value)
        fee_tx.save(update_fields=["account", "expense_link", "amount", "currency", "date", "transaction_type", "comment"])
        return "updated"

    return "updated"


def _auto_link_exchanges(connection: BybitConnection, events: Iterable[BybitExternalEvent], context: PostingContext) -> tuple[int, int, int]:
    exchange_events = [e for e in events if _is_exchange_purchase_event(e) and e.posted_transaction_id]
    if not exchange_events:
        return 0, 0, 0

    outgoing = sorted(
        [e for e in exchange_events if e.posted_transaction and e.posted_transaction.amount < 0],
        key=lambda x: (x.posted_transaction.date, x.id),
    )
    incoming = sorted(
        [e for e in exchange_events if e.posted_transaction and e.posted_transaction.amount > 0],
        key=lambda x: (x.posted_transaction.date, x.id),
    )

    used_incoming = set()
    links_created = 0
    fee_created = 0
    fee_updated = 0

    for out_event in outgoing:
        out_tx = out_event.posted_transaction
        if not out_tx:
            continue
        for in_event in incoming:
            if in_event.id in used_incoming:
                continue
            in_tx = in_event.posted_transaction
            if not in_tx:
                continue
            if abs((in_tx.date - out_tx.date).total_seconds()) > 30:
                continue
            if out_tx.amount >= 0 or in_tx.amount <= 0:
                continue
            if _create_pair_link(connection.user, TransactionLinkGroup.LinkType.EXCHANGE_PAIR, out_tx, in_tx, note="Bybit auto"):
                links_created += 1
            used_incoming.add(in_event.id)
            fee_status = _upsert_exchange_fee_tx(connection, context, out_event, in_event)
            if fee_status == "created":
                fee_created += 1
            elif fee_status == "updated":
                fee_updated += 1
            break

    return links_created, fee_created, fee_updated


def publish_bybit_events_to_core(
    connection: BybitConnection,
    *,
    start_dt,
    end_dt,
    streams: Optional[list[str]] = None,
) -> dict:
    qs = BybitExternalEvent.objects.filter(
        connection=connection,
        occurred_at__gte=start_dt,
        occurred_at__lte=end_dt,
    ).exclude(amount__isnull=True)
    if streams:
        qs = qs.filter(stream__in=streams)
    events = list(qs.order_by("occurred_at", "id"))

    stats = {
        "processed": 0,
        "created": 0,
        "updated": 0,
        "skipped": 0,
        "transfer_links": 0,
        "exchange_links": 0,
        "fee_created": 0,
        "fee_updated": 0,
    }
    if not events:
        return stats

    context = _build_context(connection)

    with db_transaction.atomic():
        for event in events:
            _, status = _upsert_transaction_for_event(event, context)
            stats[status] += 1
            stats["processed"] += 1

        stats["transfer_links"] = _auto_link_internal_transfers(connection, events)
        exchange_links, fee_created, fee_updated = _auto_link_exchanges(connection, events, context)
        stats["exchange_links"] = exchange_links
        stats["fee_created"] = fee_created
        stats["fee_updated"] = fee_updated

    return stats

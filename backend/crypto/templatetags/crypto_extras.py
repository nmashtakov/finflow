from decimal import Decimal

from django import template

register = template.Library()


@register.filter
def fmt_decimal(value, places=2):
    if value is None:
        return '—'
    try:
        places = int(places)
    except (TypeError, ValueError):
        places = 2
    number = Decimal(value)
    return f'{number:.{places}f}'


@register.filter
def fmt_qty(value):
    if value is None:
        return '—'
    text = format(Decimal(value).normalize(), 'f')
    if '.' in text:
        text = text.rstrip('0').rstrip('.')
    return text or '0'


@register.filter
def fmt_pct(value):
    if value is None:
        return '—'
    number = Decimal(value)
    sign = '+' if number > 0 else ''
    return f'{sign}{number:.2f}%'

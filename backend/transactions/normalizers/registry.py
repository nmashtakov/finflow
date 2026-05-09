from .alfa import AlfaNormalizer
from .base import BaseNormalizer
from .bybit import BybitNormalizer
from .generic import GenericNormalizer
from .t_business import TBusinessNormalizer
from .tinkoff import TinkoffNormalizer


NORMALIZERS = {
    "generic": GenericNormalizer,
    "tinkoff": TinkoffNormalizer,
    "alfa": AlfaNormalizer,
    "bybit": BybitNormalizer,
    "t_business": TBusinessNormalizer,
}


def normalize_source_code(source_code=None):
    code = (source_code or "").strip().lower()
    if not code or code == "other":
        return "generic"
    return code if code in NORMALIZERS else "generic"


def get_normalizer(source_code=None) -> BaseNormalizer:
    code = normalize_source_code(source_code)
    normalizer_cls = NORMALIZERS.get(code, GenericNormalizer)
    return normalizer_cls()

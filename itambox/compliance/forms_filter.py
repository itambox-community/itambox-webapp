from compliance.filters import AuditSessionFilterSet, CustodyReceiptFilterSet, SignatureProviderConfigFilterSet
from core.forms import FilterForm


class AuditSessionFilterForm(FilterForm):
    filterset_class = AuditSessionFilterSet


class CustodyReceiptFilterForm(FilterForm):
    filterset_class = CustodyReceiptFilterSet


class SignatureProviderConfigFilterForm(FilterForm):
    filterset_class = SignatureProviderConfigFilterSet

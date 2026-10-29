import django_tables2 as tables
from django.utils.html import format_html
from django.utils.translation import gettext_lazy as _

from core.choices import ObjectChangeActionChoices
from core.models import ObjectChange

from .base import BaseTable


class ObjectChangeTable(BaseTable):
    time = tables.DateTimeColumn(linkify=True, format="Y-m-d H:i:s")
    user_name = tables.Column(verbose_name=_("User"))
    action = tables.Column(verbose_name=_("Action"))
    changed_object_type = tables.Column(linkify=False, verbose_name=_("Type"))
    object_repr = tables.Column(linkify=False, verbose_name=_("Object"))
    request_id = tables.Column(linkify=False, verbose_name=_("Request ID"))

    changed_object = tables.Column(
        linkify=lambda record: record.get_changed_object_url(), verbose_name=_("Changed Object"), accessor="object_repr"
    )

    class Meta(BaseTable.Meta):
        model = ObjectChange
        fields = (
            "time",
            "user_name",
            "action",
            "changed_object_type",
            "changed_object",
            "request_id",
        )

    def render_action(self, value, record):

        color = "secondary"
        for val, _label, c in ObjectChangeActionChoices.CHOICES:
            if val == value:
                color = c
                break
        return format_html(
            '<span class="badge bg-{0}">{1}</span>',
            color,
            record.get_action_display(),
        )

"""Event-rule presentation owned by extras."""

from django.contrib.auth.decorators import login_required
from django.utils.decorators import method_decorator
from django.utils.translation import gettext_lazy as _

from extras.forms import EventRuleForm
from extras.models import EventRule
from extras.tables import EventRuleTable
from extras.webhook_views import WorkerStatusContextMixin
from itambox.panels import Panel
from itambox.views.generic import ObjectDeleteView, ObjectDetailView, ObjectEditView, ObjectListView


@method_decorator(login_required, name="dispatch")
class EventRuleListView(WorkerStatusContextMixin, ObjectListView):
    queryset = EventRule.objects.select_related("model")
    table = EventRuleTable
    template_name = "generic/object_list.html"
    action_buttons = ("add",)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["title"] = _("Event Rules")
        return context


@method_decorator(login_required, name="dispatch")
class EventRuleDetailView(WorkerStatusContextMixin, ObjectDetailView):
    queryset = EventRule.objects.select_related("model", "webhook")
    layout = (((Panel("info", _("Event Rule Details")),),),)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["title"] = str(self.get_object())
        return context


@method_decorator(login_required, name="dispatch")
class EventRuleEditView(ObjectEditView):
    queryset = EventRule.objects.all()
    model_form = EventRuleForm

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["title"] = _("Edit Event Rule") if self.object else _("Create Event Rule")
        return context


@method_decorator(login_required, name="dispatch")
class EventRuleDeleteView(ObjectDeleteView):
    queryset = EventRule.objects.all()

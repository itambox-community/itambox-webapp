from django.urls import include, path

from extras.event_rule_views import EventRuleDeleteView, EventRuleDetailView, EventRuleEditView, EventRuleListView
from extras.export_views import (
    ExportTemplateDeleteView,
    ExportTemplateDetailView,
    ExportTemplateEditView,
    ExportTemplateListView,
    LabelTemplateDeleteView,
    LabelTemplateDetailView,
    LabelTemplateEditView,
    LabelTemplateListView,
)
from extras.webhook_views import (
    WebhookDeliveryRedeliverView,
    WebhookEndpointDeleteView,
    WebhookEndpointDetailView,
    WebhookEndpointEditView,
    WebhookEndpointListView,
    WebhookEndpointTestView,
)

from . import definition_views, views

app_name = "extras"

urlpatterns = [
    # Tags
    path("tags/", views.TagListView.as_view(), name="tag_list"),
    path("tags/create/", views.TagCreateView.as_view(), name="tag_create"),
    path("tags/edit/", views.TagBulkEditView.as_view(), name="tag_bulk_edit"),
    path("tags/delete/", views.TagBulkDeleteView.as_view(), name="tag_bulk_delete"),
    path("tags/<int:pk>/", views.TagDetailView.as_view(), name="tag_detail"),
    path("tags/<int:pk>/edit/", views.TagUpdateView.as_view(), name="tag_update"),
    path("tags/<int:pk>/delete/", views.TagDeleteView.as_view(), name="tag_delete"),
    # Dashboard
    path("dashboard/", include("extras.dashboard.urls")),
    # Custom Fields
    path("custom-fields/", views.CustomFieldListView.as_view(), name="customfield_list"),
    path("custom-fields/add/", views.CustomFieldEditView.as_view(), name="customfield_create"),
    path("custom-fields/edit/", views.CustomFieldBulkEditView.as_view(), name="customfield_bulk_edit"),
    path("custom-fields/delete/", views.CustomFieldBulkDeleteView.as_view(), name="customfield_bulk_delete"),
    path("custom-fields/<int:pk>/", views.CustomFieldDetailView.as_view(), name="customfield_detail"),
    path("custom-fields/<int:pk>/edit/", views.CustomFieldEditView.as_view(), name="customfield_update"),
    path("custom-fields/<int:pk>/delete/", views.CustomFieldDeleteView.as_view(), name="customfield_delete"),
    # Custom Fieldsets
    path("custom-fieldsets/", views.CustomFieldsetListView.as_view(), name="customfieldset_list"),
    path("custom-fieldsets/add/", views.CustomFieldsetEditView.as_view(), name="customfieldset_create"),
    path("custom-fieldsets/edit/", views.CustomFieldsetBulkEditView.as_view(), name="customfieldset_bulk_edit"),
    path("custom-fieldsets/delete/", views.CustomFieldsetBulkDeleteView.as_view(), name="customfieldset_bulk_delete"),
    path("custom-fieldsets/<int:pk>/", views.CustomFieldsetDetailView.as_view(), name="customfieldset_detail"),
    path("custom-fieldsets/<int:pk>/edit/", views.CustomFieldsetEditView.as_view(), name="customfieldset_update"),
    path("custom-fieldsets/<int:pk>/delete/", views.CustomFieldsetDeleteView.as_view(), name="customfieldset_delete"),
    # Reusable Choice Sets and Choices
    path(
        "translations/<str:kind>/<int:pk>/<str:locale>/",
        definition_views.DefinitionTranslationEditView.as_view(),
        name="definition_translation_edit",
    ),
    path(
        "choice-sets/",
        definition_views.ChoiceSetListView.as_view(),
        name="definition_choice_set_list",
    ),
    path(
        "choice-sets/add/",
        definition_views.ChoiceSetCreateView.as_view(),
        name="definition_choice_set_add",
    ),
    path(
        "choice-sets/<int:pk>/",
        definition_views.ChoiceSetDetailView.as_view(),
        name="definition_choice_set_detail",
    ),
    path(
        "choice-sets/<int:pk>/edit/",
        definition_views.ChoiceSetUpdateView.as_view(),
        name="definition_choice_set_edit",
    ),
    path(
        "choice-sets/<int:pk>/retire/",
        definition_views.ChoiceSetRetireView.as_view(),
        name="definition_choice_set_retire",
    ),
    path(
        "choice-sets/<int:choice_set_pk>/choices/add/",
        definition_views.ChoiceCreateView.as_view(),
        name="definition_choice_add",
    ),
    path(
        "choice-sets/<int:choice_set_pk>/choices/<int:pk>/edit/",
        definition_views.ChoiceUpdateView.as_view(),
        name="definition_choice_edit",
    ),
    path(
        "choice-sets/<int:choice_set_pk>/choices/<int:pk>/retire/",
        definition_views.ChoiceRetireView.as_view(),
        name="definition_choice_retire",
    ),
    # Saved Filters
    path("saved-filters/", views.SavedFilterListView.as_view(), name="savedfilter_list"),
    path("saved-filters/add/", views.SavedFilterEditView.as_view(), name="savedfilter_create"),
    path("saved-filters/save/", views.SavedFilterSaveView.as_view(), name="savedfilter_save"),
    path("saved-filters/<int:pk>/", views.SavedFilterDetailView.as_view(), name="savedfilter_detail"),
    path("saved-filters/<int:pk>/edit/", views.SavedFilterEditView.as_view(), name="savedfilter_update"),
    path("saved-filters/<int:pk>/delete/", views.SavedFilterDeleteView.as_view(), name="savedfilter_delete"),
    # Scheduled Reporting & Alerts UI
    path("alerts/", views.AlertLogListView.as_view(), name="alertlog_list"),
    path("alerts/logs/<int:pk>/acknowledge/", views.AlertAcknowledgeView.as_view(), name="alertlog_acknowledge"),
    path("alerts/logs/<int:pk>/resolve/", views.AlertResolveView.as_view(), name="alertlog_resolve"),
    path("alerts/logs/bulk-acknowledge/", views.AlertBulkAcknowledgeView.as_view(), name="alertlog_bulk_acknowledge"),
    path("alerts/logs/bulk-resolve/", views.AlertBulkResolveView.as_view(), name="alertlog_bulk_resolve"),
    path("alerts/rules/", views.AlertRuleListView.as_view(), name="alertrule_list"),
    path("alerts/rules/add/", views.AlertRuleCreateView.as_view(), name="alertrule_create"),
    path("alerts/rules/<int:pk>/", views.AlertRuleDetailView.as_view(), name="alertrule_detail"),
    path("alerts/rules/<int:pk>/edit/", views.AlertRuleUpdateView.as_view(), name="alertrule_update"),
    path("alerts/rules/<int:pk>/delete/", views.AlertRuleDeleteView.as_view(), name="alertrule_delete"),
    path("alerts/rules/<int:pk>/run/", views.AlertRuleRunNowView.as_view(), name="alertrule_run"),
    path("alerts/rules/bulk-delete/", views.AlertRuleBulkDeleteView.as_view(), name="alertrule_bulk_delete"),
    # Notification Channels
    path("alerts/channels/", views.NotificationChannelListView.as_view(), name="notificationchannel_list"),
    path("alerts/channels/add/", views.NotificationChannelCreateView.as_view(), name="notificationchannel_create"),
    path(
        "alerts/channels/<int:pk>/edit/",
        views.NotificationChannelUpdateView.as_view(),
        name="notificationchannel_update",
    ),
    path(
        "alerts/channels/<int:pk>/delete/",
        views.NotificationChannelDeleteView.as_view(),
        name="notificationchannel_delete",
    ),
    path(
        "alerts/channels/<int:pk>/test/", views.NotificationChannelTestView.as_view(), name="notificationchannel_test"
    ),
    path(
        "alerts/channels/bulk-delete/",
        views.NotificationChannelBulkDeleteView.as_view(),
        name="notificationchannel_bulk_delete",
    ),
    # Report Templates
    path("reports/templates/", views.ReportTemplateListView.as_view(), name="reporttemplate_list"),
    path("reports/templates/preview/", views.ReportTemplatePreviewView.as_view(), name="reporttemplate_preview"),
    path("reports/templates/add/", views.ReportTemplateCreateView.as_view(), name="reporttemplate_create"),
    path("reports/templates/<int:pk>/", views.ReportTemplateDetailView.as_view(), name="reporttemplate_detail"),
    path("reports/templates/<int:pk>/edit/", views.ReportTemplateUpdateView.as_view(), name="reporttemplate_update"),
    path("reports/templates/<int:pk>/delete/", views.ReportTemplateDeleteView.as_view(), name="reporttemplate_delete"),
    path(
        "reports/templates/<int:pk>/download/",
        views.ReportTemplateDownloadView.as_view(),
        name="reporttemplate_download",
    ),
    path(
        "reports/templates/bulk-delete/",
        views.ReportTemplateBulkDeleteView.as_view(),
        name="reporttemplate_bulk_delete",
    ),
    # Scheduled Reports
    path("reports/schedules/", views.ScheduledReportListView.as_view(), name="scheduledreport_list"),
    path("reports/schedules/add/", views.ScheduledReportCreateView.as_view(), name="scheduledreport_create"),
    path("reports/schedules/<int:pk>/edit/", views.ScheduledReportUpdateView.as_view(), name="scheduledreport_update"),
    path(
        "reports/schedules/<int:pk>/delete/", views.ScheduledReportDeleteView.as_view(), name="scheduledreport_delete"
    ),
    path(
        "reports/schedules/<int:pk>/trigger/",
        views.ReportTriggerImmediateView.as_view(),
        name="scheduledreport_trigger",
    ),
    path(
        "reports/schedules/<int:pk>/retry-delivery/",
        views.ScheduledReportRetryDeliveryView.as_view(),
        name="scheduledreport_retry_delivery",
    ),
    path(
        "reports/schedules/<int:pk>/scope-approval/",
        views.ScheduledReportScopeApprovalView.as_view(),
        name="scheduledreport_scope_approval",
    ),
    path(
        "reports/schedules/bulk-delete/",
        views.ScheduledReportBulkDeleteView.as_view(),
        name="scheduledreport_bulk_delete",
    ),
    # Export Templates
    path("export-templates/", ExportTemplateListView.as_view(), name="exporttemplate_list"),
    path("export-templates/add/", ExportTemplateEditView.as_view(), name="exporttemplate_create"),
    path("export-templates/<int:pk>/", ExportTemplateDetailView.as_view(), name="exporttemplate_detail"),
    path("export-templates/<int:pk>/edit/", ExportTemplateEditView.as_view(), name="exporttemplate_update"),
    path("export-templates/<int:pk>/delete/", ExportTemplateDeleteView.as_view(), name="exporttemplate_delete"),
    # Webhook Endpoints
    path("webhooks/", WebhookEndpointListView.as_view(), name="webhookendpoint_list"),
    path("webhooks/add/", WebhookEndpointEditView.as_view(), name="webhookendpoint_create"),
    path("webhooks/<int:pk>/", WebhookEndpointDetailView.as_view(), name="webhookendpoint_detail"),
    path("webhooks/<int:pk>/test/", WebhookEndpointTestView.as_view(), name="webhookendpoint_test"),
    path("webhooks/<int:pk>/edit/", WebhookEndpointEditView.as_view(), name="webhookendpoint_update"),
    path("webhooks/<int:pk>/delete/", WebhookEndpointDeleteView.as_view(), name="webhookendpoint_delete"),
    path(
        "webhook-deliveries/<int:pk>/redeliver/",
        WebhookDeliveryRedeliverView.as_view(),
        name="webhookdelivery_redeliver",
    ),
    # Event Rules
    path("event-rules/", EventRuleListView.as_view(), name="eventrule_list"),
    path("event-rules/add/", EventRuleEditView.as_view(), name="eventrule_create"),
    path("event-rules/<int:pk>/", EventRuleDetailView.as_view(), name="eventrule_detail"),
    path("event-rules/<int:pk>/edit/", EventRuleEditView.as_view(), name="eventrule_update"),
    path("event-rules/<int:pk>/delete/", EventRuleDeleteView.as_view(), name="eventrule_delete"),
    # Label Templates
    path("label-templates/", LabelTemplateListView.as_view(), name="labeltemplate_list"),
    path("label-templates/add/", LabelTemplateEditView.as_view(), name="labeltemplate_create"),
    path("label-templates/<int:pk>/", LabelTemplateDetailView.as_view(), name="labeltemplate_detail"),
    path("label-templates/<int:pk>/edit/", LabelTemplateEditView.as_view(), name="labeltemplate_update"),
    path("label-templates/<int:pk>/delete/", LabelTemplateDeleteView.as_view(), name="labeltemplate_delete"),
]

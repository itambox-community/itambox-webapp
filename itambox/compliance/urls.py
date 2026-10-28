from django.urls import path

from . import views, views_audit

app_name = "compliance"

urlpatterns = [
    # Audit Sessions
    path("audit-sessions/", views_audit.AuditSessionListView.as_view(), name="auditsession_list"),
    path("audit-sessions/add/", views_audit.AuditSessionCreateView.as_view(), name="auditsession_create"),
    path("audit-sessions/<int:pk>/", views_audit.AuditSessionDetailView.as_view(), name="auditsession_detail"),
    path("audit-sessions/<int:pk>/scan/", views_audit.AssetAuditScanView.as_view(), name="auditsession_scan"),
    path(
        "audit-sessions/<int:pk>/validate/",
        views_audit.AuditSessionValidateView.as_view(),
        name="auditsession_validate",
    ),
    path("audit-sessions/<int:pk>/commit/", views_audit.AuditSessionCommitView.as_view(), name="auditsession_commit"),
    path("audit-sessions/<int:pk>/close/", views_audit.AuditSessionCloseView.as_view(), name="auditsession_close"),
    path("audit-sessions/<int:pk>/rehome/", views_audit.AuditSessionRehomeView.as_view(), name="auditsession_rehome"),
    path(
        "audit-sessions/<int:pk>/report.csv",
        views_audit.AuditSessionReportCsvView.as_view(),
        name="auditsession_report_csv",
    ),
    path(
        "audit-sessions/<int:pk>/flag-missing/",
        views_audit.AuditSessionFlagMissingView.as_view(),
        name="auditsession_flag_missing",
    ),
    path("audit-sessions/<int:pk>/start/", views_audit.AuditSessionStartView.as_view(), name="auditsession_start"),
    path("audit-sessions/<int:pk>/delete/", views_audit.AuditSessionDeleteView.as_view(), name="auditsession_delete"),
    # Custody Receipts
    path("custody-receipts/", views.CustodyReceiptListView.as_view(), name="custodyreceipt_list"),
    path("custody-receipts/<int:pk>/", views.CustodyReceiptDetailView.as_view(), name="custodyreceipt_detail"),
    path(
        "custody-receipts/<int:pk>/prepare/",
        views.CustodyReceiptPrepareView.as_view(),
        name="custodyreceipt_prepare",
    ),
    path(
        "custody-receipts/<int:pk>/export/",
        views.CustodyReceiptExportView.as_view(),
        name="custodyreceipt_export",
    ),
    path(
        "custody-receipts/<int:pk>/export.pdf",
        views.CustodyReceiptPdfExportView.as_view(),
        name="custodyreceipt_export_pdf",
    ),
    path(
        "custody-receipts/<int:pk>/signing-sessions/<int:session_pk>/handoff-qr.svg",
        views.CustodyReceiptHandoffQrView.as_view(),
        name="custodyreceipt_handoff_qr",
    ),
    path(
        "custody-receipts/<int:pk>/signing-sessions/<int:session_pk>/email/",
        views.CustodyReceiptHandoffEmailView.as_view(),
        name="custodyreceipt_handoff_email",
    ),
    # Custody Templates
    path("custody-templates/", views.CustodyTemplateListView.as_view(), name="custodytemplate_list"),
    path("custody-templates/add/", views.CustodyTemplateEditView.as_view(), name="custodytemplate_create"),
    path("custody-templates/<int:pk>/", views.CustodyTemplateDetailView.as_view(), name="custodytemplate_detail"),
    path("custody-templates/<int:pk>/edit/", views.CustodyTemplateEditView.as_view(), name="custodytemplate_update"),
    path("custody-templates/<int:pk>/clone/", views.CustodyTemplateCloneView.as_view(), name="custodytemplate_clone"),
    path(
        "custody-templates/<int:pk>/delete/", views.CustodyTemplateDeleteView.as_view(), name="custodytemplate_delete"
    ),
    # Signature provider configs
    path(
        "signature-provider-configs/",
        views.SignatureProviderConfigListView.as_view(),
        name="signatureproviderconfig_list",
    ),
    path(
        "signature-provider-configs/add/",
        views.SignatureProviderConfigEditView.as_view(),
        name="signatureproviderconfig_create",
    ),
    path(
        "signature-provider-configs/<int:pk>/",
        views.SignatureProviderConfigDetailView.as_view(),
        name="signatureproviderconfig_detail",
    ),
    path(
        "signature-provider-configs/<int:pk>/edit/",
        views.SignatureProviderConfigEditView.as_view(),
        name="signatureproviderconfig_update",
    ),
    path(
        "signature-provider-configs/<int:pk>/delete/",
        views.SignatureProviderConfigDeleteView.as_view(),
        name="signatureproviderconfig_delete",
    ),
    # Custody
    path("custody/sign/<str:token>/", views.custody_eula_sign, name="custody_eula_sign"),
    path("custody/sign/<str:token>/return/", views.custody_provider_return, name="custody_provider_return"),
    path(
        "custody/providers/<int:config_pk>/callback/",
        views.signature_provider_callback,
        name="signature_provider_callback",
    ),
    path("custody-templates/<int:pk>/preview/", views.custody_template_preview, name="custodytemplate_preview"),
]

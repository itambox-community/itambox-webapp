"""Managed-definition action visibility must match the existing HTML guards."""

import pytest
from django.template import RequestContext
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils.html import format_html

from core.templatetags.helpers import bulk_action_context
from extras.models import CustomField, CustomFieldset
from extras.tables import CustomFieldsetTable, CustomFieldTable, TagTable


@pytest.fixture
def admin_client(client, django_user_model):
    user = django_user_model.objects.create_user(username="definition-admin", is_staff=True, is_superuser=True)
    client.force_login(user)
    return client


@pytest.fixture
def custom_field(request):
    kind = request.param
    return CustomField.objects.create(
        name="action_test",
        label="Action Test",
        namespace="action-test" if kind == "library" else kind,
        management_kind=kind,
        field_type="text",
        activation="composed",
    )


@pytest.mark.django_db
@pytest.mark.parametrize("custom_field", ["core", "library", "local"], indirect=True)
def test_custom_field_row_actions_match_management(admin_client, custom_field):
    response = admin_client.get(reverse("extras:customfield_list"))
    assert response.status_code == 200
    row = next(row for row in response.context["table"].rows if row.record.pk == custom_field.pk)
    actions = row.get_cell("actions")
    for action in ("update", "delete"):
        url = reverse(f"extras:customfield_{action}", kwargs={"pk": custom_field.pk})
        assert (url in actions) == (custom_field.management_kind == "local")
    assert f"{custom_field.get_absolute_url()}?tab=changelog" in actions
    assert row.get_cell("management_kind") == custom_field.get_management_kind_display()


@pytest.mark.django_db
@pytest.mark.parametrize("custom_field", ["core", "library", "local"], indirect=True)
def test_custom_field_detail_actions_match_management(admin_client, custom_field):
    response = admin_client.get(custom_field.get_absolute_url())
    assert response.status_code == 200
    mutable = custom_field.management_kind == "local"
    assert response.context["can_change"] is mutable
    assert response.context["can_delete"] is mutable
    for action in ("update", "delete"):
        url = reverse(f"extras:customfield_{action}", kwargs={"pk": custom_field.pk})
        assert (format_html('href="{}"', url) in response.text) is mutable
    assert ("Managed definitions are read-only." in response.text) is not mutable


@pytest.mark.django_db
@pytest.mark.parametrize("model", [CustomField, CustomFieldset])
def test_definition_list_does_not_offer_bulk_edit(admin_client, model):
    response = admin_client.get(reverse(f"extras:{model._meta.model_name}_list"))
    assert response.status_code == 200
    assert response.context["bulk_edit_url"] is None
    url = reverse(f"extras:{model._meta.model_name}_bulk_edit")
    assert format_html('action="{}"', url) not in response.text
    assert response.context["bulk_delete_url"] == reverse(f"extras:{model._meta.model_name}_bulk_delete")
    assert admin_client.get(url).status_code == 403


@pytest.mark.parametrize("table_class", [CustomFieldTable, CustomFieldsetTable])
def test_embedded_definition_table_does_not_offer_bulk_edit(rf, django_user_model, table_class):
    request = rf.get("/extras/custom-fields/")
    request.user = django_user_model(is_staff=True, is_superuser=True)
    table = table_class([])
    context = bulk_action_context(RequestContext(request, {"request": request}), table)
    assert context["bulk_edit_url"] is None
    assert context["can_change"] is False
    assert context["can_delete"] is True
    assert context["bulk_delete_url"] == reverse(f"extras:{table.Meta.model._meta.model_name}_bulk_delete")
    table.embed_bulk_bar = True
    html = render_to_string("global_includes/htmx_table.html", {"table": table, "request": request})
    assert "btn-bulk-edit" not in html
    assert "btn-bulk-delete" in html


@pytest.mark.django_db
@pytest.mark.parametrize("custom_field", ["core", "library"], indirect=True)
@pytest.mark.parametrize("action", ["update", "delete"])
@pytest.mark.parametrize("method", ["get", "post"])
@pytest.mark.parametrize("boosted", [False, True])
def test_managed_custom_field_direct_mutation_denied(admin_client, custom_field, action, method, boosted):
    url = reverse(f"extras:customfield_{action}", kwargs={"pk": custom_field.pk})
    before = CustomField.objects.filter(pk=custom_field.pk).values().get()
    headers = {"HX-Request": "true", "HX-Boosted": "true"} if boosted else {}
    response = getattr(admin_client, method)(url, {"label": "Tampered", "confirm": "true"}, headers=headers)
    assert response.status_code == 403
    assert CustomField.objects.filter(pk=custom_field.pk).values().get() == before


@pytest.mark.django_db
@pytest.mark.parametrize("custom_field", ["local"], indirect=True)
@pytest.mark.parametrize("action", ["update", "delete"])
def test_local_custom_field_mutation_pages_remain_accessible(admin_client, custom_field, action):
    response = admin_client.get(reverse(f"extras:customfield_{action}", kwargs={"pk": custom_field.pk}))
    assert response.status_code == 200


@pytest.mark.django_db
@pytest.mark.parametrize("model", [CustomField, CustomFieldset])
@pytest.mark.parametrize("method", ["get", "post"])
def test_local_definition_bulk_edit_remains_denied(admin_client, model, method):
    identity = {"name": "local_bulk", "activation": "composed", "field_type": "text"}
    if model is CustomFieldset:
        identity = {"slug": "local-bulk"}
    definition = model.objects.create(label="Local Bulk", **identity)
    before = model.objects.filter(pk=definition.pk).values().get()
    url = reverse(f"extras:{model._meta.model_name}_bulk_edit")
    response = getattr(admin_client, method)(url, {"pk": [definition.pk], "label": "Tampered"})
    assert response.status_code == 403
    assert model.objects.filter(pk=definition.pk).values().get() == before


@pytest.mark.django_db
@pytest.mark.parametrize("custom_field", ["core", "library"], indirect=True)
@pytest.mark.parametrize("confirmed", [False, True])
def test_managed_custom_field_bulk_delete_remains_denied(admin_client, custom_field, confirmed):
    data = {"pk": [custom_field.pk]}
    if confirmed:
        data["_confirm"] = "true"
    before = CustomField.objects.filter(pk=custom_field.pk).values().get()
    response = admin_client.post(reverse("extras:customfield_bulk_delete"), data)
    assert response.status_code == 403
    assert CustomField.objects.filter(pk=custom_field.pk).values().get() == before


@pytest.mark.django_db
def test_unrelated_tag_tables_retain_bulk_actions(admin_client):
    response = admin_client.get(reverse("extras:tag_list"))
    assert response.status_code == 200
    request = response.wsgi_request
    table = TagTable([])
    context = bulk_action_context(RequestContext(request, {"request": request}), table)
    for action in ("edit", "delete"):
        url = reverse(f"extras:tag_bulk_{action}")
        assert response.context[f"bulk_{action}_url"] == url
        assert context[f"bulk_{action}_url"] == url
        assert format_html('action="{}"', url) in response.text
    assert context["can_change"] is True
    assert context["can_delete"] is True

"""Regression coverage for the legacy fieldset UI after the identity cutover."""

import html5lib
import pytest
from django.urls import reverse
from django.utils.html import format_html

from extras.models import CustomFieldset
from extras.tables import CustomFieldsetTable


@pytest.fixture
def admin_client(client, django_user_model):
    user = django_user_model.objects.create_user(username="fieldset-admin", is_staff=True, is_superuser=True)
    client.force_login(user)
    return client


@pytest.mark.django_db
@pytest.mark.parametrize("label", ["Network Config", ""])
def test_detail_name_panel_title_and_breadcrumb(admin_client, label):
    fieldset = CustomFieldset.objects.create(slug="network-config", label=label)
    response = admin_client.get(fieldset.get_absolute_url())
    name = label or fieldset.slug

    assert response.status_code == 200
    assert format_html("<strong>{}</strong>", name) in response.text
    document = html5lib.parse(response.text, namespaceHTMLElements=False)
    assert document.find("head/title").text.strip() == f"{name} - Custom Field Set - ITAMbox"
    assert response.context["title"] == name
    assert response.context["breadcrumbs"][-1] == (None, name)


@pytest.mark.django_db
@pytest.mark.parametrize("order_by, expected", [("name", ["Alpha", "Zulu"]), ("-name", ["Zulu", "Alpha"])])
def test_list_name_orders_by_label(admin_client, order_by, expected):
    first = CustomFieldset.objects.create(slug="a-fieldset", label="Zulu")
    second = CustomFieldset.objects.create(slug="z-fieldset", label="Alpha")
    response = admin_client.get(reverse("extras:customfieldset_list"), {"sort": order_by})

    assert response.status_code == 200
    rows = [row for row in response.context["table"].rows if row.record.pk in {first.pk, second.pk}]
    assert [row.record.label for row in rows] == expected
    for row in rows:
        assert format_html('<a href="{}">{}</a>', row.record.get_absolute_url(), row.record.label) in response.text


@pytest.fixture
def fieldset(request):
    kind = request.param
    return CustomFieldset.objects.create(
        namespace="issue516" if kind == "library" else kind,
        slug=f"{kind}-fieldset",
        label=f"{kind.title()} Fieldset",
        management_kind=kind,
    )


@pytest.mark.django_db
@pytest.mark.parametrize("fieldset", ["core", "library", "local"], indirect=True)
def test_list_actions_follow_management_kind(admin_client, fieldset):
    response = admin_client.get(reverse("extras:customfieldset_list"))

    assert response.status_code == 200
    row = next(row for row in response.context["table"].rows if row.record.pk == fieldset.pk)
    actions = row.get_cell("actions")
    for action in ("update", "delete"):
        url = reverse(f"extras:customfieldset_{action}", kwargs={"pk": fieldset.pk})
        assert (url in actions) == (fieldset.management_kind == "local")
    assert f"{fieldset.get_absolute_url()}?tab=changelog" in actions
    assert row.get_cell("management_kind") == fieldset.get_management_kind_display()


@pytest.mark.django_db
@pytest.mark.parametrize("fieldset", ["core", "library", "local"], indirect=True)
def test_detail_actions_follow_management_kind(admin_client, fieldset):
    response = admin_client.get(fieldset.get_absolute_url())
    mutable = fieldset.management_kind == "local"

    assert response.status_code == 200
    assert response.context["can_change"] is mutable
    assert response.context["can_delete"] is mutable
    for action, context_key in (("update", "edit_url"), ("delete", "delete_url")):
        url = reverse(f"extras:customfieldset_{action}", kwargs={"pk": fieldset.pk})
        assert response.context[context_key] == (url if mutable else None)
        assert (format_html('href="{}"', url) in response.text) is mutable
    assert fieldset.get_management_kind_display() in response.text
    assert ("Managed definitions are read-only." in response.text) is not mutable


@pytest.mark.django_db
@pytest.mark.parametrize("fieldset", ["core", "library"], indirect=True)
@pytest.mark.parametrize("action", ["update", "delete"])
@pytest.mark.parametrize("method", ["get", "post"])
@pytest.mark.parametrize("boosted", [False, True])
def test_managed_direct_mutation_denied(admin_client, fieldset, action, method, boosted):
    url = reverse(f"extras:customfieldset_{action}", kwargs={"pk": fieldset.pk})
    before = CustomFieldset.objects.filter(pk=fieldset.pk).values().get()
    headers = {"HX-Request": "true", "HX-Boosted": "true"} if boosted else {}
    response = getattr(admin_client, method)(url, {"label": "Tampered", "confirm": "true"}, headers=headers)

    assert response.status_code == 403
    assert CustomFieldset.objects.filter(pk=fieldset.pk).values().get() == before


@pytest.mark.django_db
@pytest.mark.parametrize("action", ["update", "delete"])
def test_local_mutation_pages_remain_accessible(admin_client, action):
    fieldset = CustomFieldset.objects.create(slug="local-fieldset", label="Local Fieldset")
    response = admin_client.get(reverse(f"extras:customfieldset_{action}", kwargs={"pk": fieldset.pk}))

    assert response.status_code == 200


@pytest.mark.parametrize("label", ["Network Config", ""])
def test_table_name_links_label_or_slug(label):
    fieldset = CustomFieldset(pk=516, slug="network-config", label=label)
    table = CustomFieldsetTable([fieldset])

    assert table.columns["name"].header == "Name"
    assert table.rows[0].get_cell("name") == format_html(
        '<a href="{}">{}</a>',
        reverse("extras:customfieldset_detail", kwargs={"pk": fieldset.pk}),
        label or fieldset.slug,
    )

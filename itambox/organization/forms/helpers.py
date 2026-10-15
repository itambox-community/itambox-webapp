from crispy_forms.layout import HTML, Submit
from django.urls import reverse
from django.utils.translation import gettext_lazy as _

from core.forms import cancel_button


def add_standard_buttons(helper, instance, list_url_name, *, dismiss=False):
    button_text = _("Update") if instance and instance.pk else _("Create")
    cancel_url = reverse(list_url_name)
    helper.layout.append(HTML('<div class="mt-4"></div>'))
    helper.layout.append(Submit("submit", button_text, css_class="btn btn-primary"))
    helper.layout.append(cancel_button(cancel_url, dismiss=dismiss, no_dirty_track=True))

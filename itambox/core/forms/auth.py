"""Authentication forms for the interactive login page.

The stock :class:`~django.contrib.auth.forms.AuthenticationForm` ships two
different failure messages (``invalid_login`` / ``inactive``). Rendering both a
template-level banner and the form's own non-field error produced *two* panels
for one failed attempt, and the ``inactive`` variant discloses that the account
exists. One generic message is used for every failed local login instead.
"""

from django import forms
from django.contrib.auth.forms import AuthenticationForm
from django.utils.translation import gettext_lazy as _

#: The single message shown for any failed local-credential login. It must stay
#: free of credential-specific detail (which field was wrong, whether the
#: account exists or is disabled) so it cannot be used to enumerate accounts.
INVALID_CREDENTIALS_MESSAGE = _("Your username and password didn't match. Please try again.")


class LoginForm(AuthenticationForm):
    """Authentication form with a single generic, non-enumerating error."""

    error_messages = {
        **AuthenticationForm.error_messages,
        "invalid_login": INVALID_CREDENTIALS_MESSAGE,
        "inactive": INVALID_CREDENTIALS_MESSAGE,
    }

    def full_clean(self):
        super().full_clean()
        # Crispy marks fields with their own errors as invalid, but an
        # authentication failure is a *form-level* error: without this the
        # credential inputs carry no invalid state for assistive technology.
        if self.is_bound and self.non_field_errors():
            for name in ("username", "password"):
                field = self.fields.get(name)
                if field is not None:
                    field.widget.attrs["aria-invalid"] = "true"
                    field.widget.attrs["aria-describedby"] = "login-form-errors"


class LoginIdentifierForm(forms.Form):
    """Collect an identifier without checking whether it belongs to an account."""

    identifier = forms.CharField(
        label=_("Email address or username"),
        max_length=254,
        strip=True,
        widget=forms.TextInput(
            attrs={
                "autocomplete": "username",
                "autofocus": True,
                "inputmode": "email",
            }
        ),
    )

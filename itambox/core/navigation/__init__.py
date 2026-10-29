from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from django.apps import apps
from django.urls import reverse_lazy
from django.utils.translation import gettext_lazy as _

from core.forms.import_forms import is_model_importable

__all__ = (
    "Menu",
    "MenuGroup",
    "MenuItem",
    "MenuItemButton",
    "get_model_buttons",
    "get_model_item",
)


@dataclass
class MenuItemButton:
    link: str
    title: str
    icon_class: str
    _url: str | None = field(default=None, init=False)
    permissions: Sequence[str] = ()
    color: str | None = None
    condition: Callable | None = None

    def __post_init__(self):
        if self.link:
            if isinstance(self.link, str) and "/" in self.link:
                self._url = self.link
            elif not isinstance(self.link, str):
                self._url = self.link
            else:
                self._url = reverse_lazy(self.link)

    @property
    def url(self):
        return self._url


@dataclass
class MenuItem:
    link: str
    link_text: str
    _url: str | None = field(default=None, init=False)
    permissions: Sequence[str] = ()
    auth_required: bool = False
    staff_only: bool = False
    buttons: Sequence[MenuItemButton] = ()
    # Optional extra gate evaluated per-request with the user; used for state checks the
    # plain permission strings can't express (e.g. "only when a managing/MSP tenant
    # exists", per-tenant object-level checks). The item is hidden when the callable
    # returns False.
    condition: Callable | None = None
    # Item-level Beta marker for groups that mix mature and beta capabilities;
    # the menu template renders it as a badge next to the item label.
    beta: bool = False

    def __post_init__(self):
        if self.link:
            if isinstance(self.link, str) and "/" in self.link:
                self._url = self.link
            elif not isinstance(self.link, str):
                self._url = self.link
            else:
                self._url = reverse_lazy(self.link)

    @property
    def url(self):
        return self._url


@dataclass
class MenuGroup:
    label: str
    items: Sequence[MenuItem]
    beta: bool = False


@dataclass
class Menu:
    label: str
    icon_class: str
    groups: Sequence[MenuGroup]

    @property
    def name(self):
        return self.label.replace(" ", "_").replace("&", "")


def get_model_item(app_label, model_name, label, actions=("add", "import")):
    return MenuItem(
        link=f"{app_label}:{model_name}_list",
        link_text=label,
        permissions=[f"{app_label}.view_{model_name}"],
        buttons=get_model_buttons(app_label, model_name, actions),
    )


def get_model_buttons(app_label, model_name, actions=("add", "import"), import_title=None):
    buttons = []

    if "add" in actions:
        buttons.append(
            MenuItemButton(
                link=f"{app_label}:{model_name}_create",
                title=_("Add"),
                icon_class="mdi mdi-plus-thick",
                permissions=[f"{app_label}.add_{model_name}"],
            )
        )

    if "import" in actions:
        try:
            model = apps.get_model(app_label, model_name)
        except LookupError:
            model = None
        # Only importable models (not generated logs / UI-only config) get the button.
        if model is not None and is_model_importable(model):
            buttons.append(
                MenuItemButton(
                    link=f"/import/{app_label}/{model_name}/",
                    title=import_title or _("Import"),
                    icon_class="mdi mdi-upload",
                    permissions=[f"{app_label}.add_{model_name}"],
                    color="outline text-success",
                )
            )

    return buttons

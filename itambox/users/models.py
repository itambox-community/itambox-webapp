import hashlib
import hmac
import ipaddress
import secrets
import uuid
from typing import Callable, cast

from django.apps import apps
from django.conf import settings
from django.contrib.auth.models import AbstractUser
from django.contrib.postgres.fields import ArrayField
from django.core.exceptions import ValidationError
from django.db import models
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from core.managers import ExplicitScopeAllObjectsManager, SoftDeleteManager
from core.mixins import AutoSlugMixin, SoftDeleteMixin
from core.models import ChangeLoggingMixin, StandardModel
from core.oidc_identity import (
    OIDC_ISSUER_MAX_LENGTH,
    OIDC_SUBJECT_MAX_LENGTH,
    validate_oidc_identity,
    validate_oidc_issuer,
    validate_oidc_subject,
)


def token_peppers():
    """Return the configured API-token peppers as ``{int id: secret}``.

    Peppers are server-side secrets combined (via HMAC-SHA256) with the random
    token before hashing, so a database leak alone does not yield usable tokens.
    Configured via ``ITAMBOX_API_TOKEN_PEPPERS`` (a JSON object of id->secret).
    When none are configured we fall back to a single pepper derived from
    SECRET_KEY so the app/tests work out of the box — production deployments
    should set an explicit, rotatable pepper.
    """
    configured = getattr(settings, "API_TOKEN_PEPPERS", None)
    if configured:
        return {int(k): v for k, v in configured.items()}
    return {1: settings.SECRET_KEY}


def current_pepper_id():
    """The highest (newest) configured pepper id — used to hash new tokens."""
    return max(token_peppers())


def hash_token(plaintext, pepper_id):
    """HMAC-SHA256 digest of a plaintext token under the given pepper id."""
    pepper = token_peppers()[pepper_id]
    return hmac.new(pepper.encode("utf-8"), plaintext.encode("utf-8"), hashlib.sha256).hexdigest()


def validate_cidr_list(value):
    """Validate that every entry in an allowed_ips list is a valid IPv4/IPv6 host or CIDR network."""
    for prefix in value:
        try:
            ipaddress.ip_network(prefix, strict=False)
        except ValueError:
            raise ValidationError(
                _('"%(prefix)s" is not a valid IP address or CIDR prefix.'),
                params={"prefix": prefix},
            ) from None


class User(AbstractUser):
    """Project user model — stock ``AbstractUser`` plus a dedicated ``can_login`` flag.

    ``can_login`` controls whether the person may perform *interactive* login (password or
    SSO). It is a separate axis from ``is_active`` (account / membership status, which must
    not be overloaded) and from API-token access. A person who is tracked in the directory
    but must never sign in simply has ``can_login=False`` — there is no separate "contact"
    person type.
    """

    can_login = models.BooleanField(
        default=True,
        db_index=True,
        verbose_name=_("Can log in"),
        help_text=_(
            "If unchecked, this user cannot perform interactive login (password or SSO). "
            "API tokens and account status (is_active) are unaffected."
        ),
    )
    scim_id = models.UUIDField(
        default=uuid.uuid4,
        unique=True,
        editable=False,
        verbose_name=_("SCIM resource ID"),
        help_text=_("Stable opaque identifier emitted by the SCIM API."),
    )
    # NOTE: email is deliberately NOT globally unique. SSO (OIDC/SAML/LDAP), SCIM,
    # and the SnipeIT importer all provision accounts independently of email, and
    # forcing email-based identity would either break those flows or invite
    # email-based account-linking takeover. Inline onboarding therefore resolves
    # identity at the write path (users.services): it reuses a single match, fails
    # closed on an ambiguous email, and never silently merges accounts.


class OIDCIdentity(ChangeLoggingMixin, models.Model):
    """The exact verified OIDC principal binding owned by the users domain."""

    # The external identity key is credential-like. It is intentionally absent
    # from ObjectChange snapshots; the binding itself remains auditable through
    # its global row and sanitized object representation.
    _change_logging_excluded_fields = ["issuer", "subject"]
    changelog_global = True

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="oidc_identities",
    )
    issuer = models.CharField(
        max_length=OIDC_ISSUER_MAX_LENGTH,
        validators=[cast(Callable[[object], None], validate_oidc_issuer)],
    )
    subject = models.CharField(
        max_length=OIDC_SUBJECT_MAX_LENGTH,
        validators=[cast(Callable[[object], None], validate_oidc_subject)],
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["issuer", "subject"],
                name="users_oidcidentity_unique_issuer_subject",
            ),
        ]

    def clean(self) -> None:
        super().clean()
        validate_oidc_identity(self.issuer, self.subject)

    def __str__(self) -> str:
        return f"OIDC identity binding for User #{self.user_id}"


class UserPreference(models.Model):
    """
    Stores user-specific preferences, including table configurations.
    """

    THEME_DARK = "dark"
    THEME_LIGHT = "light"
    THEME_CHOICES = (
        (THEME_LIGHT, _("Light")),
        (THEME_DARK, _("Dark")),
    )

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="preferences",  # Keep related_name for now, or change if needed
        verbose_name=_("User"),
    )
    # Store various preferences as key-value pairs
    # Example: {"tables": {"assets.AssetTable": {"columns": [...]}}}
    data = models.JSONField(default=dict, blank=True, verbose_name=_("Data"))

    def __str__(self):
        return f"Preferences for {self.user.username}"

    class Meta:
        ordering = ("user",)
        verbose_name = _("User Preference")
        verbose_name_plural = _("User Preferences")


class Token(ChangeLoggingMixin, models.Model):
    """
    An API token used for authenticating REST API requests.

    The secret is never stored in plaintext: only an HMAC-SHA256 ``digest``
    (keyed by a server-side pepper) and a short non-secret ``key_preview`` for
    identification are persisted. The plaintext is generated once, shown once,
    and available on the in-memory instance via the ``key`` property until the
    object is reloaded from the database.
    """

    # Never serialize the at-rest credential (digest), its pepper id, or the
    # high-frequency last_used heartbeat into the changelog JSON.
    _change_logging_excluded_fields = ["updated_at", "digest", "pepper", "last_used"]

    # HMAC-SHA256(pepper, plaintext) — the only at-rest representation of the
    # secret. `pepper` records which configured pepper produced this digest so
    # peppers can be rotated without rehashing.
    digest = models.CharField(max_length=64, unique=True, editable=False, blank=True)
    pepper = models.PositiveSmallIntegerField(null=True, blank=True, editable=False)
    key_preview = models.CharField(max_length=16, blank=True, editable=False)
    user = models.ForeignKey(
        to=settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="tokens",
        verbose_name=_("User"),
    )
    tenant = models.ForeignKey(
        to="organization.Tenant",
        on_delete=models.CASCADE,
        related_name="tokens",
        db_index=True,
        verbose_name=_("Tenant"),
    )
    created = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True, editable=False)
    expires = models.DateTimeField(blank=True, null=True, db_index=True, verbose_name=_("Expires"))
    last_used = models.DateTimeField(blank=True, null=True, verbose_name=_("Last Used"))
    write_enabled = models.BooleanField(default=True, verbose_name=_("Write Enabled"))
    description = models.CharField(max_length=200, blank=True, verbose_name=_("Description"))
    allowed_ips = ArrayField(
        base_field=models.CharField(max_length=43),
        blank=True,
        default=list,
        validators=[validate_cidr_list],
        verbose_name=_("Allowed IPs"),
        help_text=_(
            "Permitted IPv4/IPv6 networks from which this token may be used, in CIDR notation "
            '(e.g. "192.168.1.0/24, 10.0.0.5"). Leave blank to allow any source address.'
        ),
    )

    # Transient plaintext, set on generation and cleared once the instance is
    # reloaded from the DB. Never persisted.
    _plaintext = None

    class Meta:
        ordering = ["-created"]
        verbose_name = _("Token")
        verbose_name_plural = _("Tokens")

    def __str__(self):
        return f"{self.user.username}: {self.key_preview}..."

    @property
    def key(self):
        """The plaintext token — only available on the in-memory instance
        immediately after creation (shown once). Returns None once reloaded."""
        return self._plaintext

    @key.setter
    def key(self, value):
        self._plaintext = value

    def save(self, *args, **kwargs):
        if not self.digest:
            if not self._plaintext:
                self._plaintext = self.generate_key()
            self.pepper = current_pepper_id()
            self.digest = hash_token(self._plaintext, self.pepper)
            self.key_preview = self._plaintext[:8]
        if not getattr(self, "tenant_id", None):
            from core.managers import get_current_tenant

            tenant = get_current_tenant()
            if not tenant:
                Tenant = apps.get_model("organization", "Tenant")

                tenant = Tenant._base_manager.first()
                if not tenant:
                    tenant = Tenant._base_manager.create(name="Default Tenant", slug="default-tenant")
            self.tenant = tenant
        super().save(*args, **kwargs)

    @staticmethod
    def generate_key():
        return secrets.token_hex(20)

    @classmethod
    def find_by_key(cls, plaintext):
        """Resolve a token from a presented plaintext by comparing HMAC digests
        across all configured peppers (constant set, supports rotation)."""
        if not plaintext:
            return None
        digests = [hash_token(plaintext, pid) for pid in token_peppers()]
        return cls.objects.select_related("user", "tenant").filter(digest__in=digests).first()

    @property
    def is_expired(self):
        if self.expires is None:
            return False
        return timezone.now() >= self.expires

    def validate_client_ip(self, client_ip):
        """
        Return True if the given client IP is permitted to use this token.

        An empty allowed_ips list imposes no restriction. An unparseable client
        IP, or one outside every configured prefix, is rejected (fail closed).
        """
        if not self.allowed_ips:
            return True
        try:
            client_addr = ipaddress.ip_address(client_ip)
        except (ValueError, TypeError):
            return False
        for prefix in self.allowed_ips:
            try:
                if client_addr in ipaddress.ip_network(prefix, strict=False):
                    return True
            except (ValueError, TypeError):
                continue
        return False


class UserGroup(AutoSlugMixin, StandardModel, SoftDeleteMixin):
    """A flat application group owned by one tenant/provider.

    Members are Membership-backed :class:`GroupMembership` rows; permissions are
    additive organization.RoleGrant rows with explicit RoleGrantScope children.
    """

    # The default manager stays deliberately unscoped because groups participate
    # in cross-tenant projections. Every UI/API surface scopes them explicitly.
    # ``all_objects`` keeps recycle-bin/export access tenant-aware.
    objects = SoftDeleteManager()
    all_objects = ExplicitScopeAllObjectsManager()

    name = models.CharField(max_length=100, verbose_name=_("Name"))
    slug = models.SlugField(max_length=100, verbose_name=_("Slug"))
    description = models.TextField(blank=True, verbose_name=_("Description"))
    tenant = models.ForeignKey(
        "organization.Tenant",
        on_delete=models.CASCADE,
        related_name="user_groups",
        verbose_name=_("Tenant"),
    )
    is_active = models.BooleanField(default=True, db_index=True, verbose_name=_("Active"))
    scim_id = models.UUIDField(
        default=uuid.uuid4,
        unique=True,
        editable=False,
        verbose_name=_("SCIM resource ID"),
        help_text=_("Stable opaque identifier emitted by the SCIM API."),
    )
    external_id = models.CharField(
        max_length=255,
        blank=True,
        default="",
        verbose_name=_("External ID"),
        help_text=_("Stable identifier supplied by the provisioning domain that owns this group."),
    )

    archive_operation_id = models.UUIDField(null=True, blank=True, editable=False, verbose_name=_("Archive Operation"))

    class Meta:
        ordering = ["name"]
        verbose_name = _("User Group")
        verbose_name_plural = _("User Groups")
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "name"],
                condition=models.Q(deleted_at__isnull=True),
                name="users_usergroup_unique_tenant_name_active",
            ),
            models.UniqueConstraint(
                fields=["tenant", "slug"],
                condition=models.Q(deleted_at__isnull=True),
                name="users_usergroup_unique_tenant_slug_active",
            ),
            models.UniqueConstraint(
                fields=["tenant", "external_id"],
                condition=models.Q(deleted_at__isnull=True) & ~models.Q(external_id=""),
                name="users_usergroup_unique_tenant_external_id",
            ),
        ]

    def __str__(self):
        return self.name

    def get_absolute_url(self):
        return reverse("users:usergroup_detail", kwargs={"pk": self.pk})

    def clean(self):
        super().clean()
        if self.pk:
            original_tenant_id = type(self)._base_manager.filter(pk=self.pk).values_list("tenant_id", flat=True).first()
            if original_tenant_id is not None and self.tenant_id != original_tenant_id:
                raise ValidationError({"tenant": _("The tenant that owns a user group cannot be changed.")})


class GroupMembership(ChangeLoggingMixin, models.Model):
    """One tenant Membership included in one flat, tenant-owned UserGroup.

    External directory nesting is flattened into these rows during sync; the
    application deliberately has no group-in-group relation.
    """

    SOURCE_MANUAL = "manual"
    SOURCE_SCIM = "scim"
    SOURCE_LDAP = "ldap"
    SOURCE_OIDC = "oidc"
    SOURCE_SAML = "saml"
    SOURCE_CHOICES = [
        (SOURCE_MANUAL, _("Manual")),
        (SOURCE_SCIM, _("SCIM")),
        (SOURCE_LDAP, _("LDAP / Entra ID")),
        (SOURCE_OIDC, _("OIDC")),
        (SOURCE_SAML, _("SAML")),
    ]

    user_group = models.ForeignKey(
        "users.UserGroup",
        on_delete=models.CASCADE,
        related_name="group_memberships",
        verbose_name=_("User group"),
    )
    membership = models.ForeignKey(
        "organization.Membership",
        on_delete=models.CASCADE,
        related_name="group_memberships",
        verbose_name=_("Membership"),
    )
    added_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        related_name="added_group_memberships",
        blank=True,
        null=True,
        verbose_name=_("Added by"),
    )
    source = models.CharField(
        max_length=20,
        choices=SOURCE_CHOICES,
        default=SOURCE_MANUAL,
        db_index=True,
        verbose_name=_("Source"),
    )
    external_id = models.CharField(
        max_length=255,
        blank=True,
        verbose_name=_("External ID"),
        help_text=_("Stable membership identifier supplied by SCIM/LDAP when available."),
    )
    added_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["user_group", "membership"]
        verbose_name = _("Group membership")
        verbose_name_plural = _("Group memberships")
        constraints = [
            models.UniqueConstraint(
                fields=["user_group", "membership"],
                name="users_groupmembership_unique_member",
            ),
            models.UniqueConstraint(
                fields=["user_group", "source", "external_id"],
                condition=~models.Q(external_id=""),
                name="users_groupmembership_unique_external_id",
            ),
        ]
        indexes = [
            models.Index(
                fields=["user_group", "source", "external_id"],
                name="users_groupmember_external_idx",
            ),
        ]

    @property
    def tenant(self):
        return self.user_group.tenant

    def __str__(self):
        return f"{self.membership} in {self.user_group}"

    def clean(self):
        super().clean()
        if not self.user_group_id or not self.membership_id:
            return
        if self.user_group.tenant_id is None:
            raise ValidationError({"user_group": _("Choose a user group that belongs to a tenant.")})
        if self.membership.tenant_id != self.user_group.tenant_id:
            raise ValidationError({"membership": _("Choose a member of this group's tenant.")})

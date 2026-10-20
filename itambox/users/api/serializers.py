from collections.abc import Mapping

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.utils.translation import gettext_lazy as _
from rest_framework import serializers

from itambox.api.base import BaseModelSerializer, reject_unknown_or_writableless
from users.models import Token, UserPreference

User = get_user_model()


class UserSerializer(BaseModelSerializer):
    url = serializers.HyperlinkedIdentityField(view_name="api:users_api:user-detail")

    class Meta:
        model = User
        fields = [
            "id",
            "url",
            "username",
            "first_name",
            "last_name",
            "email",
            "is_staff",
            "is_active",
            "can_login",
            "date_joined",
        ]
        brief_fields = ["id", "url", "username"]


class GroupSerializer(BaseModelSerializer):
    url = serializers.HyperlinkedIdentityField(view_name="api:users_api:group-detail")
    user_count = serializers.IntegerField(read_only=True)

    class Meta:
        model = Group
        fields = ["id", "url", "name", "user_count"]
        brief_fields = ["id", "url", "name"]


class UserConfigSerializer(BaseModelSerializer):
    class Meta:
        model = UserPreference
        fields = ["data"]
        read_only_fields = ["data"]
        brief_fields = ["data"]


class UserConfigUpdateSerializer(serializers.Serializer):
    tables = serializers.JSONField(required=False)
    theme = serializers.JSONField(required=False)
    pagination = serializers.JSONField(required=False)
    language = serializers.CharField(required=False)

    def validate_theme(self, value):
        """Accept exactly the documented theme choices (light, dark, automatic)."""
        if not isinstance(value, dict):
            return value
        stored_theme = value.get("theme")
        if stored_theme is not None and stored_theme not in dict(UserPreference.THEME_CHOICES):
            raise serializers.ValidationError(_("Select a valid theme: light, dark or automatic."))
        return value

    def validate(self, attrs):
        initial = getattr(self, "initial_data", None)
        if not isinstance(initial, Mapping):
            return attrs

        reject_unknown_or_writableless(initial, self.fields)

        for field_name in ("tables", "theme", "pagination"):
            if field_name in initial and not isinstance(initial[field_name], dict):
                raise serializers.ValidationError({field_name: _("Expected a dictionary.")})

        if "tables" in initial:
            for models in initial["tables"].values():
                if not isinstance(models, dict):
                    raise serializers.ValidationError({"tables": _("Each app label value must be a dictionary.")})
                if any(not isinstance(config, dict) for config in models.values()):
                    raise serializers.ValidationError({"tables": _("Each table value must be a dictionary.")})

        if "language" in initial and not isinstance(initial["language"], str):
            raise serializers.ValidationError({"language": _("Expected a string.")})

        return attrs


class TokenSerializer(serializers.ModelSerializer):
    user = serializers.StringRelatedField(read_only=True)
    user_id = serializers.PrimaryKeyRelatedField(queryset=User.objects.all(), source="user", write_only=True)
    # The plaintext key is only present on the response to the CREATE call
    # (shown once); subsequent reads return null. `key_preview` is the
    # non-secret identifier shown in listings.
    key = serializers.ReadOnlyField()

    class Meta:
        model = Token
        fields = [
            "id",
            "key",
            "key_preview",
            "user",
            "user_id",
            "created",
            "expires",
            "last_used",
            "write_enabled",
            "allowed_ips",
            "description",
        ]
        read_only_fields = ["key_preview", "created", "last_used"]
        brief_fields = ["id", "key_preview", "user", "created"]

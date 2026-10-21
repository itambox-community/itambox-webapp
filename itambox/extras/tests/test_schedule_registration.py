"""Guards for the post-migrate schedule registration on half-migrated databases.

The extras app registers the alert and webhook-recovery schedules from a
``post_migrate`` receiver instead of gating on migration names that the named
baseline no longer contains. Both early exits therefore carry the safety
property: a database that is not ready for the schedules must not gain rows
that point at task paths it cannot execute yet.
"""

from unittest import mock

import pytest
from django.apps import apps

from extras.apps import ALERT_TASK_PATH, WEBHOOK_RECOVERY_TASK_PATH


@pytest.fixture()
def extras_config():
    return apps.get_app_config("extras")


def test_registration_is_skipped_when_the_migration_recorder_has_no_table(extras_config):
    """A database without ``django_migrations`` gains no periodic tasks."""

    with (
        mock.patch("extras.apps.MigrationRecorder.has_table", return_value=False),
        mock.patch("extras.apps.register_schedule") as register_schedule,
    ):
        extras_config._register_alert_schedule(sender=None)

    register_schedule.assert_not_called()


def test_registration_is_skipped_before_the_extras_schema_is_applied(extras_config):
    """A half-migrated database gains no task path its code cannot execute yet."""

    recorder = mock.Mock()
    recorder.has_table.return_value = True
    recorder.migration_qs.filter.return_value.exists.return_value = False

    with (
        mock.patch("extras.apps.MigrationRecorder", return_value=recorder),
        mock.patch("extras.apps.register_schedule") as register_schedule,
    ):
        extras_config._register_alert_schedule(sender=None)

    register_schedule.assert_not_called()


def test_registration_creates_both_canonical_schedules(extras_config):
    """Once the prerequisite is applied, exactly the two canonical tasks appear."""

    recorder = mock.Mock()
    recorder.has_table.return_value = True
    recorder.migration_qs.filter.return_value.exists.return_value = True

    with (
        mock.patch("extras.apps.MigrationRecorder", return_value=recorder),
        mock.patch("extras.apps.register_schedule") as register_schedule,
    ):
        extras_config._register_alert_schedule(sender=None)

    assert [call.args[0] for call in register_schedule.call_args_list] == [
        ALERT_TASK_PATH,
        WEBHOOK_RECOVERY_TASK_PATH,
    ]

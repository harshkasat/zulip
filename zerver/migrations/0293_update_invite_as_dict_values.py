# Generated by Django 2.2.13 on 2020-06-21 21:13

from django.db import migrations
from django.db.backends.postgresql.schema import BaseDatabaseSchemaEditor
from django.db.migrations.state import StateApps


def update_invite_as_dict_values(apps: StateApps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    PreregistrationUser = apps.get_model("zerver", "PreregistrationUser")
    MultiuseInvite = apps.get_model("zerver", "MultiuseInvite")

    OLD_INVITE_AS_DICT = dict(
        MEMBER=1,
        REALM_ADMIN=2,
        GUEST_USER=3,
        REALM_OWNER=4,
    )
    NEW_INVITE_AS_DICT = dict(
        REALM_OWNER=100,
        REALM_ADMIN=200,
        MEMBER=400,
        GUEST_USER=600,
    )

    PreregistrationUser.objects.filter(invited_as=OLD_INVITE_AS_DICT["REALM_OWNER"]).update(
        invited_as=NEW_INVITE_AS_DICT["REALM_OWNER"]
    )
    PreregistrationUser.objects.filter(invited_as=OLD_INVITE_AS_DICT["REALM_ADMIN"]).update(
        invited_as=NEW_INVITE_AS_DICT["REALM_ADMIN"]
    )
    PreregistrationUser.objects.filter(invited_as=OLD_INVITE_AS_DICT["MEMBER"]).update(
        invited_as=NEW_INVITE_AS_DICT["MEMBER"]
    )
    PreregistrationUser.objects.filter(invited_as=OLD_INVITE_AS_DICT["GUEST_USER"]).update(
        invited_as=NEW_INVITE_AS_DICT["GUEST_USER"]
    )

    MultiuseInvite.objects.filter(invited_as=OLD_INVITE_AS_DICT["REALM_OWNER"]).update(
        invited_as=NEW_INVITE_AS_DICT["REALM_OWNER"]
    )
    MultiuseInvite.objects.filter(invited_as=OLD_INVITE_AS_DICT["REALM_ADMIN"]).update(
        invited_as=NEW_INVITE_AS_DICT["REALM_ADMIN"]
    )
    MultiuseInvite.objects.filter(invited_as=OLD_INVITE_AS_DICT["MEMBER"]).update(
        invited_as=NEW_INVITE_AS_DICT["MEMBER"]
    )
    MultiuseInvite.objects.filter(invited_as=OLD_INVITE_AS_DICT["GUEST_USER"]).update(
        invited_as=NEW_INVITE_AS_DICT["GUEST_USER"]
    )


class Migration(migrations.Migration):
    dependencies = [
        ("zerver", "0292_update_default_value_of_invited_as"),
    ]

    operations = [
        migrations.RunPython(
            update_invite_as_dict_values, reverse_code=migrations.RunPython.noop, elidable=True
        ),
    ]

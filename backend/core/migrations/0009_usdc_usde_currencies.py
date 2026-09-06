from django.db import migrations


STABLES = (
    ("USDC", "USD Coin"),
    ("USDE", "Ethena USDe"),
)


def add_usd_stables(apps, schema_editor):
    Currency = apps.get_model("core", "Currency")
    for code, name in STABLES:
        Currency.objects.update_or_create(
            code=code,
            defaults={"name": name, "status": "active"},
        )


def remove_usd_stables(apps, schema_editor):
    Currency = apps.get_model("core", "Currency")
    Currency.objects.filter(code__in=[code for code, _ in STABLES]).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0008_alter_accountbalancesnapshot_balance"),
    ]

    operations = [
        migrations.RunPython(add_usd_stables, remove_usd_stables),
    ]

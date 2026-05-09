from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0004_transactionlinkgroup_transactionlinkitem_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="transaction",
            name="is_split_parent",
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name="transaction",
            name="parent_transaction",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="split_children",
                to="core.transaction",
            ),
        ),
    ]

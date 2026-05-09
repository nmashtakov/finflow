from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("transactions", "0002_import_categorization_models"),
    ]

    operations = [
        migrations.AddField(
            model_name="importedtransaction",
            name="is_split_parent",
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name="importedtransaction",
            name="split_from",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="split_children",
                to="transactions.importedtransaction",
            ),
        ),
    ]

from django.db import migrations, models
import django.db.models.deletion


def fill_purchase_warehouse(apps, schema_editor):
    Purchase = apps.get_model('crm', 'Purchase')

    for purchase in Purchase.objects.filter(warehouse__isnull=True).iterator():
        first_item = purchase.items.order_by('id').first()
        if first_item and first_item.warehouse_id:
            purchase.warehouse_id = first_item.warehouse_id
            purchase.save(update_fields=['warehouse'])


class Migration(migrations.Migration):

    dependencies = [
        ('crm', '0007_order_customer_counterparty'),
    ]

    operations = [
        migrations.AddField(
            model_name='purchase',
            name='warehouse',
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name='purchases',
                to='crm.warehouse',
            ),
        ),
        migrations.RunPython(fill_purchase_warehouse, migrations.RunPython.noop),
    ]

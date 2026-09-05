from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('crm', '0006_deliverymethod_order_orderitem_stockreservation_and_more'),
    ]

    operations = [
        migrations.AddField(
            model_name='order',
            name='customer_counterparty',
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name='customer_orders',
                to='crm.counterparty',
            ),
        ),
    ]

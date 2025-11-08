from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0002_currency'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='UserPreferences',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('default_account', models.ForeignKey(blank=True, null=True, on_delete=models.SET_NULL, related_name='preferred_by_users', to='core.account')),
                ('default_project', models.ForeignKey(blank=True, null=True, on_delete=models.SET_NULL, related_name='preferred_by_users', to='core.project')),
                ('user', models.OneToOneField(on_delete=models.CASCADE, related_name='preferences', to=settings.AUTH_USER_MODEL)),
            ],
        ),
    ]

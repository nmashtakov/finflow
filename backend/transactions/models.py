from django.conf import settings
from django.db import models


class TransactionImportSession(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='transaction_imports')
    created_at = models.DateTimeField(auto_now_add=True)
    original_name = models.CharField(max_length=255)
    columns = models.JSONField()
    sample_rows = models.JSONField()
    rows = models.JSONField()
    metadata = models.JSONField(default=dict, blank=True)

    def __str__(self):
        return f"Import {self.original_name} ({self.created_at:%Y-%m-%d %H:%M})"

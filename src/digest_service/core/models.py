from django.db import models


class ValidatedModel(models.Model):
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True

    def save(self, *args, **kwargs):
        self.full_clean()
        return super().save(*args, **kwargs)


class SeedSnapshot(ValidatedModel):
    filename = models.CharField("Файл", max_length=120)
    sha256 = models.CharField("SHA-256", max_length=64)
    payload = models.JSONField("Исходные данные")

    class Meta:
        verbose_name = "Снимок начальных данных"
        verbose_name_plural = "Снимки начальных данных"
        constraints = [
            models.UniqueConstraint(fields=["filename", "sha256"], name="unique_seed_snapshot")
        ]

    def __str__(self):
        return self.filename

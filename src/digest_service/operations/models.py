import uuid

from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone

from digest_service.core.models import ValidatedModel


class BackgroundJob(ValidatedModel):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    kind = models.CharField(
        "Тип",
        max_length=40,
        choices=[
            ("collect_source", "Собрать источник"),
            ("suggest_groupings", "Предложить группировки"),
            ("discover_editorial_candidates", "Подготовить редакторский отбор"),
            ("automatic_digest_cycle", "Автоматически собрать и опубликовать выпуск"),
            ("generate_event_cards", "Создать карточки RU/EN"),
            ("build_editorial_editions", "Собрать выпуски RU/EN"),
            ("erase_subscriber", "Удалить данные подписчика"),
            ("purge_privacy_records", "Очистить истёкшие privacy-записи"),
            ("create_database_backup", "Создать резервную копию базы"),
            ("purge_backup_artifacts", "Удалить истёкшие файлы копий"),
            ("export_erasure_guard", "Экспортировать реестр удалений"),
            ("prepare_schedule_slot", "Подготовить слот рассылки"),
            ("purge_source_text", "Удалить истёкшие тексты"),
        ],
    )
    contract_version = models.PositiveSmallIntegerField(default=1, editable=False)
    parameters = models.JSONField(default=dict, blank=True)
    idempotency_key = models.CharField(max_length=180, unique=True)
    status = models.CharField(
        "Состояние",
        max_length=15,
        default="pending",
        choices=[
            ("pending", "Ожидает"),
            ("running", "Выполняется"),
            ("retry_wait", "Ожидает повтора"),
            ("done", "Выполнено"),
            ("failed", "Ошибка"),
            ("cancelled", "Отменено"),
        ],
    )
    attempts = models.PositiveSmallIntegerField(default=0)
    max_attempts = models.PositiveSmallIntegerField(default=5)
    next_attempt_at = models.DateTimeField(default=timezone.now)
    lease_owner = models.CharField(max_length=120, blank=True)
    lease_expires_at = models.DateTimeField(null=True, blank=True)
    last_error_code = models.CharField(max_length=100, blank=True)
    result = models.JSONField(default=dict, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["next_attempt_at", "created_at"]
        verbose_name = "Фоновое задание"
        verbose_name_plural = "Фоновые задания"

    def clean(self):
        if not isinstance(self.parameters, dict):
            raise ValidationError({"parameters": "Параметры должны быть объектом JSON."})
        if self.max_attempts < 1:
            raise ValidationError({"max_attempts": "Нужна хотя бы одна попытка."})
        for field in ("next_attempt_at", "lease_expires_at", "finished_at"):
            value = getattr(self, field)
            if value and timezone.is_naive(value):
                raise ValidationError({field: "Дата должна содержать часовой пояс."})

    def __str__(self):
        return f"{self.kind} · {self.id}"


class OutboxEntry(ValidatedModel):
    job = models.OneToOneField(BackgroundJob, on_delete=models.CASCADE, related_name="outbox")
    status = models.CharField(
        max_length=12,
        default="pending",
        choices=[
            ("pending", "Ожидает"),
            ("published", "Передано брокеру"),
            ("consumed", "Принято исполнителем"),
            ("failed", "Ошибка публикации"),
        ],
    )
    attempts = models.PositiveIntegerField(default=0)
    next_attempt_at = models.DateTimeField(default=timezone.now)
    broker_message_id = models.CharField(max_length=100, blank=True)
    last_error_code = models.CharField(max_length=100, blank=True)

    class Meta:
        ordering = ["next_attempt_at", "created_at"]
        verbose_name = "Запись outbox"
        verbose_name_plural = "Transactional outbox"

    def clean(self):
        if self.next_attempt_at and timezone.is_naive(self.next_attempt_at):
            raise ValidationError({"next_attempt_at": "Дата должна содержать часовой пояс."})

    def __str__(self):
        return f"Outbox · {self.job_id}"


class ProcessLease(ValidatedModel):
    name = models.CharField(primary_key=True, max_length=80)
    owner = models.CharField(max_length=160)
    lease_expires_at = models.DateTimeField()
    heartbeat_at = models.DateTimeField(default=timezone.now)

    class Meta:
        verbose_name = "Аренда процесса"
        verbose_name_plural = "Процессы и heartbeat"

    def clean(self):
        for field in ("lease_expires_at", "heartbeat_at"):
            value = getattr(self, field)
            if value and timezone.is_naive(value):
                raise ValidationError({field: "Дата должна содержать часовой пояс."})

    def __str__(self):
        return f"{self.name} · {self.owner}"


class BackupRun(ValidatedModel):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    request_key = models.CharField(max_length=180, unique=True, default=uuid.uuid4, editable=False)
    kind = models.CharField(
        "Состав копии",
        max_length=24,
        choices=[("database", "База данных"), ("erasure_guard", "Реестр удалений")],
    )
    database_backend = models.CharField(max_length=24)
    storage_class = models.CharField(
        "Хранилище",
        max_length=24,
        choices=[("local_training", "Локальная учебная копия"), ("external", "Внешнее")],
    )
    status = models.CharField(
        "Состояние",
        max_length=12,
        default="running",
        choices=[
            ("running", "Выполняется"),
            ("succeeded", "Готово"),
            ("failed", "Ошибка"),
            ("expired", "Файл удалён по сроку"),
        ],
    )
    encrypted = models.BooleanField(default=False)
    artifact_name = models.CharField(max_length=240, blank=True)
    sha256 = models.CharField(max_length=64, blank=True)
    size_bytes = models.PositiveBigIntegerField(null=True, blank=True)
    manifest = models.JSONField(default=dict, blank=True)
    error_code = models.CharField(max_length=100, blank=True)
    expires_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Резервная копия"
        verbose_name_plural = "Резервные копии"

    def clean(self):
        if self.status == "succeeded":
            missing = [
                name
                for name in ("artifact_name", "sha256")
                if not getattr(self, name)
            ]
            if missing or self.size_bytes is None or self.finished_at is None:
                raise ValidationError("У готовой копии должны быть файл, хеш, размер и время.")
        if self.storage_class == "external" and self.status == "succeeded" and not self.encrypted:
            raise ValidationError("Внешняя резервная копия должна быть зашифрована.")
        for field in ("expires_at", "finished_at"):
            value = getattr(self, field)
            if value and timezone.is_naive(value):
                raise ValidationError({field: "Дата должна содержать часовой пояс."})

    def __str__(self):
        return f"{self.kind} · {self.created_at:%Y-%m-%d %H:%M} · {self.status}"


class RestoreDrill(ValidatedModel):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    backup = models.ForeignKey(BackupRun, on_delete=models.PROTECT, related_name="restore_drills")
    status = models.CharField(
        "Состояние",
        max_length=12,
        default="running",
        choices=[
            ("running", "Выполняется"),
            ("succeeded", "Успешно"),
            ("failed", "Ошибка"),
        ],
    )
    isolated = models.BooleanField(default=True, editable=False)
    checks = models.JSONField(default=dict, blank=True)
    error_code = models.CharField(max_length=100, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Проверка восстановления"
        verbose_name_plural = "Проверки восстановления"

    def clean(self):
        if self.finished_at and timezone.is_naive(self.finished_at):
            raise ValidationError({"finished_at": "Дата должна содержать часовой пояс."})
        if self.status in {"succeeded", "failed"} and self.finished_at is None:
            raise ValidationError("Завершённой проверке нужно время завершения.")

    def __str__(self):
        return f"{self.backup_id} · {self.status}"


class DeliverySafetyState(ValidatedModel):
    key = models.CharField(primary_key=True, max_length=24, default="global", editable=False)
    status = models.CharField(
        "Состояние доставки",
        max_length=12,
        default="blocked",
        choices=[("blocked", "Заблокирована"), ("ready", "Разрешена")],
    )
    applied_guard_sha256 = models.CharField(max_length=64, blank=True)
    reason = models.CharField(max_length=100, blank=True)
    applied_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = "Безопасность доставки"
        verbose_name_plural = "Безопасность доставки"

    def clean(self):
        if self.status == "ready" and (not self.applied_guard_sha256 or not self.applied_at):
            raise ValidationError("Для разрешения доставки нужен применённый guard.")
        if self.applied_at and timezone.is_naive(self.applied_at):
            raise ValidationError({"applied_at": "Дата должна содержать часовой пояс."})

    def __str__(self):
        return f"Доставка · {self.status}"

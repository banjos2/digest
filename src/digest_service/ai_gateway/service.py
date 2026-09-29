from datetime import date
from decimal import ROUND_UP, Decimal

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from .models import AIBudgetPeriod, AIRequest

MONEY_QUANTUM = Decimal("0.000001")


class AIBudgetExceeded(RuntimeError):
    pass


def _money(value):
    return Decimal(str(value)).quantize(MONEY_QUANTUM, rounding=ROUND_UP)


def _period_start(now):
    return date(now.year, now.month, 1)


@transaction.atomic
def _start_request(*, operation, input_hash, provider, input_characters):
    provider_name = getattr(provider, "provider_name", type(provider).__name__)
    model = provider.model_for(operation) if hasattr(provider, "model_for") else provider_name
    metered = bool(getattr(provider, "metered", False))
    estimate = _money(
        provider.estimated_max_cost(operation, input_characters) if metered else Decimal("0")
    )
    now = timezone.now()
    budget = None
    if metered:
        budget, _ = AIBudgetPeriod.objects.select_for_update().get_or_create(
            period_start=_period_start(now),
            defaults={"limit_usd": Decimal(settings.AI_MONTHLY_BUDGET_USD)},
        )
        if estimate > budget.available_usd:
            request = AIRequest.objects.create(
                operation=operation,
                provider=provider_name,
                model=model,
                input_hash=input_hash,
                status="budget_rejected",
                error_code="monthly_budget_exceeded",
                started_at=now,
                finished_at=now,
            )
            return request, budget
        budget.reserved_usd += estimate
        budget.save()
    request = AIRequest.objects.create(
        operation=operation,
        provider=provider_name,
        model=model,
        input_hash=input_hash,
        reserved_cost_usd=estimate,
        started_at=now,
    )
    return request, budget


@transaction.atomic
def _finish_request(request_id, *, status, result=None, error_code=""):
    request = AIRequest.objects.select_for_update().get(pk=request_id)
    budget = None
    if request.reserved_cost_usd:
        budget = AIBudgetPeriod.objects.select_for_update().get(
            period_start=_period_start(request.started_at)
        )
        budget.reserved_usd -= request.reserved_cost_usd
    actual_cost = Decimal("0")
    metadata = getattr(result, "metadata", None) if result is not None else None
    if status == "success" and metadata is not None:
        request.provider_request_id = metadata.request_id[:200]
        request.input_tokens = metadata.input_tokens
        request.output_tokens = metadata.output_tokens
        actual_cost = _money(metadata.actual_cost_usd)
    request.actual_cost_usd = actual_cost
    request.status = status
    request.error_code = error_code
    request.finished_at = timezone.now()
    request.save()
    if budget is not None:
        budget.spent_usd += actual_cost
        budget.save()


def run_provider_call(*, operation, input_hash, input_characters, provider, callback):
    request, _ = _start_request(
        operation=operation,
        input_hash=input_hash,
        provider=provider,
        input_characters=input_characters,
    )
    if request.status == "budget_rejected":
        raise AIBudgetExceeded(f"AI request {request.pk} exceeds the monthly budget")
    try:
        result = callback()
    except Exception as error:
        code = (
            "invalid_provider_output"
            if isinstance(error, ValidationError)
            else "provider_call_failed"
        )
        _finish_request(request.pk, status="failed", error_code=code)
        raise
    _finish_request(request.pk, status="success", result=result)
    return result

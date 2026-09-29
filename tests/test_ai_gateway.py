from decimal import Decimal
from types import SimpleNamespace

from django.test import TestCase, override_settings

from digest_service.ai_gateway.models import AIBudgetPeriod, AIRequest
from digest_service.ai_gateway.openrouter_provider import OpenRouterProvider
from digest_service.ai_gateway.service import AIBudgetExceeded, run_provider_call
from digest_service.editorial.providers import EventBrief, EvidenceItem, ProviderCallMetadata


class MeteredFixtureProvider:
    provider_name = "paid-fixture"
    metered = True

    def model_for(self, operation):
        return "fixture-model"

    def estimated_max_cost(self, operation, input_characters):
        return Decimal("0.10")


class FakeCompletions:
    def __init__(self):
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if kwargs["model"] == "openai/gpt-5.4":
            content = (
                '{"title":"Подтверждён выпуск открытой модели ИИ",'
                '"summary":"Два материала сообщают о выпуске открытой модели ИИ и описывают '
                'условия лицензии, доступность весов и основные ограничения релиза.",'
                '"citation_membership_ids":[7]}'
            )
            usage = SimpleNamespace(prompt_tokens=100, completion_tokens=50)
        else:
            content = (
                '{"title":"An open AI model has been released",'
                '"summary":"Two reports describe the release of an open AI model, including '
                'its licence, the availability of model weights and the main limitations."}'
            )
            usage = SimpleNamespace(prompt_tokens=80, completion_tokens=40)
        message = SimpleNamespace(content=content)
        return SimpleNamespace(
            id=f"response-{len(self.calls)}",
            usage=usage,
            choices=[SimpleNamespace(message=message)],
        )


class AIGatewayTests(TestCase):
    def test_zero_default_budget_rejects_before_callback(self):
        called = False

        def callback():
            nonlocal called
            called = True

        with self.assertRaises(AIBudgetExceeded):
            run_provider_call(
                operation="draft_ru",
                input_hash="a" * 64,
                input_characters=100,
                provider=MeteredFixtureProvider(),
                callback=callback,
            )
        self.assertFalse(called)
        request = AIRequest.objects.get()
        self.assertEqual(request.status, "budget_rejected")

    @override_settings(AI_MONTHLY_BUDGET_USD="0.20")
    def test_reservation_becomes_actual_spend(self):
        result = SimpleNamespace(
            metadata=ProviderCallMetadata("provider-1", 100, 50, Decimal("0.05"))
        )
        returned = run_provider_call(
            operation="draft_ru",
            input_hash="b" * 64,
            input_characters=100,
            provider=MeteredFixtureProvider(),
            callback=lambda: result,
        )
        self.assertIs(returned, result)
        request = AIRequest.objects.get()
        budget = AIBudgetPeriod.objects.get()
        self.assertEqual(request.actual_cost_usd, Decimal("0.050000"))
        self.assertEqual(budget.reserved_usd, Decimal("0"))
        self.assertEqual(budget.spent_usd, Decimal("0.050000"))

    def test_openrouter_adapter_uses_structured_private_requests(self):
        completions = FakeCompletions()
        client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
        provider = OpenRouterProvider(client=client)
        event = EventBrief(
            event_id="event-1",
            working_title="Open model release",
            evidence=(
                EvidenceItem(
                    membership_id=7,
                    title="Open model released",
                    body="The publisher released weights and documented the licence.",
                    source_name="Fixture News",
                    source_url="https://example.com/news",
                    language="en",
                    role="primary",
                ),
            ),
        )
        russian = provider.draft_russian(event)
        english = provider.translate_english(title=russian.title, summary=russian.summary)
        self.assertEqual(russian.citation_membership_ids, (7,))
        self.assertEqual(russian.metadata.request_id, "response-1")
        self.assertEqual(english.metadata.request_id, "response-2")
        self.assertEqual(completions.calls[0]["model"], "openai/gpt-5.4")
        self.assertEqual(completions.calls[1]["model"], "openai/gpt-5-mini")
        self.assertEqual(completions.calls[0]["response_format"]["type"], "json_schema")
        self.assertTrue(completions.calls[0]["extra_body"]["provider"]["zdr"])
        self.assertEqual(completions.calls[0]["extra_body"]["provider"]["data_collection"], "deny")
        self.assertEqual(russian.metadata.actual_cost_usd, Decimal("0.001"))

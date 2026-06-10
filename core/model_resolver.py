"""
OpenWorker Model Resolver v0.1
================================
Reads the model + cost sections of a worker spec and returns
the correct LLM client. The rest of the runtime never imports
anthropic, openai, or ollama directly — it only calls this.

Switching a worker from Claude to a local Llama model = two
lines in the YAML. No code change anywhere else.

Supported providers:
  anthropic   Claude API (cloud)
  openai      OpenAI API (cloud)
  ollama      Local model via Ollama — fully on-prem
  openrouter  Routes to cheapest available model automatically
  vllm        Self-hosted vLLM inside company VPC
  custom      Any OpenAI-compatible endpoint
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any


# ─────────────────────────────────────────────
# COST TRACKER
# ─────────────────────────────────────────────

@dataclass
class CostTracker:
    """
    Tracks token usage and estimated cost per task and per session.
    Updated after every LLM call. Enforces budget caps.
    """
    monthly_budget_usd:    float = 500.0
    per_task_budget_usd:   float = 2.0
    budget_alert_percent:  float = 80.0
    human_equivalent_usd:  float = 55000.0

    # Accumulated this billing period
    month_spent_usd:       float = 0.0
    month_input_tokens:    int   = 0
    month_output_tokens:   int   = 0

    # Current task
    task_spent_usd:        float = 0.0
    task_input_tokens:     int   = 0
    task_output_tokens:    int   = 0

    on_budget_exceeded:    str   = "pause_and_notify"

    # Pricing per 1M tokens (USD) — update as providers change rates
    PRICING: dict = field(default_factory=lambda: {
        # (input_per_1m, output_per_1m)
        "anthropic/claude-sonnet-4-6":   (3.00,  15.00),
        "anthropic/claude-haiku-4-5":    (0.25,   1.25),
        "openai/gpt-4o":                 (2.50,  10.00),
        "openai/gpt-4o-mini":            (0.15,   0.60),
        "ollama/*":                      (0.00,   0.00),   # free — running locally
        "vllm/*":                        (0.00,   0.00),   # free — self-hosted
        "openrouter/*":                  (0.50,   1.50),   # approximate average
    })

    def record_usage(
        self,
        provider: str,
        model: str,
        input_tokens: int,
        output_tokens: int,
    ) -> "CostRecord":
        key = f"{provider}/{model}"
        # Find the best matching pricing key
        pricing = self.PRICING.get(key) or self.PRICING.get(f"{provider}/*") or (1.0, 2.0)
        input_cost  = (input_tokens  / 1_000_000) * pricing[0]
        output_cost = (output_tokens / 1_000_000) * pricing[1]
        total_cost  = input_cost + output_cost

        self.task_spent_usd    += total_cost
        self.task_input_tokens  += input_tokens
        self.task_output_tokens += output_tokens
        self.month_spent_usd   += total_cost
        self.month_input_tokens += input_tokens
        self.month_output_tokens += output_tokens

        return CostRecord(
            provider=provider,
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=round(total_cost, 6),
        )

    def check_budget(self) -> "BudgetStatus":
        """Call before every LLM invocation. Runtime acts on the result."""
        month_pct = (self.month_spent_usd / self.monthly_budget_usd) * 100

        if self.task_spent_usd >= self.per_task_budget_usd:
            return BudgetStatus(
                ok=False,
                reason="per_task_limit_exceeded",
                message=f"Task spent ${self.task_spent_usd:.4f} — limit is ${self.per_task_budget_usd}. "
                        f"Stopping task. Manager notified.",
                action=self.on_budget_exceeded,
            )

        if self.month_spent_usd >= self.monthly_budget_usd:
            return BudgetStatus(
                ok=False,
                reason="monthly_budget_exceeded",
                message=f"Monthly spend ${self.month_spent_usd:.2f} hit cap of ${self.monthly_budget_usd}. "
                        f"Worker paused until next billing period or manager overrides.",
                action=self.on_budget_exceeded,
            )

        if month_pct >= self.budget_alert_percent:
            return BudgetStatus(
                ok=True,   # still running — just a warning
                reason="budget_alert",
                message=f"Monthly spend at {month_pct:.0f}% of budget "
                        f"(${self.month_spent_usd:.2f} of ${self.monthly_budget_usd}). "
                        f"Manager notified.",
                action="notify_only",
            )

        return BudgetStatus(ok=True, reason="within_budget")

    def roi_summary(self) -> dict:
        """
        Returns the CFO-friendly comparison shown in the Manager Dashboard.
        Answers: 'Is this AI worker cheaper than a human?'
        """
        human_monthly  = self.human_equivalent_usd / 12
        ai_monthly     = self.month_spent_usd
        savings_pct    = ((human_monthly - ai_monthly) / human_monthly * 100) if human_monthly else 0

        return {
            "human_monthly_cost_usd": round(human_monthly, 2),
            "ai_monthly_cost_usd":    round(ai_monthly, 4),
            "monthly_savings_usd":    round(human_monthly - ai_monthly, 2),
            "savings_percent":        round(savings_pct, 1),
            "verdict": (
                "AI worker is significantly cheaper"  if savings_pct > 90 else
                "AI worker is cheaper"                if savings_pct > 50 else
                "AI worker is marginally cheaper"     if savings_pct > 0  else
                "AI worker exceeds human cost — review usage"
            ),
        }

    def reset_task(self):
        self.task_spent_usd     = 0.0
        self.task_input_tokens  = 0
        self.task_output_tokens = 0


@dataclass
class CostRecord:
    provider:      str
    model:         str
    input_tokens:  int
    output_tokens: int
    cost_usd:      float


@dataclass
class BudgetStatus:
    ok:      bool
    reason:  str
    message: str = ""
    action:  str = ""


# ─────────────────────────────────────────────
# MODEL RESOLVER
# ─────────────────────────────────────────────

class ModelResolver:
    """
    Given a worker spec's model section, returns a configured LLM client
    and a CostTracker wired to the spec's cost section.

    The runtime calls:
        client, tracker = ModelResolver(spec).resolve()

    Then for every LLM call:
        status = tracker.check_budget()
        if not status.ok: handle_budget_exceeded(status)

        response = client.complete(messages)
        tracker.record_usage(provider, model, response.usage)
    """

    def __init__(self, spec_data: dict):
        self._model = spec_data.get("model", {})
        self._cost  = spec_data.get("cost", {})

    def resolve(self) -> tuple[Any, CostTracker]:
        """Returns (llm_client, cost_tracker)."""
        provider = self._model.get("provider", "anthropic").lower()
        model    = self._model.get("name", "claude-sonnet-4-6")
        base_url = self._model.get("base_url")
        key_env  = self._model.get("api_key_env", "")
        require_on_prem = self._model.get("require_on_prem", False)

        # Enforce on-prem requirement
        CLOUD_PROVIDERS = {"anthropic", "openai", "openrouter"}
        if require_on_prem and provider in CLOUD_PROVIDERS:
            raise RuntimeError(
                f"Worker spec sets require_on_prem: true "
                f"but provider is '{provider}' (cloud). "
                f"Switch provider to ollama or vllm, or set require_on_prem: false."
            )

        client = self._build_client(provider, model, base_url, key_env)
        tracker = self._build_tracker()

        return client, tracker

    def _build_client(self, provider, model, base_url, key_env):
        api_key = os.environ.get(key_env, "") if key_env else ""

        if provider == "anthropic":
            # pip install anthropic
            import anthropic
            return anthropic.Anthropic(api_key=api_key)

        elif provider == "openai":
            # pip install openai
            import openai
            return openai.OpenAI(api_key=api_key)

        elif provider == "ollama":
            # pip install openai  (Ollama exposes OpenAI-compatible API)
            import openai
            return openai.OpenAI(
                base_url=base_url or "http://ollama:11434/v1",
                api_key="ollama",          # Ollama doesn't need a real key
            )

        elif provider == "vllm":
            # pip install openai  (vLLM also exposes OpenAI-compatible API)
            import openai
            return openai.OpenAI(
                base_url=base_url or "http://vllm-server:8000/v1",
                api_key=api_key or "vllm",
            )

        elif provider == "openrouter":
            import openai
            return openai.OpenAI(
                base_url="https://openrouter.ai/api/v1",
                api_key=api_key,
            )

        elif provider == "custom":
            import openai
            if not base_url:
                raise ValueError("provider: custom requires base_url to be set in the worker spec.")
            return openai.OpenAI(base_url=base_url, api_key=api_key or "custom")

        else:
            raise ValueError(
                f"Unknown model provider: '{provider}'. "
                f"Valid options: anthropic | openai | ollama | openrouter | vllm | custom"
            )

    def _build_tracker(self) -> CostTracker:
        c = self._cost
        return CostTracker(
            monthly_budget_usd   = c.get("monthly_budget_usd", 500.0),
            per_task_budget_usd  = c.get("per_task_budget_usd", 2.0),
            budget_alert_percent = c.get("budget_alert_percent", 80.0),
            human_equivalent_usd = c.get("human_equivalent_annual_usd", 55000.0),
            on_budget_exceeded   = c.get("on_budget_exceeded", "pause_and_notify"),
        )


# ─────────────────────────────────────────────
# QUICK DEMO
# ─────────────────────────────────────────────

if __name__ == "__main__":
    # Simulate what the worker spec provides
    spec_cloud = {
        "model": {
            "provider": "anthropic",
            "name": "claude-sonnet-4-6",
            "api_key_env": "ANTHROPIC_API_KEY",
            "require_on_prem": False,
        },
        "cost": {
            "human_equivalent_annual_usd": 55000,
            "monthly_budget_usd": 500,
            "budget_alert_percent": 80,
            "per_task_budget_usd": 2.0,
            "on_budget_exceeded": "pause_and_notify",
        },
    }

    spec_local = {
        "model": {
            "provider": "ollama",
            "name": "llama3.1:8b",
            "base_url": "http://ollama:11434",
            "require_on_prem": True,
        },
        "cost": {
            "human_equivalent_annual_usd": 55000,
            "monthly_budget_usd": 50,       # much cheaper — just electricity
            "per_task_budget_usd": 0.0,     # free per call
            "on_budget_exceeded": "notify_only",
        },
    }

    print("=" * 56)
    print("OpenWorker Model Resolver — Demo")
    print("=" * 56)

    for label, spec in [("Cloud (Claude)", spec_cloud), ("Local (Ollama)", spec_local)]:
        print(f"\n--- {label} ---")
        try:
            resolver = ModelResolver(spec)
            client, tracker = resolver.resolve()
            print(f"  Client   : {type(client).__name__}")
            print(f"  Provider : {spec['model']['provider']}")
            print(f"  Model    : {spec['model']['name']}")
            print(f"  Budget   : ${tracker.monthly_budget_usd}/mo")

            # Simulate some usage
            tracker.record_usage(
                spec["model"]["provider"],
                spec["model"]["name"],
                input_tokens=12000,
                output_tokens=3000,
            )
            roi = tracker.roi_summary()
            print(f"  ROI      : {roi['verdict']}")
            print(f"  AI cost  : ${roi['ai_monthly_cost_usd']}/mo  vs  Human: ${roi['human_monthly_cost_usd']}/mo")
            print(f"  Savings  : ${roi['monthly_savings_usd']}/mo ({roi['savings_percent']}%)")

        except ImportError as e:
            print(f"  (library not installed in this env — {e})")
        except Exception as e:
            print(f"  Error: {e}")

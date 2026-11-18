# 0005: Call DeepSeek directly for the benchmark model

**Status:** Accepted (2026-11-17)

## Context

The tool makes paid model calls, and the person running it pays. The per-issue cap of 1 US dollar can only be enforced against known prices. The post-cutoff set also depends on the model's training cutoff, so a published number needs a model whose identity and cutoff can be recorded. Nothing that calls a model can be built until the provider, model, calling method and total budget are fixed.

The tool is also open to any user with an OpenAI-compatible API: the published benchmark is fixed to one model, but a user can point `bugrepro` at their own endpoint, model and key.

Alternatives considered:

- **OpenCode Go**, the subscription already used for development. It exposes an OpenAI-compatible API and a hard monthly allowance, but its DeepSeek model ID is `deepseek-v4.1-flash`, which can move within the 4.1 lifecycle, and it has no budget alert. Its Go plan allows 100 US dollars of DeepSeek V4.1 Flash a month, which is at most 100 issue-runs at the 1-dollar cap.
- **A dated snapshot from OpenAI or Anthropic.** It meets the fixed-snapshot and budget-alert checks directly, but costs more per token and adds a second paid account.
- **A gateway such as OpenRouter**, which can pin `deepseek/deepseek-v4.1-flash-1017`. It adds a routing layer and a second set of limits.
- **Self-hosting the open weights.** It removes per-token cost but needs substantial GPU capacity and operations, which is out of scope.

## Decision

The published benchmark calls DeepSeek directly at `https://api.deepseek.com`, using model ID `deepseek-flash`. DeepSeek documents that ID as the current V4.1 Flash model; it currently serves `DeepSeek-V4.1-Flash-1017`.

The cutoff is May 2025, the latest data acquisition date in DeepSeek's published training-content summary. Per the recording rule, it is stored as `2025-06-01T00:00:00Z` in [`docs/models.json`](../models.json), with the source URL.

Calls go through the OpenAI-compatible HTTP API with `urllib`, not the `openai` SDK. The request and response shape is small, and direct HTTP avoids a dependency whose upgrades and supply chain would sit between the tool and the provider. This matches the GitHub client decision in #22.

The tool reads the endpoint, model and key from `BUGREPRO_BASE_URL`, `BUGREPRO_MODEL` and `BUGREPRO_API_KEY`. The defaults are DeepSeek and `deepseek-flash`; a user can point them at any OpenAI-compatible provider. `.env.example` lists the names only.

The budget ceiling is 20 US dollars. The prepaid DeepSeek balance is below the ceiling, and auto-recharge is off, so the balance is a hard cap. The owner is G1DO. At the published prices, a million input tokens costs 0.15 US dollars off-peak and 0.30 at peak, and a million output tokens costs 0.60 off-peak and 1.20 at peak.

## Consequences

- `deepseek-flash` is an alias, not a dated snapshot. DeepSeek has already moved the 1017 update behind the same name. If the served snapshot changes, the post-cutoff set and the published results must be recomputed. The tool records the `model` field from each response so a change is visible. Replacing this with a dated snapshot needs a new decision record.
- DeepSeek does not offer a provider-side budget alert. The prepaid balance is the control; it blocks calls at zero. This does not meet the letter of #12's alert check, and is recorded here rather than claimed. A local low-balance check can be added with the spend-cap work.
- The key is only named in this change. #17 adds the code that reads it, along with the threat-model update.
- Prices stay out of this record. #17 keeps them in code and enforces the 1-dollar cap.
- Users need their own API key and pay their own bill. The project's 20-dollar budget covers only the published benchmark runs.

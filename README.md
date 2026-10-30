# SupportTriage — an enterprise support agent on OpenRouter

A prototype I built to understand OpenRouter from the support side: an agent that triages enterprise support cases, built directly on the OpenRouter API, with a strict **human-in-the-loop** safeguard on anything customer-facing or irreversible.

I built this to learn the product hands-on before interviewing — but the design maps to how real support actually works: diagnose fast, classify by where the fault lives, resolve what you can, escalate the rest with clean evidence, and never let automation do something irreversible without a human's sign-off.

## What it does (per case)
1. **Pulls a case from the queue** (mocked Zendesk — the connector is a thin swap for the live Zendesk API; I mocked the ticket source for the prototype).
2. **Classifies** the issue, routing the call to a fast, low-cost model via OpenRouter.
3. **Reproduces / isolates** the problem in a sandbox — read-only, allow-listed commands only.
4. **Analyzes root cause** with a stronger model, and decides **4xx (customer-side) vs 5xx (platform / upstream provider)** → recommends **resolve** or **escalate**.
5. **Proposes** a fix + customer reply, or a full **escalation doc** for engineering.

## The core design principle: human in the loop
The agent **never contacts a customer or takes an irreversible action on its own.** It proposes; the human approves. Self-assigning, sending a reply, and filing an escalation are all gated behind an explicit approval. The sandbox can only run read-only commands from a small allow-list (`curl`, `jq`, `cat`, `grep`, `dig`, …) — no writes, no mutations, nothing that can't be undone.

The reason is judgment, not caution for its own sake: a powerful agent has access to too much (internal logic, other customers' context) to safely draft a customer-facing reply unsupervised. So it does the mechanical 90% — gather, reproduce, classify, draft — and a human owns the last 10% that carries the risk and the relationship.

## Why it's built on OpenRouter
Each step routes to the right model by cost and quality:
- **Classification** → a cheap, fast model (high volume, simple task)
- **Root-cause analysis** → a stronger model (harder reasoning)

That's the exact cost/quality tradeoff OpenRouter exists to make easy — building on it taught me the routing model firsthand. (I also hit a real `402` mid-build when a model reserved a 16K-token output budget my key's credit cap couldn't afford up front — fixed by capping `max_tokens`, which is good cost hygiene anyway. Debugging that was a useful lesson in how the affordability checks actually behave.)

## Run it
```bash
# your key stays in the environment — never in the code
export OPENROUTER_API_KEY=sk-or-v1-your-key-here

python3 agent.py                      # work the whole queue (approves each gate)
python3 agent.py --ticket T-48213     # work one case
python3 agent.py --auto-approve       # demo mode: auto-approves gates (still shows them)
```

Optional — pick which models each step routes to (verify slugs at openrouter.ai/models):
```bash
export OR_MODEL_CLASSIFY=openai/gpt-4o-mini   # cheap/fast
export OR_MODEL_ANALYZE=openai/gpt-4o         # stronger
```

## Scope (honest)
This is a working prototype I built to understand the product and demonstrate how I'd use AI to make support faster — not a finished enterprise tool. The Zendesk layer is mocked; the **OpenRouter routing, the sandboxed reproduction, the 4xx/5xx classification, and the human-in-the-loop design are the real, working parts.**

## How I'd extend it in production
- Swap the mocked queue for the live **Zendesk API** (one function).
- Add the "**is this an OpenRouter issue or a provider issue?**" branch explicitly — isolate the layer, and if it's provider-side, check whether another provider supports the same parameters and suggest a fallback if quality holds.
- Richer per-customer repro environment (replay their actual failing request safely against a test key — never their credits or data).
- Feed recurring patterns back to Product as feature-gap signals, so a recurring issue gets productized once instead of re-triaged forever.
- Track cost per triage (already surfaces token usage) to keep the automation itself cheap.

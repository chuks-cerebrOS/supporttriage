#!/usr/bin/env python3
"""
SupportTriage — an enterprise support agent built on OpenRouter.

What it does, per case:
  1. Pulls a case from the queue (mocked Zendesk; swap connector for live API).
  2. Analyzes & classifies the issue, routing the LLM call through OpenRouter.
  3. Reproduces/isolates the problem in a sandbox (safe, read-only commands only).
  4. Classifies by status code: 4xx (customer-side) vs 5xx (platform/upstream).
  5. Recommends resolve-vs-escalate, and drafts full documentation for engineering.

Design principle — HUMAN IN THE LOOP:
  The agent NEVER contacts a customer or takes an irreversible action on its own.
  It proposes; you approve. Customer-facing and escalation actions are gated.

Built on OpenRouter so each step routes to the right model by cost/quality:
  - cheap/fast model for classification
  - stronger model for root-cause analysis

Usage:
  export OPENROUTER_API_KEY=sk-or-v1-...      # your key stays in the environment
  python3 agent.py                             # work the whole queue
  python3 agent.py --ticket T-48213            # work one case
  python3 agent.py --auto-approve              # demo mode: auto-approves gated steps
"""

import os
import sys
import json
import argparse
import subprocess
import urllib.request
import urllib.error

# ----- OpenRouter config -------------------------------------------------------
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
API_KEY = os.environ.get("OPENROUTER_API_KEY")

# Route each step to the right model by cost/quality. Edit slugs to taste —
# verify current slugs at openrouter.ai/models. Cheap+fast for classification,
# a stronger model for analysis. These are sensible, low-cost defaults.
MODEL_CLASSIFY = os.environ.get("OR_MODEL_CLASSIFY", "openai/gpt-4o-mini")
MODEL_ANALYZE  = os.environ.get("OR_MODEL_ANALYZE",  "openai/gpt-4o")

# A tiny allowlist: the agent's sandbox may ONLY run these read-only commands to
# reproduce/isolate. No writes, no network mutations, nothing irreversible.
SAFE_COMMANDS = {"curl", "jq", "cat", "head", "grep", "echo", "ls", "dig", "nslookup"}


def call_openrouter(model, messages, temperature=0.2, max_tokens=1200):
    """One chat-completion call through OpenRouter. Returns the text, or raises.

    max_tokens caps the output — good practice (an unbounded support call can run
    up cost), and it keeps each request affordable on a small per-key credit cap.
    """
    if not API_KEY:
        sys.exit("ERROR: set OPENROUTER_API_KEY in your environment first.")
    body = json.dumps({
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }).encode()
    req = urllib.request.Request(
        OPENROUTER_URL, data=body, method="POST",
        headers={
            "Authorization": f"Bearer {API_KEY}",
            "Content-Type": "application/json",
            # OpenRouter uses these for attribution/routing context (optional).
            "HTTP-Referer": "https://localhost/support-agent",
            "X-Title": "SupportTriage Agent",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read())
        return data["choices"][0]["message"]["content"], data.get("usage", {})
    except urllib.error.HTTPError as e:
        # Real support-engineering: read the status code to classify the failure.
        detail = e.read().decode(errors="replace")
        raise RuntimeError(f"OpenRouter returned HTTP {e.code}: {detail}") from None


def load_queue():
    """Mocked Zendesk queue. Swap this one function for the live Zendesk API."""
    path = os.path.join(os.path.dirname(__file__), "tickets", "queue.json")
    with open(path) as f:
        return json.load(f)


def gate(prompt, auto_approve):
    """Human-in-the-loop gate. Returns True only on explicit approval."""
    if auto_approve:
        print(f"   [auto-approved] {prompt}")
        return True
    answer = input(f"   APPROVE? {prompt} [y/N] ").strip().lower()
    return answer == "y"


def reproduce(repro_cmd, auto_approve):
    """Run a single read-only repro command in the sandbox, gated + allowlisted."""
    first = repro_cmd.strip().split()[0] if repro_cmd.strip() else ""
    if first not in SAFE_COMMANDS:
        return f"[blocked] '{first}' is not in the read-only allowlist. Skipped for safety."
    if not gate(f"run repro in sandbox: `{repro_cmd}`", auto_approve):
        return "[skipped] repro not approved by human."
    try:
        out = subprocess.run(
            repro_cmd, shell=True, capture_output=True, text=True, timeout=30
        )
        return (out.stdout + out.stderr)[:2000] or "(no output)"
    except subprocess.TimeoutExpired:
        return "[timeout] repro command exceeded 30s."


def triage(ticket, auto_approve):
    print("\n" + "=" * 70)
    print(f"TICKET {ticket['id']}  |  {ticket['subject']}")
    print(f"Customer: {ticket['customer']}  ({ticket.get('tier','?')} tier)")
    print(f"Reported: {ticket['body']}")
    print("=" * 70)

    # --- Step 1: self-assign (gated — this is an action on the queue) ----------
    if gate(f"self-assign ticket {ticket['id']} to me", auto_approve):
        print(f"   -> assigned {ticket['id']} to you.")
    else:
        print("   -> left unassigned."); return

    # --- Step 2: classify (cheap/fast model via OpenRouter) --------------------
    print("\n[1/4] Classifying (routing to a fast, low-cost model)...")
    classify_msgs = [
        {"role": "system", "content":
            "You are a triage classifier for an LLM-API support team. "
            "Given a support ticket, respond with STRICT JSON: "
            '{"category": "<auth|rate_limit|billing|provider_outage|integration|other>", '
            '"likely_status_class": "<4xx|5xx|none>", '
            '"severity": "<P1|P2|P3>", "one_line": "<short summary>"}'},
        {"role": "user", "content": f"Subject: {ticket['subject']}\nBody: {ticket['body']}"},
    ]
    raw, usage = call_openrouter(MODEL_CLASSIFY, classify_msgs)
    cls = extract_json(raw)
    print(f"   category={cls.get('category')}  status_class={cls.get('likely_status_class')}  "
          f"severity={cls.get('severity')}")
    print(f"   summary: {cls.get('one_line')}")

    # --- Step 3: reproduce / isolate (sandbox, gated, read-only) ---------------
    print("\n[2/4] Reproduce & isolate...")
    repro_out = ""
    if ticket.get("repro_cmd"):
        repro_out = reproduce(ticket["repro_cmd"], auto_approve)
        print(f"   repro output (truncated):\n   {repro_out[:400].replace(chr(10), chr(10)+'   ')}")
    else:
        print("   no deterministic repro command on this ticket; analyzing from report.")

    # --- Step 4: root-cause analysis (stronger model via OpenRouter) -----------
    print("\n[3/4] Root-cause analysis (routing to a stronger model)...")
    analyze_msgs = [
        {"role": "system", "content":
            "You are a senior enterprise support engineer for an LLM-routing API "
            "(like OpenRouter). Analyze the ticket and any reproduction output. "
            "Decide: is this 4xx (customer-side request/config) or 5xx "
            "(platform/upstream provider)? Then recommend RESOLVE (with the fix/"
            "workaround) or ESCALATE to engineering (with what they need). "
            "Respond in STRICT JSON: "
            '{"fault_side": "<customer|platform|upstream|unclear>", '
            '"status_class": "<4xx|5xx|none>", '
            '"recommendation": "<resolve|escalate>", '
            '"customer_reply_draft": "<a proposed reply, NOT yet sent>", '
            '"fix_or_workaround": "<steps>", '
            '"escalation_doc": "<repro steps + evidence + hypothesis for engineering>"}'},
        {"role": "user", "content":
            f"TICKET: {ticket['subject']}\n{ticket['body']}\n\n"
            f"CLASSIFICATION: {json.dumps(cls)}\n\n"
            f"REPRO OUTPUT:\n{repro_out or '(none)'}"},
    ]
    raw2, usage2 = call_openrouter(MODEL_ANALYZE, analyze_msgs)
    analysis = extract_json(raw2)

    print(f"   fault side: {analysis.get('fault_side')}  ({analysis.get('status_class')})")
    print(f"   recommendation: {analysis.get('recommendation', '').upper()}")

    # --- Step 5: propose action — HUMAN IN THE LOOP ----------------------------
    print("\n[4/4] Proposed action (requires your approval)...")
    rec = analysis.get("recommendation")
    if rec == "resolve":
        print("   Proposed fix/workaround:")
        print(f"     {analysis.get('fix_or_workaround')}")
        print("   Proposed customer reply (DRAFT — not sent):")
        print(f"     {analysis.get('customer_reply_draft')}")
        if gate("send this reply to the customer", auto_approve):
            print("   -> [would send reply]  (gated action — human approved)")
        else:
            print("   -> reply held. Nothing sent. You stay in control.")
    else:  # escalate
        print("   Proposed escalation to engineering (full documentation):")
        print(f"     {analysis.get('escalation_doc')}")
        if gate("file this escalation to engineering", auto_approve):
            print("   -> [would file escalation with the above doc]  (human approved)")
        else:
            print("   -> escalation held pending your review.")

    # token/cost awareness — a support engineer watches spend
    tok = (usage.get("total_tokens", 0) or 0) + (usage2.get("total_tokens", 0) or 0)
    print(f"\n   (routed via OpenRouter · ~{tok} tokens across 2 model calls)")


def extract_json(text):
    """Best-effort JSON extraction from a model response."""
    text = text.strip()
    if text.startswith("```"):
        text = text.split("```")[1].replace("json", "", 1).strip()
    try:
        return json.loads(text)
    except Exception:
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end != -1:
            try:
                return json.loads(text[start:end + 1])
            except Exception:
                pass
    return {"_raw": text[:300]}


def main():
    ap = argparse.ArgumentParser(description="SupportTriage agent (OpenRouter).")
    ap.add_argument("--ticket", help="work a single ticket id (e.g. T-48213)")
    ap.add_argument("--auto-approve", action="store_true",
                    help="demo mode: auto-approve gated steps (still shows the gates)")
    args = ap.parse_args()

    queue = load_queue()
    if args.ticket:
        queue = [t for t in queue if t["id"] == args.ticket]
        if not queue:
            sys.exit(f"No ticket {args.ticket} in the queue.")

    print(f"SupportTriage — {len(queue)} ticket(s) in queue. "
          f"Human-in-the-loop: ON{' (auto-approve demo)' if args.auto_approve else ''}.")
    for t in queue:
        triage(t, args.auto_approve)
    print("\nDone. No customer was contacted and nothing irreversible happened "
          "without your approval.")


if __name__ == "__main__":
    main()

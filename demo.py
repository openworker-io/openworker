#!/usr/bin/env python3
"""
OpenWorker demo — a worker receives a task and runs it end-to-end through
all five harness layers.

Usage:
    python demo.py "Summarise the AI employee market in 2026"
    python demo.py --worker workers/worker.maya.yaml "Draft a LinkedIn post about AI governance"

What happens:
    1. Loads worker spec
    2. Validates input (injection check)
    3. Runs agent loop with whatever tools the worker is allowed
    4. If a tool needs approval: prints approval request to terminal,
       or sends to Slack if SLACK_BOT_TOKEN / SLACK_WEBHOOK_URL is set
    5. Writes audit log to ./audit.jsonl
    6. Prints output + cost summary
"""

import argparse
import asyncio

from runtime.task_runner import TaskRunner


async def main() -> None:
    parser = argparse.ArgumentParser(description="OpenWorker demo")
    parser.add_argument("task", help="Task for the worker to complete")
    parser.add_argument(
        "--worker",
        default="workers/worker.aryan.yaml",
        help="Path to worker spec YAML",
    )
    args = parser.parse_args()

    print("\n🤖 OpenWorker Demo")
    print(f"Worker : {args.worker}")
    print(f"Task   : {args.task}")
    print("─" * 50)

    runner = TaskRunner(args.worker)
    result = await runner.run(args.task)

    if result.success:
        print("\n✅ Task completed")
        print(f"Output preview:\n{(result.output or '')[:500]}")
    elif result.approval_id:
        print("\n⏳ Task paused — waiting for approval")
        print(f"Approval ID: {result.approval_id}")
    else:
        print(f"\n❌ Task failed: {result.error}")

    print(f"\n💰 Cost: ${result.cost_record.cost_usd:.6f}")
    print(f"📋 Audit: {len(result.audit_ids)} entries written to audit.jsonl")


if __name__ == "__main__":
    asyncio.run(main())

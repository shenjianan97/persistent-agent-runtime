# Vision

## Overview

Most AI agent frameworks treat execution as ephemeral—running in-process with state living in memory. A crash means starting over. This works for demos but fails for production workloads where agents run for hours, coordinate across steps, and cost real money per LLM call.

This project delivers a **cloud-native, serverless durable execution runtime designed specifically for AI agents**. It separates agent identity (state) from execution (compute), enabling developers to submit long-running tasks without provisioning or managing the underlying worker infrastructure. It solves three critical problems:

1. **Non-deterministic execution:** LLM calls return different results each time, necessitating checkpoint-resume rather than deterministic replay.
2. **Unbounded memory bloat:** Agent memory grows with every interaction, requiring distilled long-term memory with compaction.
3. **Cost runaway:** Per-token pricing demands cost-aware scheduling and strict budget enforcement.

---

## How This Differs From Existing Systems

| Feature | Temporal | LangGraph Platform | Restate | Azure Durable Functions | This Project |
|---------|----------|--------------------|---------|-------------------------|--------------|
| Execution model | Event-sourced deterministic replay | Graph execution with checkpointing | Journal-based replay | Checkpoint-resume with orchestrator constraints | **LangGraph graphs + durable lease-based execution (database-as-queue, distributed reaper, crash recovery)** |
| Memory model | Bounded workflow state | Conversation history | Key-value per virtual object | Orchestrator state (serializable) | **LangGraph state checkpoints (per-task) + append-only long-term memory with compaction (Phase 2)** |
| Cost awareness | None | None | None | Consumption-based billing (infra only) | **Per-node cost tracking (Phase 1) + budget enforcement (Phase 2)** |
| Infrastructure model | Self-hosted or Cloud | Managed platform (opinionated) | Self-hosted or Cloud | Azure-only managed | **Self-hosted, cloud-agnostic runtime you own — uses LangGraph for agent logic, owns the infra layer (queuing, leases, retries, dead letter, cost tracking)** |

**Why not just use Temporal?**
Temporal requires deterministic orchestration logic. AI agents violate this because the LLM inherently decides the next step and its outputs vary. Temporal's workflow state is also bounded, while agent memory grows unboundedly. Finally, Temporal has no built-in cost model. At scale, where LLM calls can cost $0.10+ each, cost-aware scheduling is mandatory, not optional.

# LangGraph Proof of Concept (POC) Plan

## Objective
The goal of this POC is to validate the core assumptions made in the Phase 1 Design Document (`docs/design-docs/phase-1/design.md`) specifically regarding **LangGraph Checkpoint behavior** and its integration with a custom `BaseCheckpointSaver`. This POC ensures our technical foundation is solid before building the full Durable Execution machinery.

## Core Assumptions to Validate 
Based on the Phase 1 design, we are assuming the following behaviors which MUST be true for our architecture to succeed:

1. **Checkpointer `put()` Overrides:** We assume we can inject our own logic inside the checkpointer's `put()` method to execute a DB lookup (validating `lease_owner`) and throw an exception to halt the graph execution mid-flight if the lease is lost *without* corrupting the engine.
2. **`astream()` Exception Bubbling:** We assume that if our custom checkpointer throws an exception during a save operation, the `astream()` generator will catch/bubble this exception cleanly to the caller without leaving phantom background threads.
3. **Resiliency to `max_steps`:** We assume LangGraph throws a predictable `GraphRecursionError` (or similar) when internal cycles exceed `max_steps`, and that this does not corrupt the last saved checkpoint state.
4. **State Serialization:** We assume LangGraph state objects (including tool messages) can successfully serialize via `pickle` or `json` out-of-the-box into a PostgreSQL JSONB/Bytea compatible format.

## Scope of the POC
This POC will **NOT** use PostgreSQL, API servers, or MCP tools. It will run entirely natively in Python to isolate LangGraph behavior. 

1. Create a dummy LangGraph workflow (e.g., a simple loop graph that counts from 1 to N using an LLM mock or dummy node).
2. Create a `MockDurableCheckpointer` extending `BaseCheckpointSaver`.
3. Simulate a "Lease Revocation" by having the `put()` method throw a custom `LeaseRevokedException` after the 2nd iteration.
4. Catch the exception from the `astream()` caller.
5. Re-instantiate the graph with the same `thread_id` but a fresh "valid lease" checkpointer and verify it resumes exactly from step 2.

## Output Structure

The POC will be located at `experiments/langgraph/` and needs to contain:
1. `requirements.txt` (langgraph, langchain-core, etc.)
2. `poc.py` (The main execution script containing the graph, checkpointer, and test runner)
3. `README.md` (Instructions on how to run it and a summary of findings)

## Agent Task Execution

We have defined a single agent task to implement this POC.
- **[Task: Implement LangGraph POC](./agent_task.md)**

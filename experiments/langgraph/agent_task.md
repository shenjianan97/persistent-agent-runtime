<!-- AGENT_TASK_START: poc-langgraph-validation.md -->

# Task: Implement LangGraph Proof of Concept

## Agent Instructions
You are a software engineer building a Proof of Concept (POC) to validate core architectural assumptions.
Your scope is strictly limited to this task. 

**CRITICAL PRE-WORK:** Before beginning implementation, you MUST read the following context files to understand the system architecture and constraints:
1. `docs/PROJECT.md` 
2. `docs/design/PHASE1_DURABLE_EXECUTION.md`
3. `experiments/langgraph/plan.md`

## Context
We need to validate that LangGraph's `BaseCheckpointSaver` can be overridden to throw exceptions during `put()` operations (simulating a lost lease) and that the graph can cleanly resume from the last successful checkpoint without state corruption. We are mocking the DB using an in-memory dictionary inside the custom checkpointer for this POC.

## Affected Component
- **Service/Module:** LangGraph POC
- **File paths (if known):** `experiments/langgraph/poc.py`
- **Change type:** new code

## Implementation Specification
Step 1: Create a `requirements.txt` with `langgraph` and `langchain-core` (and any other necessary standard libraries).
Step 2: Create `poc.py`. Inside, define a custom `MockLeaseCheckpointer(BaseCheckpointSaver)` that wraps an in-memory dictionary.
Step 3: Add logic to `MockLeaseCheckpointer.put()` that accepts a `should_revoke_lease` flag. If true, throw a custom `LeaseRevokedError()`. Ensure it only throws *after* 2 successful saves to prove resumption.
Step 4: Create a simple dummy `StateGraph` (e.g., a counter) that iterates multiple times.
Step 5: Write a runner function that calls `astream()` or `stream()`. Catch the `LeaseRevokedError`. 
Step 6: Write a secondary runner function that resumes the exact same `thread_id` using a new checkpointer (where `should_revoke_lease=False`) and assert via print statements that the state continued from the correct integer.

## Acceptance Criteria
The implementation is complete when:
- [ ] `poc.py` executes successfully.
- [ ] The terminal output clearly demonstrates the graph pausing due to an exception in the checkpointer.
- [ ] The terminal output clearly demonstrates the graph resuming from the exact halted state on a second run.
- [ ] You output your findings in a `README.md` summarizing if our Phase 1 Design assumptions hold true.

## Testing Requirements
- **Unit tests:** The script itself acts as the test (it should print PASS/FAIL criteria).

## Constraints and Guardrails
- **Do NOT** use a real database (SQLite, Postgres). Keep it pure Python memory to isolate LangGraph behavior.
- **Do NOT** use a real LLM. Use dummy functions returning static state updates.

## Assumptions / Open Questions for This Task
- None

<!-- AGENT_TASK_END: poc-langgraph-validation.md -->

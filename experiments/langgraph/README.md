# LangGraph Checkpoint POC

This POC validates core assumptions from Phase 1 durable execution design using only Python memory state.

## Files
- `requirements.txt`: Python dependencies for the POC.
- `poc.py`: Runnable script with 3 validations.
- `poc_with_real_llm.py`: Same lease-revocation/resume pattern but with a real OpenAI call in each graph step.
- `.env.example`: Environment variable template for the real-LLM run.

## What It Validates
1. **Checkpointer `put()` override + lease revocation:**
   - `MockLeaseCheckpointer.put()` throws `LeaseRevokedError` after 2 successful checkpoint saves.
2. **`astream()` exception bubbling:**
   - The caller catches `LeaseRevokedError` directly from `graph.astream(...)`.
3. **Resume from last checkpoint with same `thread_id`:**
   - A second graph/checkpointer instance resumes from the last successful checkpoint and finishes.
4. **`max_steps` / recursion behavior:**
   - Infinite loop graph with low `recursion_limit` raises `GraphRecursionError`.
5. **Serialization sanity:**
   - State-like object round-trips via JSON and pickle.

## Run
```bash
cd experiments/langgraph
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python poc.py
```

## Run With Real LLM
```bash
cd experiments/langgraph
cp .env.example .env
# edit .env and set OPENAI_API_KEY
python poc_with_real_llm.py
```

## Expected Output Signals
- `[EXPECTED] Caught LeaseRevokedError from astream()`
- `[RESUME START] count=2 (expected 2)`
- `[FINAL RESUMED STATE] count=5 (expected 5)`
- `[EXPECTED] Caught GraphRecursionError`
- `OVERALL RESULT: PASS`

## Findings Template
After execution, record:
- `put()` override behavior: PASS/FAIL
- `astream()` exception propagation: PASS/FAIL
- Resume correctness from same `thread_id`: PASS/FAIL
- `recursion_limit` safety: PASS/FAIL
- Serialization compatibility: PASS/FAIL

If any are FAIL, include exact stack trace and runtime versions (`python --version`, `pip freeze | rg langgraph`).

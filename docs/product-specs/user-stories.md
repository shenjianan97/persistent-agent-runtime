# User Stories

## As an AI application developer:
- I want to submit a long-running multi-step task and have it execute reliably without babysitting.
- I want my agent's progress to survive worker crashes and seamlessly resume from the last checkpoint.
- I want full visibility into execution history—every step, input, output, latency, and cost.
- I want to set strict task budgets so a runaway agent doesn't drain LLM credits.

## As a platform operator (Agent-as-a-Service provider):
- I want to offer a serverless "Agent-as-a-Service" where customers submit tasks without managing underlying compute or worker infrastructure.
- I want to run multiple agents concurrently with fair resource sharing and horizontal scaling.
- I want dead-lettered tasks to be visible, investigateable, and re-drivable on behalf of customers.
- I want alerts for stuck tasks, excessive retries, or budget breaches to protect my margins.

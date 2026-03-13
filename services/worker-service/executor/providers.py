import asyncpg
from typing import Optional
from langchain.chat_models import init_chat_model
from langchain_core.language_models import BaseChatModel

async def create_llm(
    pool: asyncpg.Pool, 
    provider: str, 
    model_name: str, 
    temperature: float
) -> BaseChatModel:
    """Fetch API key from the database and initialize the LangChain chat model."""
    async with pool.acquire() as conn:
        api_key = await conn.fetchval(
            "SELECT api_key FROM provider_keys WHERE provider_id = $1",
            provider
        )
    
    if not api_key:
        raise ValueError(f"No API key found in database for provider: {provider}")

    return init_chat_model(
        model=model_name,
        model_provider=provider,
        temperature=temperature,
        api_key=api_key
    )

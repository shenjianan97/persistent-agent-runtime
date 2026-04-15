import importlib.util
from pathlib import Path
import sys
import types


def load_discover_models_module():
    module_path = Path(__file__).resolve().parents[1] / "services" / "model-discovery" / "main.py"
    spec = importlib.util.spec_from_file_location("discover_models", module_path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules.setdefault("psycopg", types.SimpleNamespace(connect=None))
    spec.loader.exec_module(module)
    return module


def test_resolve_model_pricing_returns_explicit_price_for_known_model():
    discover_models = load_discover_models_module()

    pricing = discover_models.resolve_model_pricing("openai", "gpt-4o")

    assert pricing == {"input": 2_500_000, "output": 10_000_000}


def test_resolve_model_pricing_uses_provider_fallback_for_unknown_model():
    discover_models = load_discover_models_module()

    pricing = discover_models.resolve_model_pricing("openai", "gpt-unknown-next")

    assert pricing == discover_models.PRICING_FALLBACKS["openai"]


def test_resolve_model_pricing_uses_provider_fallback_for_bedrock():
    discover_models = load_discover_models_module()

    pricing = discover_models.resolve_model_pricing("bedrock", "future-model")

    assert pricing == discover_models.PRICING_FALLBACKS["bedrock"]


def test_resolve_model_pricing_uses_global_fallback_for_unknown_provider():
    discover_models = load_discover_models_module()

    pricing = discover_models.resolve_model_pricing("some-unknown-provider", "some-model")

    assert pricing == discover_models.GLOBAL_FALLBACK_PRICING

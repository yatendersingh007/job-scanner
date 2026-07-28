"""Adapter registry — map a provider name (from config) to its adapter class."""
from scanner.connectors.adapters.jsearch import JSearchAdapter
from scanner.connectors.adapters.predictleads import PredictLeadsAdapter
from scanner.connectors.adapters.theirstack import TheirStackAdapter

REGISTRY = {
    "jsearch": JSearchAdapter,
    "theirstack": TheirStackAdapter,
    "predictleads": PredictLeadsAdapter,
}


def get_adapter(provider: str, api_key):
    provider = (provider or "").lower()
    cls = REGISTRY.get(provider)
    if cls is None:
        raise ValueError(
            f"Unknown aggregator provider '{provider}'. "
            f"Available: {', '.join(REGISTRY)}"
        )
    return cls(api_key)

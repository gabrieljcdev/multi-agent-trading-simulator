"""
data_sources/sources/__init__.py

Registry of data sources the aggregator polls by default.

═══════════════════════════════════════════════════════════════════════
To add a new data source
═══════════════════════════════════════════════════════════════════════

1. Create data_sources/sources/my_source.py
2. Subclass BaseDataSource (from data_sources.base)
3. Set the class attrs:
       source_id        = "my_source"
       display_name     = "My Source"
       refresh_interval = 600              # seconds
       optional         = True             # False = core
       requires_api_key = False
       api_key_env_var  = ""                # only if requires_api_key
4. Implement:
       async def fetch_all(self) -> list[DataPoint]: ...
       def list_metrics(self) -> list[str]: ...
       def is_available(self) -> bool:       # override only if non-default
5. Append an instance to REGISTERED_SOURCES below

Nothing else needs to change. The aggregator picks it up automatically,
the dashboard reads from it via data_sources.<source_id>.cached_value(),
DB logging happens via the base class on every refresh, and pub/sub
works through the universal _notify_callback hook.

Sources never have to manage caching, retries, error logging, or
notification — BaseDataSource.get() / refresh_if_stale() / _do_fetch()
handle all of that. fetch_all() should just build DataPoints; on failure
return DataPoints with .error set, or an empty list.
"""

from data_sources.sources.coinglass     import CoinglassSource
from data_sources.sources.fred          import FREDSource
from data_sources.sources.alpha_vantage import AlphaVantageSource
from data_sources.sources.frankfurter   import FrankfurterSource


# Ordered registry — the aggregator iterates this list to expose each
# instance as data_sources.<source_id>. Append new instances here.
REGISTERED_SOURCES: list = [
    CoinglassSource(),
    FREDSource(),
    AlphaVantageSource(),
    FrankfurterSource(),
]

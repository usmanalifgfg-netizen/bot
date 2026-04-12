"""
Polymarket API client — fetches live markets and prices.
Docs: https://docs.polymarket.com
"""
import time
import logging
import requests
from typing import List, Dict, Optional

logger = logging.getLogger(__name__)

GAMMA_BASE  = "https://gamma-api.polymarket.com"   # market metadata
CLOB_BASE   = "https://clob.polymarket.com"         # live order book


class PolymarketClient:
    def __init__(self, api_key: Optional[str] = None):
        self.session = requests.Session()
        if api_key:
            self.session.headers.update({"Authorization": f"Bearer {api_key}"})

    # ── Market Discovery ─────────────────────────────────────────────────────
    def get_active_markets(self, limit: int = 100) -> List[Dict]:
        """Fetch open markets sorted by volume."""
        try:
            resp = self.session.get(
                f"{GAMMA_BASE}/markets",
                params={"active": "true", "closed": "false", "limit": limit,
                        "_order": "volume24hr", "_sort": "desc"},
                timeout=10,
            )
            resp.raise_for_status()
            return resp.json().get("markets", resp.json())
        except Exception as e:
            logger.error(f"get_active_markets failed: {e}")
            return []

    # ── Price Feed ───────────────────────────────────────────────────────────
    def get_market_price(self, condition_id: str) -> Optional[float]:
        """Return the YES ask price (0-1) for a market."""
        try:
            resp = self.session.get(
                f"{CLOB_BASE}/midpoint",
                params={"token_id": condition_id},
                timeout=5,
            )
            resp.raise_for_status()
            data = resp.json()
            price = float(data.get("mid", 0))
            return price
        except Exception as e:
            logger.error(f"get_market_price failed for {condition_id}: {e}")
            return None

    # ── Order Book ───────────────────────────────────────────────────────────
    def get_orderbook(self, token_id: str) -> Optional[Dict]:
        try:
            resp = self.session.get(
                f"{CLOB_BASE}/book",
                params={"token_id": token_id},
                timeout=5,
            )
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            logger.error(f"get_orderbook failed: {e}")
            return None

    # ── Place Order (live trading — use with caution) ─────────────────────
    def place_order(self, token_id: str, side: str, size: float,
                    price: float, api_key: str, private_key: str) -> Optional[Dict]:
        """
        side: "BUY" or "SELL"
        Requires CLOB API key + wallet private key.
        See: https://docs.polymarket.com/#place-order
        """
        # This is intentionally left as a stub — real signing requires
        # py-clob-client (pip install py-clob-client)
        raise NotImplementedError(
            "Install py-clob-client and implement signed order submission. "
            "See scripts/live_trading.py for the wrapper."
        )

    # ── Utility ──────────────────────────────────────────────────────────────
    def batch_prices(self, condition_ids: List[str], delay: float = 0.1) -> Dict[str, float]:
        prices = {}
        for cid in condition_ids:
            p = self.get_market_price(cid)
            if p is not None:
                prices[cid] = p
            time.sleep(delay)
        return prices

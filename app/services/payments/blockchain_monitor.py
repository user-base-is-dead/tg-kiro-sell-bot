from __future__ import annotations

import httpx
from app.core.config import get_settings

# USDT on BNB Smart Chain (BEP20) - 18 decimals
USDT_BEP20_CONTRACT = "0x55d398326f99059fF775485246999027B3197955"
MATCH_TOLERANCE = 0.004  # Sub-cent float tolerance
SERVICE_FEE = 0.2  # USD service fee per transaction


class BlockchainMonitor:
    """Monitor BSC blockchain for incoming USDT transfers."""

    def __init__(self):
        settings = get_settings()
        self.wallet_address = settings.wallet_address
        self.api_key = settings.bscscan_api_key
        self.bscscan_url = "https://api.bscscan.com/api"
        self.tolerance = MATCH_TOLERANCE

    async def fetch_recent_transfers(self, limit: int = 25) -> list[dict]:
        """Fetch recent USDT BEP20 transfers to wallet address."""
        params = {
            "module": "account",
            "action": "tokentx",
            "contractaddress": USDT_BEP20_CONTRACT,
            "address": self.wallet_address,
            "sort": "desc",
            "page": 1,
            "offset": limit,
            "apikey": self.api_key,
        }
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.get(self.bscscan_url, params=params)
                resp.raise_for_status()
                data = resp.json()
        except Exception as e:
            raise RuntimeError(f"BSCscan API error: {e}")

        if data.get("status") != "1" or not isinstance(data.get("result"), list):
            return []

        transfers = []
        for tx in data["result"]:
            if tx.get("to", "").lower() != self.wallet_address.lower():
                continue
            decimals = int(tx.get("tokenDecimal", 18))
            value = int(tx["value"]) / (10 ** decimals)
            transfers.append(
                {
                    "hash": tx["hash"],
                    "value": value,
                    "from": tx["from"],
                    "timestamp": int(tx.get("timeStamp", 0)),
                }
            )
        return transfers

    def matches_amount(self, transfer_amount: float, expected_amount: float) -> bool:
        """Check if transfer amount matches expected amount within tolerance."""
        return abs(transfer_amount - expected_amount) <= self.tolerance

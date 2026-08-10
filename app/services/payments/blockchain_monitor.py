from __future__ import annotations

import httpx

from app.core.config import get_settings

# USDT on BNB Smart Chain (BEP20) - 18 decimals
USDT_BEP20_CONTRACT = "0x55d398326f99059fF775485246999027B3197955"
MATCH_TOLERANCE = 0.004  # Sub-cent float tolerance

# keccak256("Transfer(address,address,uint256)")
TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"

# Invoices live 15 minutes. BSC blocks land well under a second, so ~1500 blocks covers that
# window with room to spare for a slow tick or a brief RPC outage.
LOOKBACK_BLOCKS = 1500
# Providers cap how wide a single `eth_getLogs` range may be and answer "limit exceeded" rather
# than truncating, so the window is walked in chunks. The cap varies wildly between endpoints
# (keyless ones go as low as 25 blocks), hence `BSC_RPC_LOG_SPAN`; this is only the fallback.
MAX_BLOCK_SPAN = 500


class BlockchainMonitor:
    """Monitor BSC for incoming USDT transfers, straight off a JSON-RPC node.

    This used to read BSCscan's `tokentx` endpoint. That API was retired: the V1 host answers
    every request with "deprecated endpoint", and the V2 replacement refuses BNB Chain on the free
    plan. Both failures arrive as a normal HTTP 200 body, so the checker job saw no transfers and
    every payment silently expired. Reading `Transfer` logs from a node removes the indexer from
    the path entirely — it is the same data the explorer itself is built on.
    """

    def __init__(self) -> None:
        settings = get_settings()
        self.wallet_address = settings.wallet_address
        self.rpc_url = settings.bsc_rpc_url
        self.log_span = settings.bsc_rpc_log_span
        self.tolerance = MATCH_TOLERANCE

    async def _call(self, method: str, params: list):
        """One JSON-RPC round trip. A JSON-RPC error is returned with HTTP 200, so the body has to
        be inspected — raising here is what lets the caller log a real failure instead of
        reporting an empty chain."""
        payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                resp = await client.post(self.rpc_url, json=payload)
                resp.raise_for_status()
                data = resp.json()
        except Exception as e:
            raise RuntimeError(f"BSC RPC error: {e}") from e

        if "error" in data:
            raise RuntimeError(f"BSC RPC error: {data['error']}")
        return data["result"]

    def _topic_address(self, address: str) -> str:
        return "0x" + address[2:].lower().rjust(64, "0")

    async def fetch_recent_transfers(self, limit: int = 25) -> list[dict]:
        """Recent USDT transfers *into* the shop wallet, newest first.

        Returns the same records the checker job has always consumed —
        `{hash, value, from, timestamp}` — so nothing downstream had to change.
        """
        latest = int(await self._call("eth_blockNumber", []), 16)
        start = max(0, latest - LOOKBACK_BLOCKS)

        logs: list[dict] = []
        lo = start
        while lo <= latest:
            hi = min(lo + self.log_span, latest)
            chunk = await self._call(
                "eth_getLogs",
                [
                    {
                        "fromBlock": hex(lo),
                        "toBlock": hex(hi),
                        "address": USDT_BEP20_CONTRACT,
                        # topics[2] is the indexed `to` — filtering node-side means the node never
                        # ships us the rest of the chain's USDT traffic.
                        "topics": [TRANSFER_TOPIC, None, self._topic_address(self.wallet_address)],
                    }
                ],
            )
            logs.extend(chunk)
            lo = hi + 1

        logs = logs[-limit:]

        block_times: dict[int, int] = {}
        transfers = []
        for log in logs:
            block_number = int(log["blockNumber"], 16)
            if block_number not in block_times:
                block = await self._call("eth_getBlockByNumber", [hex(block_number), False])
                block_times[block_number] = int(block["timestamp"], 16)

            # The amount is the unindexed value in `data`; USDT-BEP20 is fixed at 18 decimals.
            value = int(log["data"], 16) / (10**18)
            transfers.append(
                {
                    "hash": log["transactionHash"],
                    "value": value,
                    "from": "0x" + log["topics"][1][-40:],
                    "timestamp": block_times[block_number],
                }
            )

        transfers.reverse()  # newest first, matching the old `sort=desc` contract
        return transfers

    def matches_amount(self, transfer_amount: float, expected_amount: float) -> bool:
        """Check if transfer amount matches expected amount within tolerance."""
        return abs(transfer_amount - expected_amount) <= self.tolerance

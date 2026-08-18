from __future__ import annotations

import asyncio

import httpx

from app.core.config import get_settings

# USDT on BNB Smart Chain (BEP20) - 18 decimals
USDT_BEP20_CONTRACT = "0x55d398326f99059fF775485246999027B3197955"
# How far off the invoiced total a transfer may land and still count as payment for it.
#
# Wallets do not send what the buyer typed. Trust Wallet and friends round the USDT amount to their
# own display precision, some deduct a spread on a swap-and-send, and a buyer paying $5.20 by hand
# from a balance of $5.19 sends $5.19. Every one of those is somebody who paid and then sat looking
# at an unconfirmed invoice. Two cents either way covers all of it and costs at most two cents.
#
# The price of a wide window is that neighbouring invoices can both look like a match, and the
# checker will not guess between them — so `_unique_total` in topup_crypto.py must keep live
# invoices further apart than this. Widen one and the other has to move with it.
MATCH_TOLERANCE = 0.02

# The smallest gap allowed between two live invoice totals: one cent wider than the window on both
# sides, so no single transfer can ever sit inside two invoices' windows at once.
MIN_AMOUNT_GAP = round(MATCH_TOLERANCE * 2 + 0.01, 2)

# keccak256("Transfer(address,address,uint256)")
TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"

# Invoices live 15 minutes. BSC blocks land ~every 3 seconds, so 300 blocks covers that window.
LOOKBACK_BLOCKS = 300
RPC_DELAY = 0.35  # seconds between RPC calls to stay under free-tier rate limits


class BlockchainMonitor:
    """Monitor BSC for incoming USDT transfers, straight off a JSON-RPC node."""

    def __init__(self) -> None:
        settings = get_settings()
        self.wallet_address = settings.wallet_address
        self.rpc_url = settings.bsc_rpc_url
        self.log_span = settings.bsc_rpc_log_span
        self.tolerance = MATCH_TOLERANCE

    async def _call(self, client: httpx.AsyncClient, method: str, params: list):
        payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
        resp = await client.post(self.rpc_url, json=payload)
        resp.raise_for_status()
        data = resp.json()
        if "error" in data:
            raise RuntimeError(f"BSC RPC error: {data['error']}")
        return data["result"]

    def _topic_address(self, address: str) -> str:
        return "0x" + address[2:].lower().rjust(64, "0")

    async def fetch_recent_transfers(self, limit: int = 25) -> list[dict]:
        """Recent USDT transfers *into* the shop wallet, newest first."""
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                latest = int(await self._call(client, "eth_blockNumber", []), 16)
                start = max(0, latest - LOOKBACK_BLOCKS)

                logs: list[dict] = []
                lo = start
                while lo <= latest:
                    hi = min(lo + self.log_span, latest)
                    await asyncio.sleep(RPC_DELAY)
                    chunk = await self._call(
                        client,
                        "eth_getLogs",
                        [
                            {
                                "fromBlock": hex(lo),
                                "toBlock": hex(hi),
                                "address": USDT_BEP20_CONTRACT,
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
                        await asyncio.sleep(RPC_DELAY)
                        block = await self._call(client, "eth_getBlockByNumber", [hex(block_number), False])
                        block_times[block_number] = int(block["timestamp"], 16)

                    value = int(log["data"], 16) / (10**18)
                    transfers.append(
                        {
                            "hash": log["transactionHash"],
                            "value": value,
                            "from": "0x" + log["topics"][1][-40:],
                            "timestamp": block_times[block_number],
                        }
                    )

                transfers.reverse()
                return transfers
        except RuntimeError:
            raise
        except Exception as e:
            raise RuntimeError(f"BSC RPC error: {e}") from e

    def matches_amount(self, transfer_amount: float, expected_amount: float) -> bool:
        """Whether this transfer pays this invoice, judged in whole cents.

        The comparison is rounded to cents before it is made because the naive float form quietly
        excludes the exact edge: 5.20 - 5.18 comes out as 0.02000000000000046, which is larger than
        a tolerance of 0.02, so the buyer who paid two cents light — the case this window exists
        for — would be refused by a rounding artefact. Sub-cent dust on the transfer itself is
        rounded away with it, which is the intent: nobody invoices fractions of a cent.
        """
        return round(abs(transfer_amount - expected_amount), 2) <= self.tolerance + 1e-9

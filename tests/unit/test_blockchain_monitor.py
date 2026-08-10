from __future__ import annotations

import pytest

from app.services.payments.blockchain_monitor import (
    TRANSFER_TOPIC,
    USDT_BEP20_CONTRACT,
    BlockchainMonitor,
)

WALLET = "0x517f918B14df080d3f7f5244AC61f5B1Cd122f21"


def _topic_addr(addr: str) -> str:
    return "0x" + addr[2:].lower().rjust(64, "0")


def _log(*, value_wei: int, sender: str, block: int, tx: str) -> dict:
    return {
        "address": USDT_BEP20_CONTRACT.lower(),
        "topics": [TRANSFER_TOPIC, _topic_addr(sender), _topic_addr(WALLET)],
        "data": hex(value_wei),
        "blockNumber": hex(block),
        "transactionHash": tx,
    }


class FakeRPC:
    """Stands in for the JSON-RPC endpoint. Records every request so the tests can assert on the
    filter that was actually sent, not just on the parsed output."""

    def __init__(self, *, latest: int, logs: list[dict], block_times: dict[int, int]) -> None:
        self.latest = latest
        self.logs = logs
        self.block_times = block_times
        self.calls: list[tuple[str, list]] = []

    async def __call__(self, method: str, params: list):
        self.calls.append((method, params))
        if method == "eth_blockNumber":
            return hex(self.latest)
        if method == "eth_getLogs":
            lo = int(params[0]["fromBlock"], 16)
            hi = int(params[0]["toBlock"], 16)
            return [log for log in self.logs if lo <= int(log["blockNumber"], 16) <= hi]
        if method == "eth_getBlockByNumber":
            n = int(params[0], 16)
            return {"timestamp": hex(self.block_times[n])}
        raise AssertionError(f"unexpected RPC method {method}")


@pytest.fixture
def monitor(monkeypatch) -> BlockchainMonitor:
    m = BlockchainMonitor.__new__(BlockchainMonitor)
    m.wallet_address = WALLET
    m.rpc_url = "http://fake"
    m.log_span = 500
    m.tolerance = 0.004
    return m


async def test_fetch_parses_incoming_usdt_transfer(monitor) -> None:
    """The shape the checker job consumes — hash/value/from/timestamp — has to survive the move
    off BSCscan unchanged, since the job and its matching logic were never touched."""
    fake = FakeRPC(
        latest=1000,
        logs=[_log(value_wei=15_200_000_000_000_000_000, sender="0xabc1", block=995, tx="0xdead")],
        block_times={995: 1_700_000_000},
    )
    monitor._call = fake

    transfers = await monitor.fetch_recent_transfers()

    assert len(transfers) == 1
    tx = transfers[0]
    assert tx["hash"] == "0xdead"
    assert tx["value"] == pytest.approx(15.2)
    assert tx["timestamp"] == 1_700_000_000
    assert tx["from"].lower().endswith("abc1")


async def test_filter_targets_usdt_contract_and_our_address(monitor) -> None:
    """A filter that forgot the `to` topic would pull in every USDT transfer on the chain and
    happily credit a payment from a transfer that never reached us."""
    fake = FakeRPC(latest=1000, logs=[], block_times={})
    monitor._call = fake

    await monitor.fetch_recent_transfers()

    get_logs = [p for m, p in fake.calls if m == "eth_getLogs"]
    assert get_logs
    for params in get_logs:
        assert params[0]["address"].lower() == USDT_BEP20_CONTRACT.lower()
        assert params[0]["topics"][0] == TRANSFER_TOPIC
        assert params[0]["topics"][2] == _topic_addr(WALLET)


async def test_lookback_is_chunked_to_survive_provider_span_caps(monitor) -> None:
    """Public BSC endpoints reject a wide `eth_getLogs` range outright ("limit exceeded"). One
    oversized request would fail the whole tick, so the window is walked in bounded chunks."""
    fake = FakeRPC(latest=100_000, logs=[], block_times={})
    monitor._call = fake

    await monitor.fetch_recent_transfers()

    spans = [
        int(p[0]["toBlock"], 16) - int(p[0]["fromBlock"], 16)
        for m, p in fake.calls
        if m == "eth_getLogs"
    ]
    assert len(spans) > 1, "the lookback window should be split across several requests"
    assert max(spans) <= 500


async def test_block_timestamp_is_fetched_once_per_block(monitor) -> None:
    """Two transfers in one block must not cost two block lookups — the job runs every 30s and
    free RPC tiers are rate-limited per call, not per tick."""
    fake = FakeRPC(
        latest=1000,
        logs=[
            _log(value_wei=10**18, sender="0xaaa1", block=990, tx="0x1"),
            _log(value_wei=2 * 10**18, sender="0xbbb2", block=990, tx="0x2"),
        ],
        block_times={990: 1_700_000_500},
    )
    monitor._call = fake

    transfers = await monitor.fetch_recent_transfers()

    assert len(transfers) == 2
    assert all(t["timestamp"] == 1_700_000_500 for t in transfers)
    assert sum(1 for m, _ in fake.calls if m == "eth_getBlockByNumber") == 1


async def test_rpc_error_response_raises(monitor) -> None:
    """A JSON-RPC error body comes back HTTP 200. Swallowing it would let the job report "no
    transfers" forever while every payment quietly expired — exactly the failure being fixed."""

    async def erroring(method: str, params: list):
        raise RuntimeError("BSC RPC error: limit exceeded")

    monitor._call = erroring

    with pytest.raises(RuntimeError):
        await monitor.fetch_recent_transfers()


def test_matches_amount_tolerance(monitor) -> None:
    assert monitor.matches_amount(15.2, 15.2)
    assert monitor.matches_amount(15.203, 15.2)
    assert not monitor.matches_amount(15.21, 15.2)

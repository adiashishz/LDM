import asyncio
import time

from ldm.ratelimit import TokenBucket

MIB = 1024 * 1024


def consume_all(bucket: TokenBucket, total: int, chunk: int = 64 * 1024) -> float:
    async def go():
        start = time.monotonic()
        for _ in range(total // chunk):
            await bucket.consume(chunk)
        return time.monotonic() - start
    return asyncio.run(go())


def test_unlimited_does_not_wait():
    assert consume_all(TokenBucket(0), 8 * MIB) < 0.1


def test_rate_is_enforced():
    assert consume_all(TokenBucket(4 * MIB), MIB) >= 0.2  # 1 MiB at 4 MiB/s ≈ 0.25 s


def test_rate_change_applies_immediately():
    bucket = TokenBucket(1024)
    bucket.set_rate(0)
    assert consume_all(bucket, MIB) < 0.1

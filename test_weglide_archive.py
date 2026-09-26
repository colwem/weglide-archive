import unittest

from weglide_archive import StopAccess, browser_fetch


class FakePage:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    async def evaluate(self, _script, urls):
        self.calls.append(urls)
        return self.responses.pop(0)


class BrowserFetchTests(unittest.IsolatedAsyncioTestCase):
    async def test_retries_only_failed_request_after_status_zero(self):
        page = FakePage([
            [
                {"status": 200, "body": "detail", "retry_after": None},
                {"status": 0, "body": "TypeError: Failed to fetch", "retry_after": None},
            ],
            [{"status": 200, "body": "track", "retry_after": None}],
        ])

        results = await browser_fetch(
            page,
            ["https://example/detail", "https://example/track"],
            transient_retry_min=0,
            transient_retry_max=0,
        )

        self.assertEqual([result.status for result in results], [200, 200])
        self.assertEqual(page.calls[1], ["https://example/track"])

    async def test_explicit_rate_limit_stops_without_retry(self):
        page = FakePage([[
            {"status": 429, "body": "rate limited", "retry_after": "120"},
        ]])

        with self.assertRaisesRegex(StopAccess, "HTTP 429.*Retry-After: 120"):
            await browser_fetch(page, ["https://example/track"], transient_retry_min=0,
                                transient_retry_max=0)

        self.assertEqual(len(page.calls), 1)

    async def test_second_ambiguous_failure_stops(self):
        failure = [{"status": 0, "body": "TypeError: Failed to fetch", "retry_after": None}]
        page = FakePage([failure, failure])

        with self.assertRaisesRegex(StopAccess, "failed twice"):
            await browser_fetch(page, ["https://example/track"], transient_retry_min=0,
                                transient_retry_max=0)

        self.assertEqual(len(page.calls), 2)


if __name__ == "__main__":
    unittest.main()

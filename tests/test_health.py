import asyncio
import unittest

import httpx

from orchestrator.main import app


class HealthEndpointTests(unittest.TestCase):
    def test_health_returns_liveness_response(self) -> None:
        async def get_health():
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://testserver",
            ) as client:
                return await client.get("/health")

        response = asyncio.run(get_health())

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok"})


if __name__ == "__main__":
    unittest.main()

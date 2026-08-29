import pytest
from httpx import ASGITransport, AsyncClient

from backend.app.main import app


@pytest.mark.asyncio
async def test_liveness_does_not_depend_on_external_services() -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/api/v2/health/live")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}

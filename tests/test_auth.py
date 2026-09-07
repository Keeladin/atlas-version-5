
import atlas.api.app as app_module
import httpx
import pytest
from atlas.auth.service import AuthService


@pytest.mark.asyncio
async def test_auth_status_is_open_in_development() -> None:
    transport = httpx.ASGITransport(app=app_module.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get("/api/auth/status")
    assert response.status_code == 200
    assert response.json()["authenticated"] is True


def test_enrollment_code_uses_constant_time_file_check(tmp_path) -> None:
    code_file = tmp_path / "enrollment-code"
    code_file.write_text("abc123\n")
    service = AuthService(
        object(),
        rp_id="localhost",
        rp_name="Atlas V5",
        origin="http://localhost:5173",
        enrollment_code_file=code_file,
        enrolled_marker_file=tmp_path / "enrolled",
    )
    assert service.enrollment_code_valid("abc123") is True
    assert service.enrollment_code_valid("wrong") is False

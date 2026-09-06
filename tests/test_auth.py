
import atlas.api.app as app_module
from atlas.auth.service import AuthService
from fastapi.testclient import TestClient

client = TestClient(app_module.app)


def test_auth_status_is_open_in_development() -> None:
    response = client.get("/api/auth/status")
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

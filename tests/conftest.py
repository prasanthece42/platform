from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from payments_api.main import create_app


@pytest.fixture
def client(tmp_path) -> Iterator[TestClient]:
    with TestClient(create_app(tmp_path / "payments.db")) as test_client:
        yield test_client

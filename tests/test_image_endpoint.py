"""Unit tests for the /api/image endpoint: validation, processing, caching."""

import io
import shutil
import sys
import tempfile
from pathlib import Path
from posixpath import basename

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from fastapi.testclient import TestClient
from PIL import Image

from _runner import run_suite
from syna import server

TEMP_DIRS = []


class FakeContainer:
    """Materializes copied pod files; raises the bollard 404 shape when missing."""

    def __init__(self, pod_files):
        self.pod_files = pod_files

    def copy_from(self, source, destination):
        name = basename(source)
        payload = self.pod_files.get(source, self.pod_files.get(name))
        if payload is None:
            raise RuntimeError(f"Docker API Error (404): no such file {source}")
        Path(destination, name).write_bytes(payload)


class StubRecord:
    """Just the attributes the image endpoint touches."""

    def __init__(self, container):
        self.client_id = "testclient"
        self.tmp_dir = Path(tempfile.mkdtemp(prefix="syna-image-test-"))
        self.container = container
        self.image_cache = {}


def _png_bytes(size=(64, 64), color="red"):
    buffer = io.BytesIO()
    Image.new("RGB", size, color).save(buffer, format="PNG")
    return buffer.getvalue()


def _client_with(monkeypatch, container) -> TestClient:
    record = StubRecord(container)
    TEMP_DIRS.append(record.tmp_dir)
    monkeypatch.setattr(server, "_sessions", {"testclient": record})
    return TestClient(server.app), record


def test_unknown_session_returns_404(monkeypatch):
    monkeypatch.setattr(server, "_sessions", {})
    client = TestClient(server.app)

    response = client.get("/api/image/testclient", params={"path": "/workspace/a.png"})

    assert response.status_code == 404


def test_path_validation(monkeypatch):
    client, _ = _client_with(monkeypatch, FakeContainer({}))
    bad_paths = [
        "/etc/passwd.png",
        "/workspace/../etc/passwd.png",
        "workspace/a.png",
        "/workspace/a.png.txt",
        "/workspace/a.svg",
        "",
    ]
    for pod_path in bad_paths:
        response = client.get("/api/image/testclient", params={"path": pod_path})
        assert response.status_code in (400, 404), pod_path


def test_unknown_pod_file_returns_404(monkeypatch):
    client, _ = _client_with(monkeypatch, FakeContainer({}))

    response = client.get(
        "/api/image/testclient", params={"path": "/workspace/images/missing.png"}
    )

    assert response.status_code == 404


def test_small_image_is_served_untouched(monkeypatch):
    payload = _png_bytes()
    client, _ = _client_with(
        monkeypatch, FakeContainer({"/workspace/images/small.png": payload})
    )

    response = client.get(
        "/api/image/testclient", params={"path": "/workspace/images/small.png"}
    )

    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"
    assert response.headers["cache-control"] == "no-store"
    assert response.content == payload


def test_gif_passes_through_without_recompression(monkeypatch):
    buffer = io.BytesIO()
    Image.new("P", (40, 40), color=3).save(buffer, format="GIF")
    payload = buffer.getvalue()
    client, _ = _client_with(
        monkeypatch, FakeContainer({"/workspace/images/anim.gif": payload})
    )

    response = client.get(
        "/api/image/testclient", params={"path": "/workspace/images/anim.gif"}
    )

    assert response.status_code == 200
    assert response.headers["content-type"] == "image/gif"
    assert response.content == payload


def test_oversized_image_is_downscaled_to_png(monkeypatch):
    payload = _png_bytes(size=(2000, 1500), color="blue")
    client, _ = _client_with(
        monkeypatch, FakeContainer({"/workspace/images/big.png": payload})
    )

    response = client.get(
        "/api/image/testclient", params={"path": "/workspace/images/big.png"}
    )

    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"
    with Image.open(io.BytesIO(response.content)) as served:
        assert served.format == "PNG"
        assert served.width <= 1024
        assert served.height <= 1024


def test_oversized_source_is_rejected(monkeypatch):
    payload = _png_bytes() + b"x" * 5000
    monkeypatch.setattr(server, "MAX_SERVE_BYTES", 4096)
    client, _ = _client_with(
        monkeypatch, FakeContainer({"/workspace/images/huge.png": payload})
    )

    response = client.get(
        "/api/image/testclient", params={"path": "/workspace/images/huge.png"}
    )

    assert response.status_code == 413


def test_oversized_directory_entry_is_rejected(monkeypatch):
    client, _ = _client_with(monkeypatch, FakeContainer({}))

    response = client.get("/api/image/testclient", params={"path": "/workspace/images"})

    assert response.status_code == 400


def test_non_image_file_is_rejected(monkeypatch):
    client, _ = _client_with(
        monkeypatch,
        FakeContainer({"/workspace/images/report.png": b"not an image at all"}),
    )

    response = client.get(
        "/api/image/testclient", params={"path": "/workspace/images/report.png"}
    )

    assert response.status_code == 415


def test_recompress_threshold_converts_to_png(monkeypatch):
    payload = _png_bytes()
    monkeypatch.setattr(server, "RECOMPRESS_BYTES", 8)
    client, _ = _client_with(
        monkeypatch, FakeContainer({"/workspace/images/heavy.jpg": payload})
    )

    response = client.get(
        "/api/image/testclient", params={"path": "/workspace/images/heavy.jpg"}
    )

    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"
    with Image.open(io.BytesIO(response.content)) as served:
        assert served.format == "PNG"
        assert served.size == (64, 64)


def test_image_cache_evicts_oldest(monkeypatch):
    payload = _png_bytes()
    monkeypatch.setattr(server, "IMAGE_CACHE_LIMIT", 2)
    container = FakeContainer(
        {f"/workspace/images/p{index}.png": payload for index in range(3)}
    )
    client, record = _client_with(monkeypatch, container)

    for index in range(3):
        response = client.get(
            "/api/image/testclient", params={"path": f"/workspace/images/p{index}.png"}
        )
        assert response.status_code == 200

    assert len(record.image_cache) == 2


def main():
    """Run every image-endpoint test and report a summary."""
    try:
        run_suite(
            [
                test_unknown_session_returns_404,
                test_path_validation,
                test_unknown_pod_file_returns_404,
                test_small_image_is_served_untouched,
                test_gif_passes_through_without_recompression,
                test_oversized_image_is_downscaled_to_png,
                test_oversized_source_is_rejected,
                test_oversized_directory_entry_is_rejected,
                test_non_image_file_is_rejected,
                test_recompress_threshold_converts_to_png,
                test_image_cache_evicts_oldest,
            ]
        )
    finally:
        for temp_dir in TEMP_DIRS:
            shutil.rmtree(temp_dir, ignore_errors=True)


if __name__ == "__main__":
    main()

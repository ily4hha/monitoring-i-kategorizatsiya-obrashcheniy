"""Exercise actual HTTP/1.1 chunk framing and the documented Uvicorn factory."""
from __future__ import annotations

import http.client
import json
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import threading
import time

import pytest
import uvicorn
from starlette.datastructures import UploadFile

from app.main import create_app
from app.services.dataset_store import MAX_UPLOAD_BYTES
from test_import_security import multipart_parts, workbook


@pytest.fixture
def live_server(tmp_path):
    application = create_app(tmp_path / "runtime")
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(application, log_level="error", lifespan="off", http="h11"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 10
        while not server.started:
            assert thread.is_alive() and time.monotonic() < deadline, "Uvicorn startup failed"
            time.sleep(0.01)
        yield application, port
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        listener.close()
        assert not thread.is_alive()


def send_chunk(connection, chunk):
    connection.send(f"{len(chunk):x}\r\n".encode() + chunk + b"\r\n")


def begin_chunked(port):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    connection.putrequest("POST", "/api/datasets")
    connection.putheader("Content-Type", "multipart/form-data; boundary=test-boundary")
    connection.putheader("Transfer-Encoding", "chunked")
    # Deliberately no Content-Length.
    connection.endheaders()
    return connection


def test_real_chunked_upload_succeeds_without_content_length(live_server):
    application, port = live_server
    prefix, suffix = multipart_parts()
    content = workbook()
    connection = begin_chunked(port)
    try:
        send_chunk(connection, prefix)
        for index in range(0, len(content), 127):
            send_chunk(connection, content[index:index+127])
        send_chunk(connection, suffix)
        connection.send(b"0\r\n\r\n")
        response = connection.getresponse()
        assert response.status == 201, response.read()
        assert json.loads(response.read())["row_count"] == 1
        assert application.state.dataset_store.current_dataset().row_count == 1
    finally:
        connection.close()


def test_real_chunked_upload_rejected_before_end_or_writing_over_limit(live_server, monkeypatch):
    application, port = live_server
    written, handles = [], []
    original = UploadFile.write
    async def track(self, data):
        written.append(len(data))
        handles.append(self.file)
        await original(self, data)
    monkeypatch.setattr(UploadFile, "write", track)
    connection = begin_chunked(port)
    try:
        send_chunk(connection, multipart_parts()[0])
        for _ in range(MAX_UPLOAD_BYTES // (64 * 1024)):
            send_chunk(connection, b"x" * (64 * 1024))
        send_chunk(connection, b"x" * 1024)
        # Do not finish either the multipart or the chunked stream. A protected
        # server must reply now instead of waiting for the rest of a 26 MiB file.
        response = connection.getresponse()
        assert response.status == 400, response.read()
        assert "превышает" in json.loads(response.read())["detail"]
        assert sum(written) <= MAX_UPLOAD_BYTES
        assert handles and all(handle.closed for handle in handles)
        assert application.state.dataset_store.current_dataset() is None
        assert list(application.state.dataset_store.upload_dir.iterdir()) == []
    finally:
        connection.close()


def test_documented_production_factory_runs_from_clean_copy(tmp_path):
    root = Path(__file__).resolve().parents[1]
    shutil.copytree(root / "app", tmp_path / "app", ignore=shutil.ignore_patterns("__pycache__"))
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    with (tmp_path / "uvicorn.log").open("w+") as log:
        process = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "app.main:create_app", "--factory", "--host", "127.0.0.1", "--port", str(port), "--log-level", "error"],
            cwd=tmp_path, stdout=log, stderr=log,
        )
        try:
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                assert process.poll() is None, "Uvicorn factory failed to start"
                connection = http.client.HTTPConnection("127.0.0.1", port, timeout=1)
                try:
                    connection.request("GET", "/api/health")
                    response = connection.getresponse()
                    assert response.status == 200 and json.loads(response.read()) == {"status": "ok"}
                    break
                except OSError:
                    time.sleep(0.02)
                finally:
                    connection.close()
            else:
                pytest.fail("Uvicorn factory did not become healthy")
            assert (tmp_path / "data/runtime/app.db").exists()
        finally:
            process.terminate()
            process.wait(timeout=10)

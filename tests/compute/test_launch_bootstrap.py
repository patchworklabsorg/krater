from __future__ import annotations

import http.server
import re
import subprocess
import sys
import threading

from krater.compute import launch


def test_every_download_in_the_bootstrap_names_itself() -> None:
    script = launch.bootstrap_script("0" * 64)
    assert "curl -fsSL -A krater-bootstrap" in script
    assert "wget -q -U krater-bootstrap" in script
    assert '"User-Agent": "krater-bootstrap"' in script


def test_every_download_is_bounded_and_kept_only_when_complete() -> None:
    """Staging, 2026-11-12: one download was cut short and the next hung past the runner start deadline. A cut-short
    curl or wget download used to stay as the runner, so the loop stopped and the checksum refused it."""
    script = launch.bootstrap_script("0" * 64)
    assert "--max-time 90" in script and "-T 30" in script and "timeout=30" in script
    for fetch in ('-o "$R.part"', '-O "$R.part"', '"$U" "$R.part"'):
        assert f'{fetch} "$U" && mv -f "$R.part" "$R"' in script or f'{fetch} && mv -f "$R.part" "$R"' in script


def test_the_python_fallback_writes_nothing_from_a_cut_short_download(tmp_path) -> None:
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self.send_response(200)
            self.send_header("Content-Length", "1000")
            self.end_headers()
            self.wfile.write(b"only part of it")

        def log_message(self, *args) -> None:
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.handle_request, daemon=True).start()
    program = re.search(r"^P='(.*?)'$", launch.bootstrap_script("0" * 64), re.MULTILINE | re.DOTALL).group(1)
    out = tmp_path / "runner.part"
    result = subprocess.run(
        [sys.executable, "-c", program, f"http://127.0.0.1:{server.server_port}/runner/v1/krater-runner", str(out)],
        capture_output=True,
        timeout=60,
    )
    server.server_close()
    assert result.returncode != 0
    assert not out.exists()


def test_the_python_fallback_sends_its_user_agent_and_saves_the_file(tmp_path) -> None:
    """Cloudflare refuses Python's default user agent (error 1010), and slim Python images have no curl or wget."""
    seen: list[str] = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            seen.append(self.headers.get("User-Agent", ""))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"runner-bytes")

        def log_message(self, *args) -> None:
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.handle_request, daemon=True).start()
    program = re.search(r"^P='(.*?)'$", launch.bootstrap_script("0" * 64), re.MULTILINE | re.DOTALL).group(1)
    out = tmp_path / "runner"
    subprocess.run(
        [sys.executable, "-c", program, f"http://127.0.0.1:{server.server_port}/runner/v1/krater-runner", str(out)],
        check=True,
        timeout=30,
    )
    server.server_close()
    assert seen == ["krater-bootstrap"]
    assert out.read_bytes() == b"runner-bytes"

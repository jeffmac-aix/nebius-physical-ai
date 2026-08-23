"""Owner-scoped lifecycle for a dedicated local SkyPilot API server."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import time
from urllib.error import URLError
from urllib.request import urlopen

from npa.orchestration.skypilot._bin import ensure_skypilot_version
from npa.orchestration.skypilot.cleanup import sky_environment


class IsolatedApiServerError(RuntimeError):
    """Raised when the exact isolated server cannot be safely managed."""


@dataclass(frozen=True)
class IsolatedApiServer:
    endpoint: str
    pid: int
    port: int
    state_dir: Path
    reused: bool

    def to_dict(self) -> dict[str, object]:
        return {
            "endpoint": self.endpoint,
            "pid": self.pid,
            "port": self.port,
            "state_dir": str(self.state_dir),
            "reused": self.reused,
            "status": "healthy",
        }


def ensure_isolated_api_server(
    *, sky_bin: str | os.PathLike[str], state_dir: Path, port: int
) -> IsolatedApiServer:
    """Start or reuse one exact loopback server without touching shared state."""

    root = _validate_state_dir(state_dir)
    endpoint = f"http://127.0.0.1:{_validate_port(port)}"
    queue_port = _validate_port(port + 1_000)
    record_path = root / "server.json"
    existing = _load_record(record_path)
    if existing:
        pid = int(existing.get("pid") or 0)
        if _owned_process(pid, port) and _healthy(endpoint):
            return IsolatedApiServer(endpoint, pid, port, root, True)
        if _process_exists(pid):
            raise IsolatedApiServerError(
                "isolated SkyPilot server record points at a non-matching live process"
            )
        record_path.unlink(missing_ok=True)

    occupied = [candidate for candidate in (port, queue_port) if not _port_available(candidate)]
    if occupied:
        raise IsolatedApiServerError(
            f"loopback port {occupied[0]} is already occupied by an unowned process"
        )
    executable = ensure_skypilot_version(sky_bin)
    python_bin = executable.parent / "python"
    log_path = root / "server.log"
    flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    log_fd = os.open(log_path, flags, 0o600)
    os.fchmod(log_fd, 0o600)
    env = sky_environment(root)
    env.pop("SKYPILOT_API_SERVER_ENDPOINT", None)
    # SkyPilot 0.12.2 exposes the HTTP port but hardcodes its multiprocessing
    # queue manager to 50011. Set that imported constant before loading the
    # server module so multiple owner-scoped servers can coexist safely.
    server_entrypoint = (
        "import runpy,sys;"
        "from sky.server.requests.queues import mp_queue;"
        "mp_queue.DEFAULT_QUEUE_MANAGER_PORT=int(sys.argv.pop(1));"
        "runpy.run_module('sky.server.server',run_name='__main__')"
    )
    command = [
        str(python_bin),
        "-c",
        server_entrypoint,
        str(queue_port),
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
    ]
    try:
        process = subprocess.Popen(  # noqa: S603 - exact pinned interpreter/argv
            command,
            cwd=root,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=log_fd,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    finally:
        os.close(log_fd)
    try:
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise IsolatedApiServerError(
                    f"isolated SkyPilot API server exited with code {process.returncode}"
                )
            if _healthy(endpoint):
                _write_record(
                    record_path,
                    {
                        "pid": process.pid,
                        "port": port,
                        "queue_port": queue_port,
                        "python": str(python_bin.resolve()),
                        "started_at": datetime.now(timezone.utc).isoformat(),
                    },
                )
                return IsolatedApiServer(endpoint, process.pid, port, root, False)
            time.sleep(0.5)
        raise IsolatedApiServerError("isolated SkyPilot API server did not become healthy")
    except Exception:
        _terminate_owned_process(process.pid, port)
        raise


def stop_isolated_api_server(*, state_dir: Path) -> dict[str, object]:
    """Stop only the process attested by an exact owner-only server record."""

    root = _validate_state_dir(state_dir)
    record_path = root / "server.json"
    record = _load_record(record_path)
    if not record:
        return {"status": "absent", "stopped": False, "state_dir": str(root)}
    pid = int(record.get("pid") or 0)
    port = int(record.get("port") or 0)
    if _process_exists(pid) and not _owned_process(pid, port):
        raise IsolatedApiServerError(
            "refusing to stop a process that does not match the isolated server record"
        )
    if _process_exists(pid):
        _terminate_owned_process(pid, port)
    record_path.unlink(missing_ok=True)
    return {"status": "stopped", "stopped": True, "state_dir": str(root)}


def _validate_state_dir(path: Path) -> Path:
    root = path.expanduser().resolve()
    forbidden = {Path("/"), Path.home().resolve()}
    if root in forbidden or len(root.parts) < 3:
        raise IsolatedApiServerError(f"unsafe isolated server state directory: {root}")
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    root.chmod(0o700)
    return root


def _validate_port(port: int) -> int:
    if not 1024 <= int(port) <= 65535:
        raise IsolatedApiServerError("isolated server port must be between 1024 and 65535")
    return int(port)


def _healthy(endpoint: str) -> bool:
    try:
        with urlopen(f"{endpoint}/api/health", timeout=2) as response:  # noqa: S310
            return response.status == 200
    except (OSError, URLError):
        return False


def _port_available(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def _process_exists(pid: int) -> bool:
    if pid <= 1:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _owned_process(pid: int, port: int) -> bool:
    try:
        argv = (Path("/proc") / str(pid) / "cmdline").read_bytes().split(b"\0")
    except OSError:
        return False
    decoded = [item.decode("utf-8", "replace") for item in argv if item]
    return (
        "-c" in decoded
        and "sky.server.server" in decoded
        and "--port" in decoded
        and str(port) in decoded
    )


def _terminate_owned_process(pid: int, port: int) -> None:
    if not _owned_process(pid, port):
        return
    try:
        os.killpg(os.getpgid(pid), signal.SIGTERM)
    except ProcessLookupError:
        return
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline and _process_exists(pid):
        time.sleep(0.1)
    if _process_exists(pid) and _owned_process(pid, port):
        os.killpg(os.getpgid(pid), signal.SIGKILL)


def _load_record(path: Path) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _write_record(path: Path, value: dict[str, object]) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")
    temporary.chmod(0o600)
    os.replace(temporary, path)

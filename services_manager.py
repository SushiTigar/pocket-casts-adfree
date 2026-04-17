"""Services panel backend.

Discovers, starts, stops, and inspects the four backend services this
project depends on:

    - Ollama        (LLM, port 11434)         — managed via `brew services` or `ollama serve`
    - Whisper       (transcription, port 8765)— native Metal binary OR Docker container
    - MinusPod      (ad detection, port 8000) — Flask app under MinusPod/venv
    - Pipeline UI   (this app, port 5050)     — `pocketcasts_adfree.py ui`

Design goals:

    - Pure helpers, no Flask import — easy to unit-test by patching subprocess.
    - HTTP health probes are the source of truth for "running"; pid lookup
      is best-effort context for the panel and logs.
    - Mutations (start/stop/restart) shell out to the same scripts the
      README documents (`start_services.sh`, `brew services`, `docker compose`).
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).parent
WHISPER_DIR = ROOT / "whisper.cpp"
WHISPER_BIN = WHISPER_DIR / "build" / "bin" / "whisper-server"
WHISPER_MODEL_DIR = WHISPER_DIR / "models"
MINUSPOD_DIR = ROOT / "MinusPod"
MINUSPOD_LOG = Path("/tmp/minuspod.log")
WHISPER_LOG = Path("/tmp/whisper-server.log")
OLLAMA_LOG_GUESSES = [
    Path.home() / "Library/Logs/Homebrew/ollama/ollama.log",
    Path("/tmp/ollama.log"),
]
UI_LOG = Path("/tmp/pocketcasts-ui.log")


# ---------------------------------------------------------------------------
# Low-level helpers
# ---------------------------------------------------------------------------

def _http_ok(url: str, timeout: float = 2.0, expect_substr: str | None = None) -> bool:
    """Treat 2xx as healthy; optionally also require substring in the body."""
    try:
        r = httpx.get(url, timeout=timeout)
        if r.status_code >= 400:
            return False
        if expect_substr is not None:
            return expect_substr in r.text
        return True
    except Exception:
        return False


def _pid_listening(port: int) -> int | None:
    """Find pid of the process currently listening on `port` (TCP, IPv4 or v6)."""
    try:
        out = subprocess.run(
            ["lsof", "-nP", "-iTCP:" + str(port), "-sTCP:LISTEN", "-t"],
            capture_output=True, text=True, timeout=5,
        )
        if out.returncode != 0:
            return None
        for line in out.stdout.splitlines():
            line = line.strip()
            if line.isdigit():
                return int(line)
    except Exception:
        return None
    return None


def _proc_command(pid: int) -> str | None:
    """Return the full command line for a pid, or None if it doesn't exist."""
    try:
        out = subprocess.run(
            ["ps", "-p", str(pid), "-o", "command="],
            capture_output=True, text=True, timeout=5,
        )
        if out.returncode == 0:
            return out.stdout.strip() or None
    except Exception:
        pass
    return None


def _read_log_tail(path: Path, lines: int = 200) -> str:
    """Best-effort tail. Empty string when file is missing or unreadable."""
    if not path.exists():
        return ""
    try:
        # Read up to last ~200KB to avoid loading 100MB log files in full
        size = path.stat().st_size
        with path.open("rb") as f:
            if size > 200_000:
                f.seek(size - 200_000)
                f.readline()  # drop partial first line
            data = f.read().decode("utf-8", errors="replace")
        return "\n".join(data.splitlines()[-lines:])
    except Exception as e:
        return f"<failed to read {path}: {e}>"


def _find_first_existing(paths: list[Path]) -> Path | None:
    for p in paths:
        if p.exists():
            return p
    return None


def _docker_container_status(name: str) -> str | None:
    """Return docker container status string, or None if docker is unavailable
    or the container doesn't exist."""
    if not shutil.which("docker"):
        return None
    try:
        out = subprocess.run(
            ["docker", "inspect", "-f", "{{.State.Status}}", name],
            capture_output=True, text=True, timeout=5,
        )
        if out.returncode == 0:
            return out.stdout.strip() or None
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# Per-service status
# ---------------------------------------------------------------------------

@dataclass
class ServiceStatus:
    id: str
    name: str
    running: bool
    healthy: bool
    pid: int | None
    port: int | None
    backend: str | None  # e.g. "native", "docker", "brew", "manual"
    extra: dict
    log_path: str | None
    can_start: bool
    can_stop: bool
    can_restart: bool

    def as_dict(self) -> dict:
        return asdict(self)


def status_ollama() -> ServiceStatus:
    pid = _pid_listening(11434)
    healthy = _http_ok("http://localhost:11434/api/tags")
    extra: dict = {}
    if healthy:
        try:
            r = httpx.get("http://localhost:11434/api/tags", timeout=3)
            extra["models"] = [m["name"] for m in (r.json().get("models") or [])]
        except Exception:
            pass
    backend = None
    if pid:
        cmd = _proc_command(pid) or ""
        if "brew" in cmd or "/opt/homebrew" in cmd:
            backend = "brew"
        else:
            backend = "manual"
    return ServiceStatus(
        id="ollama", name="Ollama", running=pid is not None,
        healthy=healthy, pid=pid, port=11434, backend=backend,
        extra=extra,
        log_path=str(_find_first_existing(OLLAMA_LOG_GUESSES) or ""),
        can_start=True, can_stop=pid is not None, can_restart=True,
    )


def status_whisper() -> ServiceStatus:
    pid = _pid_listening(8765)
    healthy = _http_ok("http://localhost:8765/health")
    backend = None
    extra: dict = {
        "native_binary_exists": WHISPER_BIN.exists(),
        "models_dir": str(WHISPER_MODEL_DIR),
        "available_models": (
            sorted(p.name for p in WHISPER_MODEL_DIR.glob("ggml-*.bin"))
            if WHISPER_MODEL_DIR.exists() else []
        ),
    }
    docker_status = _docker_container_status("whisper-server")
    if docker_status:
        extra["docker_container_status"] = docker_status
    if pid:
        cmd = _proc_command(pid) or ""
        if "com.docker" in cmd or docker_status == "running":
            backend = "docker"
            extra["warning"] = (
                "Docker Whisper on Apple Silicon runs under emulation and "
                "is ~10x slower than the native Metal build. Switch to "
                "'Native (Metal)' for proper GPU acceleration."
            )
        else:
            backend = "native"
    return ServiceStatus(
        id="whisper", name="Whisper",
        running=pid is not None, healthy=healthy, pid=pid, port=8765,
        backend=backend, extra=extra,
        log_path=str(WHISPER_LOG),
        can_start=True, can_stop=pid is not None, can_restart=True,
    )


def status_minuspod() -> ServiceStatus:
    pid = _pid_listening(8000)
    healthy = _http_ok(
        "http://localhost:8000/api/v1/health", expect_substr="healthy"
    )
    extra: dict = {}
    if healthy:
        try:
            r = httpx.get("http://localhost:8000/api/v1/status", timeout=3)
            j = r.json()
            extra["currentJob"] = j.get("currentJob")
            extra["queueLength"] = j.get("queueLength")
        except Exception:
            pass
    return ServiceStatus(
        id="minuspod", name="MinusPod",
        running=pid is not None, healthy=healthy, pid=pid, port=8000,
        backend="native", extra=extra,
        log_path=str(MINUSPOD_LOG),
        can_start=True, can_stop=pid is not None, can_restart=True,
    )


def status_ui() -> ServiceStatus:
    pid = _pid_listening(5050)
    healthy = _http_ok("http://localhost:5050/api/queue/status")
    return ServiceStatus(
        id="ui", name="Pipeline UI",
        running=pid is not None, healthy=healthy, pid=pid, port=5050,
        backend="native", extra={"note": "Stopping this service stops the panel itself."},
        log_path=str(UI_LOG),
        # The UI itself is what's hosting the panel — refuse stop/restart
        # to avoid the "saw off the branch you're sitting on" footgun.
        can_start=False, can_stop=False, can_restart=False,
    )


def all_statuses() -> list[ServiceStatus]:
    return [status_ollama(), status_whisper(), status_minuspod(), status_ui()]


# ---------------------------------------------------------------------------
# Mutations
# ---------------------------------------------------------------------------

class ServiceError(RuntimeError):
    pass


def _wait_until(predicate, timeout: float = 30.0, interval: float = 0.5) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


def _kill_pid(pid: int, sig: int = signal.SIGTERM) -> None:
    try:
        os.kill(pid, sig)
    except ProcessLookupError:
        pass


def stop_ollama() -> dict:
    # Try brew first (graceful), then SIGTERM the listening pid as fallback.
    if shutil.which("brew"):
        try:
            subprocess.run(
                ["brew", "services", "stop", "ollama"],
                capture_output=True, text=True, timeout=15,
            )
        except Exception:
            pass
    pid = _pid_listening(11434)
    if pid:
        _kill_pid(pid)
    ok = _wait_until(lambda: _pid_listening(11434) is None, timeout=15)
    return {"ok": ok}


def start_ollama() -> dict:
    if _pid_listening(11434):
        return {"ok": True, "note": "already running"}
    if shutil.which("brew"):
        try:
            subprocess.run(
                ["brew", "services", "start", "ollama"],
                capture_output=True, text=True, timeout=15,
            )
        except Exception:
            pass
    if not _pid_listening(11434):
        # Detached background process — use Popen with double-fork-equivalent
        try:
            subprocess.Popen(
                ["ollama", "serve"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except FileNotFoundError:
            raise ServiceError("`ollama` not found on PATH. brew install ollama.")
    ok = _wait_until(
        lambda: _http_ok("http://localhost:11434/api/tags"), timeout=20,
    )
    return {"ok": ok}


def restart_ollama() -> dict:
    stop_ollama()
    return start_ollama()


def stop_whisper() -> dict:
    """Stop whichever flavor is currently bound to 8765 (docker or native)."""
    docker_status = _docker_container_status("whisper-server")
    if docker_status in ("running", "restarting", "paused"):
        try:
            subprocess.run(
                ["docker", "stop", "whisper-server"],
                capture_output=True, text=True, timeout=20,
            )
        except Exception:
            pass
    pid = _pid_listening(8765)
    if pid:
        _kill_pid(pid)
    ok = _wait_until(lambda: _pid_listening(8765) is None, timeout=15)
    return {"ok": ok}


def _start_whisper_native() -> dict:
    if not WHISPER_BIN.exists():
        raise ServiceError(
            f"Native whisper-server binary not found at {WHISPER_BIN}. "
            "Build it: cd whisper.cpp && cmake -B build "
            "-DWHISPER_METAL=ON -DWHISPER_METAL_EMBED_LIBRARY=ON && "
            "cmake --build build --config Release -j"
        )
    # Pick the first model that exists, preferring large-v3-turbo.
    preferred = WHISPER_MODEL_DIR / "ggml-large-v3-turbo.bin"
    model = preferred if preferred.exists() else next(
        iter(WHISPER_MODEL_DIR.glob("ggml-*.bin")), None
    )
    if model is None:
        raise ServiceError(
            f"No Whisper model found in {WHISPER_MODEL_DIR}. "
            "Run: cd whisper.cpp/models && bash download-ggml-model.sh large-v3-turbo"
        )
    cores = subprocess.run(
        ["sysctl", "-n", "hw.performancecores"],
        capture_output=True, text=True, timeout=3,
    ).stdout.strip() or "4"
    log_fd = open(WHISPER_LOG, "ab")
    subprocess.Popen(
        [
            str(WHISPER_BIN),
            "--host", "0.0.0.0", "--port", "8765",
            "--model", str(model),
            "--inference-path", "/v1/audio/transcriptions",
            "--dtw", "large.v3.turbo",
            "--no-flash-attn",
            "--threads", cores, "--processors", cores,
        ],
        stdout=log_fd, stderr=log_fd, start_new_session=True,
    )
    ok = _wait_until(
        lambda: _http_ok("http://localhost:8765/health"), timeout=30,
    )
    return {"ok": ok, "backend": "native", "model": model.name}


def _start_whisper_docker() -> dict:
    if not shutil.which("docker"):
        raise ServiceError("Docker not installed.")
    compose = MINUSPOD_DIR / "docker-compose.whisper.yml"
    if not compose.exists():
        raise ServiceError(f"Compose file missing: {compose}")
    proc = subprocess.run(
        ["docker", "compose", "-f", str(compose), "up", "-d"],
        capture_output=True, text=True, timeout=60,
    )
    if proc.returncode != 0:
        raise ServiceError(f"docker compose failed: {proc.stderr.strip()}")
    ok = _wait_until(
        lambda: _http_ok("http://localhost:8765/health"), timeout=120,
    )
    return {"ok": ok, "backend": "docker"}


def start_whisper(backend: str = "native") -> dict:
    if _pid_listening(8765) and _http_ok("http://localhost:8765/health"):
        return {"ok": True, "note": "already running"}
    backend = (backend or "native").lower()
    if backend == "native":
        return _start_whisper_native()
    if backend == "docker":
        return _start_whisper_docker()
    raise ServiceError(f"Unknown whisper backend: {backend!r}")


def restart_whisper(backend: str = "native") -> dict:
    stop_whisper()
    return start_whisper(backend)


def stop_minuspod() -> dict:
    pid = _pid_listening(8000)
    if pid:
        _kill_pid(pid)
        # MinusPod is a Flask reloader parent + child; SIGTERM the group.
        try:
            os.killpg(os.getpgid(pid), signal.SIGTERM)
        except Exception:
            pass
    ok = _wait_until(lambda: _pid_listening(8000) is None, timeout=15)
    return {"ok": ok}


def start_minuspod() -> dict:
    if _pid_listening(8000):
        return {"ok": True, "note": "already running"}
    venv_python = MINUSPOD_DIR / "venv" / "bin" / "python"
    if not venv_python.exists():
        raise ServiceError(
            f"MinusPod venv not found at {venv_python}. "
            "Run: cd MinusPod && python3 -m venv venv && "
            "source venv/bin/activate && pip install -r requirements.txt"
        )
    src_dir = MINUSPOD_DIR / "src"
    if not src_dir.exists():
        raise ServiceError(f"MinusPod source dir missing: {src_dir}")

    cores = subprocess.run(
        ["sysctl", "-n", "hw.performancecores"],
        capture_output=True, text=True, timeout=3,
    ).stdout.strip() or "4"

    env = os.environ.copy()
    env.update({
        "DATA_DIR": str(MINUSPOD_DIR / "data"),
        "LLM_PROVIDER": "ollama",
        "OPENAI_BASE_URL": "http://localhost:11434/v1",
        "OPENAI_API_KEY": "not-needed",
        "OPENAI_MODEL": env.get("OPENAI_MODEL", "qwen3.5-addetect"),
        "WHISPER_BACKEND": "openai-api",
        "WHISPER_API_BASE_URL": "http://localhost:8765/v1",
        "WHISPER_DEVICE": "cpu",
        "BASE_URL": "http://localhost:8000",
        "HF_HOME": str(MINUSPOD_DIR / "data" / ".cache"),
        "SKIP_VERIFICATION": "true",
        "WINDOW_SIZE_SECONDS": "600",
        "WINDOW_OVERLAP_SECONDS": "120",
        "AD_DETECTION_MAX_TOKENS": "4096",
        "OLLAMA_NUM_PARALLEL": "2",
        "PYTHONPATH": ".",
    })
    log_fd = open(MINUSPOD_LOG, "ab")
    subprocess.Popen(
        [
            str(venv_python), "-m", "flask",
            "--app", "main_app:app",
            "run", "--host", "0.0.0.0", "--port", "8000",
        ],
        cwd=str(src_dir), env=env,
        stdout=log_fd, stderr=log_fd, start_new_session=True,
    )
    ok = _wait_until(
        lambda: _http_ok(
            "http://localhost:8000/api/v1/health", expect_substr="healthy",
        ),
        timeout=30,
    )
    return {"ok": ok}


def restart_minuspod() -> dict:
    stop_minuspod()
    return start_minuspod()


# ---------------------------------------------------------------------------
# Ollama model management
# ---------------------------------------------------------------------------

def list_ollama_models() -> list[dict]:
    try:
        r = httpx.get("http://localhost:11434/api/tags", timeout=5)
        if r.status_code != 200:
            return []
        return r.json().get("models", [])
    except Exception:
        return []


def get_minuspod_model() -> str | None:
    """Return the model MinusPod currently uses for ad detection."""
    try:
        r = httpx.get(
            "http://localhost:8000/api/v1/settings/ad-detection", timeout=5,
        )
        if r.status_code == 200:
            j = r.json()
            return (
                j.get("claudeModel")
                or j.get("model")
                or j.get("settings", {}).get("claudeModel")
            )
    except Exception:
        pass
    return None


def set_minuspod_model(model: str) -> dict:
    if not model:
        raise ServiceError("model name required")
    body = {
        "claudeModel": model,
        "verificationModel": model,
        "chaptersModel": model,
    }
    try:
        r = httpx.put(
            "http://localhost:8000/api/v1/settings/ad-detection",
            json=body, timeout=10,
        )
        return {"ok": r.status_code < 400, "status_code": r.status_code}
    except Exception as e:
        raise ServiceError(f"MinusPod settings update failed: {e}")


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------

ACTIONS: dict[str, dict] = {
    "ollama":    {"start": start_ollama,   "stop": stop_ollama,   "restart": restart_ollama},
    "whisper":   {"start": start_whisper,  "stop": stop_whisper,  "restart": restart_whisper},
    "minuspod":  {"start": start_minuspod, "stop": stop_minuspod, "restart": restart_minuspod},
}


def perform_action(service_id: str, action: str, **kwargs: Any) -> dict:
    """Dispatch a start/stop/restart action.

    Whisper start/restart accept an optional `backend` kwarg ("native"
    or "docker"). All other services ignore extras.
    """
    svc = ACTIONS.get(service_id)
    if not svc:
        raise ServiceError(f"Unknown service: {service_id}")
    fn = svc.get(action)
    if not fn:
        raise ServiceError(f"Unsupported action {action!r} for {service_id}")
    if service_id == "whisper" and action in ("start", "restart"):
        return fn(backend=kwargs.get("backend", "native"))
    return fn()

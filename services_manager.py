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

import getpass
import logging
import os
import shutil
import signal
import subprocess
import threading
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any

import httpx

log = logging.getLogger(__name__)

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
MINUSPOD_PATCH = ROOT / "patches" / "minuspod-local.patch"
# Additional additive patches (LLM cost-optimisations, etc.) reapplied
# on top of the core patch after each upstream update. Order matters:
# each patch is applied to whatever the previous one left behind.
MINUSPOD_ADDITIONAL_PATCHES = [
    ROOT / "patches" / "llm-cost-optimizations.patch",
    ROOT / "patches" / "house-ad-detection.patch",
]

_KEYCHAIN_ENV_MAP = {
    "pocketcasts-email": "POCKETCASTS_EMAIL",
    "pocketcasts-password": "POCKETCASTS_PASSWORD",
    "openrouter-api-key": "OPENROUTER_API_KEY",
    "ui-auth-password": "UI_AUTH_PASSWORD",
}

# Internet-password entries show in the Passwords app and sync via iCloud Keychain.
# Generic-password entries (legacy) are still read as a fallback.
_POCKETCASTS_INTERNET_SERVER = "pocketcasts.com"
_OPENROUTER_INTERNET_SERVER = "openrouter.ai"
_OPENROUTER_INTERNET_ACCOUNT = "api-key"
_UI_INTERNET_SERVER = "localhost"
_UI_INTERNET_PORT = 5050


def _run_security(args: list[str]) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            ["security", *args],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return subprocess.CompletedProcess(args, 1, "", "")


def _get_internet_password(
    server: str,
    account: str | None = None,
    port: int | None = None,
) -> str:
    """Read password from an Internet password item (Passwords app / iCloud)."""
    args = ["find-internet-password", "-s", server]
    if account:
        args.extend(["-a", account])
    if port is not None:
        args.extend(["-P", str(port)])
    args.append("-w")
    result = _run_security(args)
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


def _get_internet_account(server: str, port: int | None = None) -> str:
    """Read the account field from an Internet password (e.g. Pocket Casts email)."""
    args = ["find-internet-password", "-s", server]
    if port is not None:
        args.extend(["-P", str(port)])
    result = _run_security(args)
    if result.returncode != 0:
        return ""
    for line in result.stdout.splitlines():
        if '"acct"' in line or "acct" in line:
            # acct"<blob>="email@example.com"
            if '"' in line:
                parts = line.split('"')
                for i, part in enumerate(parts):
                    if part == "acct" and i + 2 < len(parts):
                        return parts[i + 2]
    return ""


def _get_generic_password(service: str) -> str:
    """Read a legacy generic password from Keychain. Returns '' on failure."""
    account = getpass.getuser()
    result = _run_security(
        ["find-generic-password", "-a", account, "-s", service, "-w"]
    )
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


def _get_keychain_secret(service: str) -> str:
    """Read a secret from Passwords-app (internet) or legacy generic Keychain."""
    if service == "ui-auth-password":
        ui_user = os.environ.get("UI_AUTH_USER", "admin")
        value = _get_internet_password(
            _UI_INTERNET_SERVER, account=ui_user, port=_UI_INTERNET_PORT,
        )
        if value:
            return value
        return _get_generic_password(service)

    if service == "openrouter-api-key":
        value = _get_internet_password(
            _OPENROUTER_INTERNET_SERVER, account=_OPENROUTER_INTERNET_ACCOUNT,
        )
        if value:
            return value
        return _get_generic_password(service)

    if service == "pocketcasts-password":
        value = _get_internet_password(_POCKETCASTS_INTERNET_SERVER)
        if value:
            return value
        return _get_generic_password(service)

    if service == "pocketcasts-email":
        value = _get_internet_account(_POCKETCASTS_INTERNET_SERVER)
        if value:
            return value
        return _get_generic_password(service)

    return _get_generic_password(service)


def load_keychain_secrets_into_environ() -> int:
    """Overlay missing env vars from Keychain. Returns count of keys set."""
    loaded = 0
    for service, env_key in _KEYCHAIN_ENV_MAP.items():
        if os.environ.get(env_key):
            continue
        value = _get_keychain_secret(service)
        if value:
            os.environ[env_key] = value
            loaded += 1
    return loaded


def _overlay_openrouter_from_keychain(env: dict) -> None:
    key = _get_keychain_secret("openrouter-api-key")
    if key:
        env["OPENROUTER_API_KEY"] = key


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


def _reload_dotenv_into(env: dict, exclude: set[str] | None = None) -> int:
    """Re-read ``ROOT/.env`` and overlay keys onto ``env``.

    The parent process (UI / pocketcasts_adfree.py) loads ``.env`` at startup
    (see ``_load_dotenv_file`` in pocketcasts_adfree.py). If the user edits
    ``.env`` afterwards and clicks "Restart
    MinusPod", the parent's ``os.environ`` is stale and would otherwise be
    copied into the subprocess unchanged. This helper re-reads ``.env`` and
    merges it on top so the subprocess picks up the latest values without
    requiring a full UI restart.

    Returns the number of keys overlaid (for logging).

    Lines starting with ``#`` and empty lines are skipped. ``export`` prefix
    and surrounding quotes are stripped. Values that look empty after that
    are skipped (we don't want to mask an existing shell value with an
    empty string).
    """
    exclude = exclude or set()
    env_path = ROOT / ".env"
    if not env_path.exists():
        return 0
    overlaid = 0
    try:
        for raw_line in env_path.read_text().splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[len("export "):].strip()
            if "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip()
            if key in exclude or not key:
                continue
            if not value:
                continue
            if (value.startswith('"') and value.endswith('"')) or (
                value.startswith("'") and value.endswith("'")
            ):
                value = value[1:-1]
            env[key] = value
            overlaid += 1
    except Exception as exc:
        log.warning("Failed to reload .env from %s: %s", env_path, exc)
    return overlaid


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
    healthy = _http_ok("http://localhost:5050/api/health")
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
        lambda: _http_ok("http://localhost:11434/api/tags"), timeout=45,
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
            
    # Find all pids listening on Whisper's port
    pids = []
    try:
        out = subprocess.run(
            ["lsof", "-nP", "-iTCP:8765", "-sTCP:LISTEN", "-t"],
            capture_output=True, text=True, timeout=5,
        )
        if out.returncode == 0:
            for line in out.stdout.splitlines():
                line = line.strip()
                if line.isdigit():
                    pids.append(int(line))
    except Exception:
        pass

    for pid in pids:
        _kill_pid(pid, signal.SIGTERM)
        
    # Give them up to 3 seconds to exit gracefully, then escalate to SIGKILL
    if pids and not _wait_until(lambda: _pid_listening(8765) is None, timeout=3):
        for pid in pids:
            _kill_pid(pid, signal.SIGKILL)
            
    ok = _wait_until(lambda: _pid_listening(8765) is None, timeout=12)
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
    # Pick a sane thread count without blowing up the Metal command-buffer
    # budget. `hw.performancecores` only exists on Apple Silicon Sequoia+;
    # older OIDs are `hw.perflevel0.physicalcpu`. Fall back to a hard floor
    # of 4 if neither is available. Cap at 8 because:
    #   - whisper.cpp's Metal backend has a hard ceiling of 8 command
    #     buffers (GGML_METAL_MAX_COMMAND_BUFFERS); going higher crashes.
    #   - more threads than perf cores on Apple Silicon just degrades
    #     performance because efficiency cores run Metal kernels slowly.
    cores_raw = (
        subprocess.run(
            ["sysctl", "-n", "hw.performancecores"],
            capture_output=True, text=True, timeout=3,
        ).stdout.strip()
        or subprocess.run(
            ["sysctl", "-n", "hw.perflevel0.physicalcpu"],
            capture_output=True, text=True, timeout=3,
        ).stdout.strip()
        or "4"
    )
    try:
        cores = str(min(int(cores_raw), 8))
    except ValueError:
        cores = "4"
    log_fd = open(WHISPER_LOG, "ab")
    subprocess.Popen(
        [
            str(WHISPER_BIN),
            "--host", "0.0.0.0", "--port", "8765",
            "--model", str(model),
            "--inference-path", "/v1/audio/transcriptions",
            "--dtw", "large.v3.turbo",
            "--no-flash-attn",
            # `--processors 1` (single chunk processed sequentially):
            # whisper.cpp issue #2036 reports that `--processors > 1`
            # corrupts token timestamps because each parallel chunk
            # restarts its timestamp counter from zero. We rely on those
            # timestamps for ad cutting, so misaligned timestamps mean
            # we cut the wrong audio. Combined with whisper.cpp issue
            # #2521 (Metal crashes with `--processors > 8`), keeping
            # this at 1 is both safer and produces correct output.
            "--threads", cores, "--processors", "1",
        ],
        stdout=log_fd, stderr=log_fd, start_new_session=True,
    )
    ok = _wait_until(
        lambda: _http_ok("http://localhost:8765/health"), timeout=60,
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
    """Stop MinusPod, escalating SIGTERM -> SIGKILL.

    The Flask dev server we launch (`python -m flask run`) doesn't always
    exit on SIGTERM — its signal handler relies on the click runner being
    in the foreground, and `start_services.sh` daemonises it inside a
    subshell so its parent (the subshell) is already gone when we try to
    stop it. Concretely: PPID is reparented to launchd (1) and the process
    group leader has exited, so SIGTERM is silently absorbed.
    Escalate to SIGKILL after a short grace period.
    """
    pid = _pid_listening(8000)
    if not pid:
        return {"ok": True, "note": "not running"}

    # First try graceful SIGTERM on the pid and (best-effort) its group.
    _kill_pid(pid, signal.SIGTERM)
    try:
        os.killpg(os.getpgid(pid), signal.SIGTERM)
    except Exception:
        pass

    # Give Flask up to 5s to exit cleanly.
    if _wait_until(lambda: _pid_listening(8000) is None, timeout=5):
        return {"ok": True}

    # Escalate: SIGKILL the listener pid and any sibling in the same group.
    _kill_pid(pid, signal.SIGKILL)
    try:
        os.killpg(os.getpgid(pid), signal.SIGKILL)
    except Exception:
        pass

    ok = _wait_until(lambda: _pid_listening(8000) is None, timeout=10)
    return {"ok": ok, "note": "killed (SIGTERM ignored)"} if ok else {"ok": False}


def update_minuspod() -> dict:
    """Pull the latest MinusPod from upstream and reapply local patches.

    Safe to call when MinusPod is NOT running (start_minuspod calls this
    automatically). When MinusPod is already running the update is skipped
    and the caller receives ``{"ok": True, "note": "already running"}``.  

    Returns a dict with keys:
        ok      – True on success or if already up-to-date / offline.
        updated – True if a new upstream commit was pulled.
        note    – Human-readable status message.
    """
    import logging
    log = logging.getLogger(__name__)
    if not MINUSPOD_DIR.exists():
        return {"ok": False, "updated": False, "note": "MinusPod directory not found"}

    def _run(*args, **kwargs):
        return subprocess.run(
            list(args), cwd=str(MINUSPOD_DIR),
            capture_output=True, text=True, timeout=60, **kwargs
        )

    try:
        # Fetch from upstream (tolerate offline)
        fetch = _run("git", "fetch", "origin", "--quiet")
        if fetch.returncode != 0:
            log.warning("MinusPod update: git fetch failed — offline? %s", fetch.stderr.strip())
            return {"ok": True, "updated": False, "note": "offline — skipped update check"}

        local_sha = _run("git", "rev-parse", "HEAD").stdout.strip()
        # Try main then master
        for branch in ("origin/main", "origin/master"):
            r = _run("git", "rev-parse", branch)
            if r.returncode == 0:
                remote_sha = r.stdout.strip()
                break
        else:
            return {"ok": True, "updated": False, "note": "could not resolve remote branch"}

        if local_sha == remote_sha:
            short = local_sha[:7]
            log.info("MinusPod already at latest (%s)", short)
            if local_sha == remote_sha:
                short = local_sha[:7]
                log.info("MinusPod already at pinned SHA (%s)", short)
                return {"ok": True, "updated": False, "note": f"already at pinned SHA ({short})"}

        # Don't pull latest — reset to the pinned SHA from setup_minuspod.sh.
        # This matches the contract in scripts/setup_minuspod.sh: the pinned
        # SHA is the known-working upstream that our patches were generated
        # against. Chasing origin/main can silently break patches.
        pin_sha = "d900bdd0622b89089247bafe6a5f9db87876233a"
        old_short = local_sha[:7]
        new_short = pin_sha[:7]
        log.info("MinusPod: resetting %s → pinned %s", old_short, new_short)

        _run("git", "fetch", "origin", pin_sha, "--quiet")
        _run("git", "reset", "--hard", pin_sha)
        _run("git", "clean", "-fd")

        # Reapply local patches on top of new upstream. The core patch
        # is applied first; additional additive patches (e.g. LLM cost
        # optimisations) are applied on top, each best-effort.
        for patch_path in [MINUSPOD_PATCH, *MINUSPOD_ADDITIONAL_PATCHES]:
            if not patch_path.exists():
                continue
            patch_r = _run("git", "apply", "--3way", str(patch_path))
            if patch_r.returncode == 0:
                log.info("MinusPod patch applied cleanly: %s", patch_path.name)
            else:
                log.warning(
                    "MinusPod patch did not apply cleanly: %s — manual merge needed.\n%s",
                    patch_path.name,
                    patch_r.stderr.strip(),
                )

        # Reinstall Python deps if requirements.txt changed
        venv_pip = MINUSPOD_DIR / "venv" / "bin" / "pip"
        req = MINUSPOD_DIR / "requirements.txt"
        if venv_pip.exists() and req.exists():
            pip_r = subprocess.run(
                [str(venv_pip), "install", "-r", str(req),
                 "--quiet", "--disable-pip-version-check"],
                capture_output=True, text=True, timeout=120,
            )
            if pip_r.returncode != 0:
                log.warning("MinusPod pip install failed: %s", pip_r.stderr.strip())
            else:
                log.info("MinusPod deps updated")

        return {"ok": True, "updated": True, "note": f"updated {old_short} → {new_short}"}

    except subprocess.TimeoutExpired:
        log.warning("MinusPod update timed out")
        return {"ok": False, "updated": False, "note": "update timed out"}
    except Exception as exc:
        log.warning("MinusPod update error: %s", exc)
        return {"ok": False, "updated": False, "note": str(exc)}


def start_minuspod() -> dict:
    if _pid_listening(8000):
        return {"ok": True, "note": "already running"}
    # Auto-update before starting so we always run the latest upstream + local patches.
    # BUT: skip the pull if the local MinusPod already has our local patches
    # applied (the patches include symbols that upstream doesn't, like
    # _large_window_range and LARGE_WINDOW_MIN_SECONDS_DEFAULT). Pulling latest
    # upstream overwrites them; users have to manually re-apply via
    # bash scripts/setup_minuspod.sh when they actually want an upgrade.
    config_path = MINUSPOD_DIR / "src" / "config.py"
    needs_update = True
    if config_path.exists():
        try:
            content = config_path.read_text()
            if "_large_window_range" in content and "LARGE_WINDOW_MIN_SECONDS_DEFAULT" in content:
                needs_update = False
        except Exception:
            pass
    if needs_update:
        update_result = update_minuspod()
        if update_result.get("updated"):
            import logging
            logging.getLogger(__name__).info("MinusPod updated: %s", update_result["note"])
    else:
        import logging
        logging.getLogger(__name__).info(
            "MinusPod already has local patches applied; skipping upstream pull"
        )
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
    # Re-read .env so an edit between UI launch and "Restart MinusPod" is
    # honoured. Credentials (POCKETCASTS_EMAIL/PASSWORD) are excluded because
    # they're sensitive and the user already has them set in their shell.
    overlaid = _reload_dotenv_into(env, exclude={"POCKETCASTS_EMAIL", "POCKETCASTS_PASSWORD"})
    if overlaid:
        log.debug("Reloaded %d keys from .env into MinusPod subprocess env", overlaid)
    _overlay_openrouter_from_keychain(env)
    env.update({
        "DATA_DIR": str(MINUSPOD_DIR / "data"),
        "DATA_PATH": str(MINUSPOD_DIR / "data"),
        "MINUSPOD_DATA_DIR": str(MINUSPOD_DIR / "data"),
        "LLM_PROVIDER": env.get("LLM_PROVIDER", "ollama"),
        "OPENAI_BASE_URL": env.get("OPENAI_BASE_URL", "http://localhost:11434/v1"),
        "OPENAI_API_KEY": env.get("OPENAI_API_KEY", "not-needed"),
        "OPENAI_MODEL": env.get("OPENAI_MODEL", "qwen3.5-addetect"),
        "OPENROUTER_API_KEY": env.get("OPENROUTER_API_KEY", ""),
        "OPENROUTER_PROVIDER_ORDER": env.get("OPENROUTER_PROVIDER_ORDER", ""),
        "OPENROUTER_ALLOW_FALLBACKS": env.get("OPENROUTER_ALLOW_FALLBACKS", ""),
        "OPENROUTER_PROVIDER_SORT": env.get("OPENROUTER_PROVIDER_SORT", ""),
        "OPENROUTER_PROVIDER_ONLY": env.get("OPENROUTER_PROVIDER_ONLY", ""),
        "OPENROUTER_PROVIDER_IGNORE": env.get("OPENROUTER_PROVIDER_IGNORE", ""),
        "ANTHROPIC_API_KEY": env.get("ANTHROPIC_API_KEY", ""),
        "AD_MERGE_GAP": env.get("AD_MERGE_GAP", "15.0"),
        "AD_START_PAD": env.get("AD_START_PAD", "0.5"),
        "AD_END_PAD": env.get("AD_END_PAD", "1.0"),
        "WHISPER_BACKEND": "openai-api",
        "WHISPER_API_BASE_URL": "http://localhost:8765/v1",
        "WHISPER_DEVICE": "cpu",
        "WHISPER_SKIP_PREPROCESS": "1",
        "HTTP_TIMEOUT_WHISPER": env.get("HTTP_TIMEOUT_WHISPER", "1800"),
        "API_CHUNK_DURATION_SECONDS": env.get("API_CHUNK_DURATION_SECONDS", "300"),
        "BASE_URL": "http://localhost:8000",
        "HF_HOME": str(MINUSPOD_DIR / "data" / ".cache"),
        "SKIP_VERIFICATION": "true",
        "WINDOW_SIZE_SECONDS": "600",
        "WINDOW_OVERLAP_SECONDS": "120",
        # Default to 8192 so a single-window ad list (10 hr episode on
        # LARGE_WINDOW_SECONDS=36000) fits in one response. The upstream
        # default of 4096 risks truncating long-form shows. Override via
        # AD_DETECTION_MAX_TOKENS in .env for smaller models/contexts.
        "AD_DETECTION_MAX_TOKENS": env.get("AD_DETECTION_MAX_TOKENS", "8192"),
        # Default to a single in-flight LLM request. The previous default of
        # 2 doubled the KV-cache footprint (~5GB per 16K context for Qwen3.5
        # 35B-MoE Q4). On a 36GB Mac that, plus Whisper Metal buffers,
        # plus the browser/IDE, is enough to push the system into swap and
        # trigger the GPU OOM panic the user hit. Override per-machine via
        # `OLLAMA_NUM_PARALLEL=2` in `.env` only if you know you have headroom.
        "LLM_TIMEOUT_LOCAL": env.get("LLM_TIMEOUT_LOCAL", "1200"),
        "OLLAMA_NUM_PARALLEL": env.get("OLLAMA_NUM_PARALLEL", "1"),
        # Never keep more than one model resident. Ollama's default is 3,
        # which means switching detection ↔ verification ↔ chapters models
        # silently piles ~50GB onto the GPU.
        "OLLAMA_MAX_LOADED_MODELS": env.get("OLLAMA_MAX_LOADED_MODELS", "1"),
        # Tell ollama to evict idle models quickly. Long keep-alives are
        # the second contributor to "fans never quiet down" reports.
        "OLLAMA_KEEP_ALIVE": env.get("OLLAMA_KEEP_ALIVE", "30s"),
        # LLM cost-optimisation tunables (see README "LLM cost optimisations"
        # section). These flow through to MinusPod's get_stage_tunable resolver
        # as the env > DB > default source. Explicit passthroughs (rather than
        # relying on os.environ.copy()) so they're visible in the process listing
        # and so the log line below confirms what MinusPod actually sees.
        "LARGE_WINDOW_SECONDS":          env.get("LARGE_WINDOW_SECONDS", ""),
        "LARGE_WINDOW_MIN_SECONDS":      env.get("LARGE_WINDOW_MIN_SECONDS", ""),
        "LARGE_WINDOW_MAX_SECONDS":      env.get("LARGE_WINDOW_MAX_SECONDS", ""),
        "SKIP_VERIFICATION_UNDER_SECONDS": env.get("SKIP_VERIFICATION_UNDER_SECONDS", "0"),
        "ENABLE_PROMPT_CACHING":         env.get("ENABLE_PROMPT_CACHING", ""),
        "PYTHONPATH": ".",
    })
    log.info(
        "Starting MinusPod with cost-optimisation tunables: "
        "LARGE_WINDOW_SECONDS=%r SKIP_VERIFICATION_UNDER_SECONDS=%r "
        "ENABLE_PROMPT_CACHING=%r "
        "(WINDOW_SIZE_SECONDS=%r WINDOW_OVERLAP_SECONDS=%r)",
        env.get("LARGE_WINDOW_SECONDS") or "<unset>",
        env.get("SKIP_VERIFICATION_UNDER_SECONDS") or "<unset>",
        env.get("ENABLE_PROMPT_CACHING") or "<unset>",
        env.get("WINDOW_SIZE_SECONDS"),
        env.get("WINDOW_OVERLAP_SECONDS"),
    )
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
        timeout=60,
    )
    if ok:
        try:
            sync_minuspod_model_from_env()
        except Exception:
            pass
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
            "http://localhost:8000/api/v1/settings", timeout=5,
        )
        if r.status_code == 200:
            j = r.json()
            # The setting payload returns entries as dictionary keys like
            # 'claudeModel': {'value': 'name', 'isDefault': bool}
            claude_model = j.get("claudeModel")
            if isinstance(claude_model, dict):
                return claude_model.get("value")
            return claude_model or j.get("model")
    except Exception:
        pass
    return None


def sync_minuspod_model_from_env() -> dict:
    """Push ``OPENAI_MODEL`` from the environment into MinusPod's settings DB.

    MinusPod persists the active model in SQLite. When switching from Ollama
    to OpenRouter (or changing cloud models), a stale name like ``qwen3:14b``
    is rejected by OpenRouter with HTTP 400 unless we sync the env var.
    """
    provider = os.environ.get("LLM_PROVIDER", "ollama")
    if provider == "ollama":
        return {"ok": True, "skipped": True, "reason": "ollama provider"}
    model = os.environ.get("OPENAI_MODEL", "").strip()
    if not model:
        return {"ok": False, "error": "OPENAI_MODEL not set in environment"}
    current = get_minuspod_model()
    if current == model:
        return {"ok": True, "unchanged": True, "model": model}
    result = set_minuspod_model(model)
    return {
        "ok": result.get("ok", False),
        "model": model,
        "previous": current,
    }


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
# LLM cost-optimisation tunables (large window, skip pass 2, prompt caching)
# ---------------------------------------------------------------------------

def get_minuspod_settings() -> dict | None:
    """Fetch the full /api/v1/settings payload from MinusPod, or None if
    the backend is unreachable. The parent UI only consumes the few
    fields it needs (`stageTunables` and `stageTunableDefaults`), so the
    whole dict is returned verbatim and the JS picks out the keys.
    """
    try:
        r = httpx.get("http://localhost:8000/api/v1/settings", timeout=5)
        if r.status_code == 200:
            return r.json()
    except Exception:
        pass
    return None


def put_minuspod_stage_tunables(tunables: dict) -> dict:
    """Push a subset of the stage-tunables payload to MinusPod.

    The parent UI only manages the three cost-optimisation levers
    (``largeWindowSeconds``, ``skipVerificationUnderSeconds``,
    ``enablePromptCaching``). To avoid overwriting other tunables the
    caller may have changed locally (e.g. via MinusPod's own UI or
    another instance of this dashboard), we first GET the current
    ``stageTunables`` and merge the supplied subset on top before
    PUTting the full object back to ``/api/v1/settings/ad-detection``.
    Unknown keys in ``tunables`` are ignored.
    """
    if not isinstance(tunables, dict):
        raise ServiceError("tunables must be an object")
    # Allow only the cost-opt keys through; reject obvious typos early so
    # the JS gets a 400 with a useful message instead of a 200 that did
    # nothing.
    allowed = {
        "largeWindowSeconds",
        "skipVerificationUnderSeconds",
        "enablePromptCaching",
    }
    unknown = set(tunables) - allowed
    if unknown:
        raise ServiceError(
            f"Unknown tunable(s): {', '.join(sorted(unknown))}. "
            f"Allowed: {', '.join(sorted(allowed))}"
        )

    current = get_minuspod_settings() or {}
    existing = current.get("stageTunables") or {}
    merged = dict(existing)
    # Strip the wrapper shape ({value, isDefault, envOverride}) so we
    # only forward the bare values the PUT endpoint expects.
    for key, value in existing.items():
        if isinstance(value, dict) and "value" in value:
            merged[key] = value["value"]
    for key, value in tunables.items():
        merged[key] = value

    try:
        r = httpx.put(
            "http://localhost:8000/api/v1/settings/ad-detection",
            json=merged, timeout=10,
        )
    except Exception as e:
        raise ServiceError(f"MinusPod settings update failed: {e}")
    if r.status_code >= 400:
        # MinusPod's handler returns JSON: {"ok": false, "error": "..."}
        # for cross-field validation failures; surface that text instead
        # of the bare status code.
        try:
            detail = r.json().get("error") or r.text
        except Exception:
            detail = r.text
        raise ServiceError(
            f"MinusPod rejected update (HTTP {r.status_code}): {detail}"
        )
    return {"ok": True, "status_code": r.status_code, "updated": list(tunables)}


# ---------------------------------------------------------------------------
# Batch control & Background pulling
# ---------------------------------------------------------------------------

_pull_progress: dict[str, dict] = {}
_pull_lock = threading.Lock()

def start_all_services(whisper_backend: str = "native") -> dict:
    results = {}
    # 1. Start Ollama first (required for model check) only if using Ollama provider
    if os.environ.get("LLM_PROVIDER", "ollama") == "ollama":
        results["ollama"] = start_ollama()
    else:
        results["ollama"] = {"ok": True, "note": f"skipped (provider is {os.environ.get('LLM_PROVIDER')})"}
    # 2. Start Whisper
    results["whisper"] = start_whisper(backend=whisper_backend)
    # 3. Start MinusPod
    results["minuspod"] = start_minuspod()
    all_ok = all(svc.get("ok", False) for svc in results.values())
    return {"ok": all_ok, "results": results}

def stop_all_services() -> dict:
    results = {}
    # Stop in reverse order (MinusPod, then Whisper, then Ollama)
    results["minuspod"] = stop_minuspod()
    results["whisper"] = stop_whisper()
    results["ollama"] = stop_ollama()
    all_ok = all(svc.get("ok", False) for svc in results.values())
    return {"ok": all_ok, "results": results}

def pull_ollama_model(model_name: str) -> dict:
    if not model_name:
        raise ServiceError("model name required")
    
    # Make sure Ollama is running before starting the pull
    start_ollama()

    global _pull_progress
    with _pull_lock:
        if model_name in _pull_progress and _pull_progress[model_name]["status"] in ("downloading", "starting"):
            return {"ok": True, "note": "already pulling"}
        _pull_progress[model_name] = {
            "status": "starting",
            "completed": 0,
            "total": 0,
            "error": None
        }

    def _target():
        import json
        global _pull_progress
        try:
            with httpx.stream("POST", "http://localhost:11434/api/pull", json={"name": model_name}, timeout=3600) as r:
                if r.status_code != 200:
                    with _pull_lock:
                        _pull_progress[model_name] = {"status": "error", "error": f"Ollama returned {r.status_code}"}
                    return
                for line in r.iter_lines():
                    if not line:
                        continue
                    try:
                        data = json.loads(line)
                        status = data.get("status", "downloading")
                        completed = data.get("completed", 0)
                        total = data.get("total", 0)
                        with _pull_lock:
                            _pull_progress[model_name].update({
                                "status": status,
                                "completed": completed,
                                "total": total
                            })
                    except Exception:
                        pass
            with _pull_lock:
                _pull_progress[model_name]["status"] = "success"
        except Exception as e:
            with _pull_lock:
                _pull_progress[model_name] = {"status": "error", "error": str(e)}

    t = threading.Thread(target=_target, daemon=True)
    t.start()
    return {"ok": True}

def get_pull_progress(model_name: str) -> dict | None:
    with _pull_lock:
        return _pull_progress.get(model_name)



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


# ---------------------------------------------------------------------------
# Resource pressure / preflight
# ---------------------------------------------------------------------------

def get_memory_pressure() -> dict:
    """Return system memory state suitable for a preflight check.

    Exposes total/available memory dynamically and recommends the best local
    Ollama model based on system RAM. Supports macOS, Linux, and Windows.
    """
    import sys
    total_bytes = 0
    available_bytes = 0

    # 1. Try psutil first if available (cross-platform and highly reliable)
    try:
        import psutil
        vm = psutil.virtual_memory()
        total_bytes = vm.total
        available_bytes = vm.available
    except ImportError:
        # 2. Fallbacks if psutil is not installed
        if sys.platform == "darwin":
            try:
                out = subprocess.run(
                    ["sysctl", "-n", "hw.memsize"],
                    capture_output=True, text=True, timeout=3,
                ).stdout.strip()
                total_bytes = int(out)
            except Exception:
                pass

            page_size = 4096
            pages = {"free": 0, "inactive": 0, "speculative": 0, "purgeable": 0,
                     "wired": 0, "active": 0}
            try:
                vm = subprocess.run(
                    ["vm_stat"], capture_output=True, text=True, timeout=3,
                ).stdout
                m = _PAGE_SIZE_RE.search(vm)
                if m:
                    page_size = int(m.group(1))
                for line in vm.splitlines():
                    if ":" not in line:
                        continue
                    key, _, val = line.partition(":")
                    key = key.strip().lower()
                    val = val.strip().rstrip(".").replace(",", "")
                    try:
                        n = int(val)
                    except ValueError:
                        continue
                    if "pages free" in key:
                        pages["free"] = n
                    elif "pages inactive" in key:
                        pages["inactive"] = n
                    elif "pages speculative" in key:
                        pages["speculative"] = n
                    elif "pages purgeable" in key:
                        pages["purgeable"] = n
                    elif "pages wired" in key:
                        pages["wired"] = n
                    elif "pages active" in key:
                        pages["active"] = n
                available_bytes = (
                    pages["free"] + pages["inactive"]
                    + pages["speculative"] + pages["purgeable"]
                ) * page_size
            except Exception:
                pass
        elif sys.platform.startswith("linux"):
            try:
                # Read /proc/meminfo directly
                with open("/proc/meminfo", "r") as f:
                    for line in f:
                        if line.startswith("MemTotal:"):
                            total_bytes = int(line.split()[1]) * 1024
                        elif line.startswith("MemAvailable:"):
                            available_bytes = int(line.split()[1]) * 1024
            except Exception:
                pass
        elif sys.platform == "win32":
            try:
                out = subprocess.run(
                    ["wmic", "OS", "get", "TotalVisibleMemorySize,FreePhysicalMemory", "/Value"],
                    capture_output=True, text=True, timeout=3
                ).stdout
                for line in out.splitlines():
                    if "=" in line:
                        k, v = line.split("=", 1)
                        if k.strip() == "TotalVisibleMemorySize":
                            total_bytes = int(v.strip()) * 1024
                        elif k.strip() == "FreePhysicalMemory":
                            available_bytes = int(v.strip()) * 1024
            except Exception:
                pass

    ollama_loaded_bytes = 0
    try:
        r = httpx.get("http://localhost:11434/api/ps", timeout=3)
        if r.status_code == 200:
            for m in r.json().get("models", []):
                ollama_loaded_bytes += int(m.get("size_vram") or m.get("size") or 0)
    except Exception:
        pass

    total_gb = total_bytes / (1024**3) if total_bytes else 0.0
    available_gb = available_bytes / (1024**3) if available_bytes else 0.0
    loaded_gb = ollama_loaded_bytes / (1024**3) if ollama_loaded_bytes else 0.0

    # Dynamic model recommendation based on overall hardware capability (Total RAM)
    # Recommends lightweight model to save RAM and avoid swap-thrashing unless high RAM is present.
    recommended_model = "qwen3.5-addetect"
    if total_gb >= 24.0:
        recommended_model = "qwen3:14b"

    warning = None
    if total_gb and available_gb and (available_gb < 8.0):
        warning = (
            f"Only {available_gb:.1f} GB free of {total_gb:.0f} GB. "
            "Running a large LLM + Whisper now risks swap thrashing. "
            "Close memory-heavy apps (browsers, IDEs, Docker) or switch "
            f"to a smaller model (e.g. {recommended_model})."
        )

    return {
        "total_gb": round(total_gb, 1),
        "available_gb": round(available_gb, 1),
        "ollama_loaded_gb": round(loaded_gb, 1),
        "warning": warning,
        "recommended_model": recommended_model,
    }


import re as _re  # noqa: E402  -- keep regex local to this helper
_PAGE_SIZE_RE = _re.compile(r"page size of (\d+) bytes")

"""One command to run everything: `uv run start`.

Rebuilds the stand-in data, starts the five stand-ins, waits for each to be healthy,
starts Core last, and stops all six cleanly on Ctrl-C.
"""

import argparse
import os
import signal
import socket
import subprocess
import sys
import time

import httpx

from limitara.clock import clock_from_env, ist_date
from limitara.db import data_dir, reset
from limitara.ports import STANDINS, core_port, port_of, url_of
from limitara.seed import seed_all

HEALTH_TIMEOUT_SECONDS = 25


def port_is_free(port: int) -> bool:
    with socket.socket() as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        return s.connect_ex(("127.0.0.1", port)) != 0


def spawn(module: str, port: int) -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, "-m", "uvicorn", f"{module}:create_app", "--factory", "--host", "127.0.0.1",
         "--port", str(port), "--log-level", "warning"],
        env=os.environ.copy(),
    )


def wait_healthy(name: str, url: str, proc: subprocess.Popen) -> None:
    deadline = time.time() + HEALTH_TIMEOUT_SECONDS
    while time.time() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"{name} exited early with code {proc.returncode}")
        try:
            if httpx.get(f"{url}/health", timeout=1).json().get("ok"):
                return
        except Exception:
            pass
        time.sleep(0.2)
    raise RuntimeError(f"{name} did not become healthy within {HEALTH_TIMEOUT_SECONDS}s")


def stop_all(procs: list[subprocess.Popen]) -> None:
    for p in procs:
        if p.poll() is None:
            p.terminate()
    deadline = time.time() + 5
    for p in procs:
        try:
            p.wait(timeout=max(0.1, deadline - time.time()))
        except subprocess.TimeoutExpired:
            p.kill()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Start the stand-ins and Core with fresh seed data")
    parser.add_argument("--no-core", action="store_true", help="start only the five stand-ins")
    parser.add_argument("--no-seed", action="store_true", help="keep existing data instead of re-seeding")
    args = parser.parse_args(argv)

    wanted = {name: port_of(name) for name in STANDINS}
    if not args.no_core:
        wanted["core"] = core_port()
    busy = [f"{n} (port {p})" for n, p in wanted.items() if not port_is_free(p)]
    if busy:
        print("These ports are already in use: " + ", ".join(busy) + ". Stop the old run and try again.")
        return 2

    if not args.no_seed:
        now = clock_from_env().now()
        seed_all(ist_date(now), now)
        reset(data_dir() / "core.db")
        print("seeded fresh data for", ", ".join(STANDINS))

    procs: list[subprocess.Popen] = []
    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
    try:
        for name in STANDINS:
            procs.append(spawn(f"limitara.standins.{name}.app", port_of(name)))
        for name, proc in zip(STANDINS, procs):
            wait_healthy(name, url_of(name), proc)
            print(f"  {name:<11} ready on {url_of(name)}")
        if not args.no_core:
            procs.append(spawn("limitara.core.app", core_port()))
            wait_healthy("core", f"http://127.0.0.1:{core_port()}", procs[-1])
            print(f"\nCore ready: http://127.0.0.1:{core_port()}/   (sign in, then open the approvals, rep and customer pages)")
        print("Press Ctrl-C to stop everything.")
        while True:
            for name, p in zip(list(wanted), procs):
                if p.poll() is not None:
                    raise RuntimeError(f"{name} stopped unexpectedly (code {p.returncode})")
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nstopping...")
    except RuntimeError as exc:
        print(f"error: {exc}")
        return 1
    finally:
        stop_all(procs)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

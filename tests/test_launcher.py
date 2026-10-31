import os
import signal
import subprocess
import sys
import time

import httpx

from limitara.launcher import port_is_free


def test_one_command_starts_all_stand_ins_and_ctrl_c_leaves_no_orphans(tmp_path):
    base = 47100
    env = {**os.environ, "LIMITARA_DATA_DIR": str(tmp_path), "LIMITARA_PORT_BASE": str(base)}
    proc = subprocess.Popen([sys.executable, "-m", "limitara.launcher", "--no-core"], env=env,
                            stdout=subprocess.PIPE, text=True)
    try:
        deadline = time.time() + 30
        healthy = set()
        while time.time() < deadline and len(healthy) < 5:
            for i in range(5):
                try:
                    if httpx.get(f"http://127.0.0.1:{base + i}/health", timeout=0.5).json()["ok"]:
                        healthy.add(i)
                except Exception:
                    pass
            time.sleep(0.3)
        assert len(healthy) == 5
        assert "mailbox" in httpx.get(f"http://127.0.0.1:{base}/health").text
    finally:
        proc.send_signal(signal.SIGINT)
        proc.wait(timeout=15)
    time.sleep(0.5)
    assert all(port_is_free(base + i) for i in range(5))


def test_a_busy_port_is_reported_instead_of_hanging(tmp_path):
    import socket

    base = 47200
    blocker = socket.socket()
    blocker.bind(("127.0.0.1", base + 2))
    blocker.listen()
    env = {**os.environ, "LIMITARA_DATA_DIR": str(tmp_path), "LIMITARA_PORT_BASE": str(base)}
    try:
        out = subprocess.run([sys.executable, "-m", "limitara.launcher", "--no-core"], env=env,
                             capture_output=True, text=True, timeout=20)
        assert out.returncode == 2 and "already in use" in out.stdout
    finally:
        blocker.close()

"""Foreground bridge lifecycle and systemd readiness, using only the standard library."""

import asyncio
import os
import signal
import socket
import subprocess
import sys
import time


def notify_ready():
    """Notify the manager from the main process; manual launches need no manager."""
    address = os.environ.get("NOTIFY_SOCKET")
    if not address:
        return
    # systemd writes abstract Unix socket addresses with a leading @.
    if address.startswith("@"):
        address = "\0" + address[1:]
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as channel:
        channel.sendto(b"READY=1", address)


def supervise(command, workspace, child_env, ready):
    """Own the child until stopped; even a spontaneous exit 0 is a service failure."""
    stopping = False
    process = None

    def stop(_signum, _frame):
        nonlocal stopping
        stopping = True

    def signal_group(signum):
        if process is not None:
            try:
                os.killpg(process.pid, signum)
            except (ProcessLookupError, PermissionError):
                # macOS answers EPERM for a group whose members are all zombies.
                pass

    handlers = {sig: signal.signal(sig, stop) for sig in (signal.SIGTERM, signal.SIGINT)}
    try:
        process = subprocess.Popen(
            command, cwd=workspace, env=child_env, stdin=subprocess.DEVNULL, start_new_session=True
        )
        deadline = time.monotonic() + 40
        notified = not os.environ.get("NOTIFY_SOCKET")
        while process.poll() is None and not stopping:
            if not notified:
                if asyncio.run(ready()):
                    if process.poll() is not None or stopping:
                        break
                    notify_ready()
                    notified = True
                elif time.monotonic() >= deadline:
                    print("Umay köprüsü: açılış denetimi zaman aşımına uğradı.", file=sys.stderr)
                    return 1
            time.sleep(0.1)
        if stopping:
            return 0
        print(
            f"Umay köprüsü beklenmedik biçimde durdu (çıkış {process.returncode}).",
            file=sys.stderr,
        )
        return process.returncode if process.returncode > 0 else 1
    finally:
        if process is not None:
            signal_group(signal.SIGTERM)
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
            finally:
                signal_group(signal.SIGKILL)
                process.wait()
        for sig, handler in handlers.items():
            signal.signal(sig, handler)


def occupied(host, port):
    """Whether any process already accepts connections on the bridge address."""
    try:
        with socket.create_connection((host.strip("[]"), port), timeout=1):
            return True
    except OSError:
        return False

"""SSH / host command library with a complete audit trail.

Connection settings are read by the library itself (environment variables or
the env file, see ``audit_common.get_setting``) so the password never appears
as a Robot keyword argument and therefore never ends up in ``log.html`` or
``output.xml``.

Settings
--------
``JETSON_HOST``              required, IP / hostname of the target
``JETSON_USER``              default ``root``
``JETSON_PASSWORD``          may be empty / unset (empty-password login)
``JETSON_PORT``              default ``22``
``JETSON_KEY_FILE``          optional private key (tried before the password)
``JETSON_KEY_PASSPHRASE``    optional passphrase for the key
``JETSON_CONNECT_TIMEOUT``   default ``15`` seconds
``JETSON_POWER_CYCLE_CMD``   optional host command that power-cycles the device
``JETSON_POWER_CYCLE_AFTER`` seconds to wait for a boot before running it (300)

Every command (target and host) is recorded in the per-test ``commands.log``
and in the run-wide ``audit/commands.jsonl`` together with exit code, stdout,
stderr, duration and timestamps.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import socket
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import paramiko
from robot.api import logger

import audit_common as ac

# Make sbin directories available regardless of how sshd sets up PATH, and
# force the C locale so output parsing is stable.
_REMOTE_PREFIX = (
    'PATH="$PATH:/usr/local/sbin:/usr/local/bin:/usr/sbin:/sbin"; export PATH; '
    "LC_ALL=C; export LC_ALL; "
)


def _say(message: str, level: str = "INFO") -> None:
    """Log *message* to log.html and the console so long waits are never silent."""
    stamped = f"[{datetime.now().strftime('%H:%M:%S')}] {message}"
    if level == "WARN":
        logger.warn(stamped)  # warnings are always echoed to the console
    else:
        logger.info(stamped, also_console=True)


def _short(command: str, limit: int = 100) -> str:
    one_line = " ".join(command.split())
    return one_line if len(one_line) <= limit else one_line[: limit - 3] + "..."


# Commands allowed to run this long (seconds) announce themselves and report progress on the console.
_LONG_COMMAND_S = 120
_HEARTBEAT_S = 60


@dataclass
class CommandResult:
    command: str
    rc: int
    stdout: str
    stderr: str
    duration: float
    where: str = "target"

    def __str__(self) -> str:  # shown in log.html when returned
        return f"rc={self.rc}\n{self.stdout}"


class JetsonTarget:
    """Keywords to drive the Jetson over SSH and to run commands on the host."""

    ROBOT_LIBRARY_SCOPE = "GLOBAL"
    ROBOT_LIBRARY_VERSION = "1.0"

    def __init__(self) -> None:
        self._transport: paramiko.Transport | None = None
        self._hash_cache: dict[tuple[str, int, int], str] = {}

    # ------------------------------------------------------------------ config
    @staticmethod
    def _setting(name: str, default: str | None = None) -> str | None:
        return ac.get_setting(name, default)

    def _conn_params(self) -> dict:
        host = self._setting("JETSON_HOST")
        if not host:
            raise AssertionError(
                "JETSON_HOST is not set. Provide it via the environment or the env file "
                f"({ac.env_file_path()}); see env.example."
            )
        return {
            "host": host,
            "port": int(self._setting("JETSON_PORT", "22") or "22"),
            "user": self._setting("JETSON_USER", "root") or "root",
            # An empty or unset password means "empty password" login.
            "password": self._setting("JETSON_PASSWORD", "") or "",
            "key_file": self._setting("JETSON_KEY_FILE", "") or "",
            "key_passphrase": self._setting("JETSON_KEY_PASSPHRASE", "") or "",
            "timeout": float(self._setting("JETSON_CONNECT_TIMEOUT", "15") or "15"),
        }

    def get_target_settings(self) -> dict:
        """Connection settings without any secret (for logging / manifests)."""
        p = self._conn_params()
        return {
            "host": p["host"],
            "port": p["port"],
            "user": p["user"],
            "password_set": bool(p["password"]),
            "key_file": p["key_file"],
        }

    # -------------------------------------------------------------- connection
    def connect_to_target(self, retries: int = 3, retry_delay: float = 5.0) -> None:
        """Open the SSH connection (key, password, then empty-password/none auth)."""
        last: Exception | None = None
        for attempt in range(1, int(retries) + 1):
            try:
                self._connect_once()
                return
            except Exception as exc:  # noqa: BLE001 - reported below
                last = exc
                _say(
                    f"SSH connect attempt {attempt}/{retries} failed: {exc}"
                    + (f" - retrying in {retry_delay:.0f}s" if attempt < retries else "")
                )
                if attempt < retries:
                    time.sleep(retry_delay)
        raise AssertionError(f"Cannot connect to target after {retries} attempt(s): {last}")

    def disconnect_from_target(self) -> None:
        self._drop_connection()

    def _drop_connection(self) -> None:
        if self._transport is not None:
            try:
                self._transport.close()
            except Exception:  # noqa: BLE001
                pass
        self._transport = None

    def _connect_once(self) -> paramiko.Transport:
        p = self._conn_params()
        self._drop_connection()
        sock = socket.create_connection((p["host"], p["port"]), timeout=p["timeout"])
        transport = paramiko.Transport(sock)
        transport.banner_timeout = p["timeout"]
        try:
            transport.start_client(timeout=p["timeout"])
            methods: list[str] = []
            if p["key_file"] and self._auth_key(transport, p):
                methods.append("publickey")
            if not transport.is_authenticated():
                try:
                    transport.auth_password(p["user"], p["password"])
                    methods.append("password(empty)" if not p["password"] else "password")
                except paramiko.SSHException as exc:
                    logger.debug(f"password auth failed: {exc}")
            if not transport.is_authenticated() and not p["password"]:
                try:
                    transport.auth_none(p["user"])
                    methods.append("none")
                except paramiko.SSHException as exc:
                    logger.debug(f"none auth failed: {exc}")
            if not transport.is_authenticated():
                raise paramiko.AuthenticationException(
                    f"Authentication failed for {p['user']}@{p['host']} "
                    f"(password {'set' if p['password'] else 'empty'}, "
                    f"key {'configured' if p['key_file'] else 'not configured'})"
                )
        except Exception:
            transport.close()
            raise
        transport.set_keepalive(30)
        self._transport = transport
        key = transport.get_remote_server_key()
        fp = "SHA256:" + base64.b64encode(hashlib.sha256(key.asbytes()).digest()).decode().rstrip("=")
        self._audit_event(
            {
                "kind": "connect",
                "target": f"{p['user']}@{p['host']}:{p['port']}",
                "auth": methods,
                "host_key": f"{key.get_name()} {fp}",
            }
        )
        logger.info(f"Connected to {p['user']}@{p['host']}:{p['port']} ({methods[-1]}), host key {fp}")
        return transport

    @staticmethod
    def _auth_key(transport: paramiko.Transport, p: dict) -> bool:
        for cls in (paramiko.Ed25519Key, paramiko.ECDSAKey, paramiko.RSAKey):
            try:
                pkey = cls.from_private_key_file(p["key_file"], password=p["key_passphrase"] or None)
            except (paramiko.SSHException, ValueError):
                continue
            try:
                transport.auth_publickey(p["user"], pkey)
                return True
            except paramiko.SSHException as exc:
                logger.debug(f"public key auth failed: {exc}")
        return False

    def _ensure_transport(self) -> paramiko.Transport:
        t = self._transport
        if t is not None and t.is_active() and t.is_authenticated():
            return t
        logger.info("SSH connection is not active, reconnecting")
        return self._connect_once()

    # -------------------------------------------------------- command running
    def run_on_target(
        self, command: str, timeout: float = 120, expected_rc: int | str | None = 0
    ) -> CommandResult:
        """Run *command* on the target and return an object with rc/stdout/stderr.

        Fails when the exit code differs from ``expected_rc``; use ``any`` to
        accept every exit code.
        """
        started = time.monotonic()
        t0 = _utcnow()
        if float(timeout) >= _LONG_COMMAND_S:
            _say(f"Running on target (timeout {float(timeout):.0f}s): {_short(command)}")
        rc, out, err, timed_out = self._exec(command, float(timeout))
        result = CommandResult(command, rc, out, err, time.monotonic() - started, "target")
        self._record(result, t0, timed_out)
        if timed_out:
            _say(f"TIMEOUT: target command did not finish within {float(timeout):.0f}s: {_short(command)}", "WARN")
            raise AssertionError(f"Target command timed out after {timeout}s: {command}")
        self._check_rc(result, expected_rc)
        return result

    def run_on_host(
        self,
        command: str,
        timeout: float = 600,
        expected_rc: int | str | None = 0,
        cwd: str | None = None,
    ) -> CommandResult:
        """Run a shell command on the machine executing the tests."""
        env = {k: v for k, v in os.environ.items() if k not in ("JETSON_PASSWORD", "JETSON_KEY_PASSPHRASE")}
        if float(timeout) >= _LONG_COMMAND_S:
            _say(f"Running on host (timeout {float(timeout):.0f}s): {_short(command)}")
        started = time.monotonic()
        t0 = _utcnow()
        timed_out = False
        try:
            proc = subprocess.run(  # noqa: S602 - test-author controlled command
                command, shell=True, executable="/bin/bash", cwd=cwd, env=env,
                capture_output=True, timeout=float(timeout), check=False,
            )
            rc, out, err = proc.returncode, proc.stdout, proc.stderr
        except subprocess.TimeoutExpired as exc:
            rc, out, err, timed_out = -1, exc.stdout or b"", exc.stderr or b"", True
        result = CommandResult(
            command, rc, out.decode("utf-8", "replace").rstrip("\r\n"),
            err.decode("utf-8", "replace").rstrip("\r\n"), time.monotonic() - started, "host",
        )
        self._record(result, t0, timed_out)
        if timed_out:
            _say(f"TIMEOUT: host command did not finish within {float(timeout):.0f}s: {_short(command)}", "WARN")
            raise AssertionError(f"Host command timed out after {timeout}s: {command}")
        self._check_rc(result, expected_rc)
        return result

    def target_has_command(self, name: str) -> bool:
        return self.run_on_target(f"command -v {name}", timeout=20, expected_rc="any").rc == 0

    def host_has_command(self, name: str) -> bool:
        return shutil.which(name) is not None

    def _exec(self, command: str, timeout: float) -> tuple[int, str, str, bool]:
        transport = self._ensure_transport()
        chan = transport.open_session(timeout=30)
        chan.exec_command(_REMOTE_PREFIX + command)
        try:
            chan.shutdown_write()
        except Exception:  # noqa: BLE001
            pass
        out, err = bytearray(), bytearray()
        begin = time.monotonic()
        deadline = begin + timeout
        next_notice = begin + _HEARTBEAT_S
        timed_out = False
        while True:
            if timeout >= _LONG_COMMAND_S and time.monotonic() >= next_notice:
                next_notice += _HEARTBEAT_S
                _say(
                    f"Still waiting for target command ({time.monotonic() - begin:.0f}s of max {timeout:.0f}s): "
                    f"{_short(command, 70)}"
                )
            got = False
            while chan.recv_ready():
                out += chan.recv(65536)
                got = True
            while chan.recv_stderr_ready():
                err += chan.recv_stderr(65536)
                got = True
            if chan.exit_status_ready() and not got:
                time.sleep(0.1)  # grace period for trailing data
                if not chan.recv_ready() and not chan.recv_stderr_ready():
                    break
                continue
            if time.monotonic() > deadline:
                timed_out = True
                break
            if not got:
                time.sleep(0.05)
        rc = -1 if timed_out else chan.recv_exit_status()
        chan.close()
        return (
            rc,
            out.decode("utf-8", "replace").rstrip("\r\n"),
            err.decode("utf-8", "replace").rstrip("\r\n"),
            timed_out,
        )

    @staticmethod
    def _check_rc(result: CommandResult, expected_rc: int | str | None) -> None:
        if expected_rc is None or str(expected_rc).lower() in ("any", "none"):
            return
        if result.rc != int(expected_rc):
            raise AssertionError(
                f"{result.where} command exited with rc={result.rc}, expected {expected_rc}: "
                f"{result.command}\n--- stdout ---\n{_tail(result.stdout)}\n--- stderr ---\n{_tail(result.stderr)}"
            )

    # ------------------------------------------------------------------ files
    def upload_file_to_target(self, local_path: str, remote_path: str, verify: bool = True) -> str:
        """Upload a file via SFTP, verify its SHA-256 and return the checksum."""
        local = Path(local_path)
        if not local.is_file():
            raise AssertionError(f"Local file does not exist: {local}")
        size = local.stat().st_size
        digest = self.file_sha256(str(local))
        transport = self._ensure_transport()
        sftp = paramiko.SFTPClient.from_transport(transport)
        state = {"next": 10}
        started = time.monotonic()

        def progress(done: int, total: int) -> None:
            pct = int(done * 100 / total) if total else 100
            if pct >= state["next"]:
                state["next"] = pct + 10
                logger.info(f"upload {local.name}: {pct}%", also_console=size > 50 * 1024 * 1024)

        try:
            sftp.put(str(local), remote_path, callback=progress)
        finally:
            sftp.close()
        elapsed = time.monotonic() - started
        self._audit_event(
            {"kind": "upload", "local": str(local), "remote": remote_path, "bytes": size,
             "sha256": digest, "seconds": round(elapsed, 2)}
        )
        if verify:
            res = self.run_on_target(f"sha256sum {_q(remote_path)}", timeout=600)
            remote_digest = res.stdout.split()[0] if res.stdout else ""
            if remote_digest != digest:
                raise AssertionError(
                    f"Upload verification failed for {remote_path}: local {digest}, target {remote_digest}"
                )
        logger.info(f"Uploaded {local} -> {remote_path} ({size} bytes, sha256 {digest}) in {elapsed:.1f}s")
        return digest

    def download_file_from_target(self, remote_path: str, name: str | None = None) -> str:
        """Download a (small) file into the test's artifact directory; returns its path."""
        dest = ac.current_artifact_dir() / (name or Path(remote_path).name)
        sftp = paramiko.SFTPClient.from_transport(self._ensure_transport())
        try:
            sftp.get(remote_path, str(dest))
        finally:
            sftp.close()
        digest = ac.sha256_file(dest)
        self._audit_event({"kind": "download", "remote": remote_path, "local": str(dest), "sha256": digest})
        logger.info(f"Downloaded {remote_path} -> {dest} (sha256 {digest})")
        return str(dest)

    def save_artifact(self, name: str, content: str) -> str:
        """Write *content* into the test's artifact directory and return the path."""
        dest = ac.current_artifact_dir() / name
        dest.write_text(content if content.endswith("\n") else content + "\n", encoding="utf-8")
        logger.info(f"Saved artifact {dest}")
        return str(dest)

    def archive_host_file(self, path: str, name: str | None = None) -> str:
        """Copy a (small) host file into the test's artifact directory."""
        src = Path(path)
        dest = ac.current_artifact_dir() / (name or src.name)
        shutil.copyfile(src, dest)
        logger.info(f"Archived {src} -> {dest} (sha256 {ac.sha256_file(dest)})")
        return str(dest)

    def file_sha256(self, path: str) -> str:
        """SHA-256 of a host file (cached per path/size/mtime)."""
        st = os.stat(path)
        key = (os.path.realpath(path), st.st_size, int(st.st_mtime_ns))
        if key not in self._hash_cache:
            self._hash_cache[key] = ac.sha256_file(path)
        return self._hash_cache[key]

    def get_test_artifact_dir(self) -> str:
        return str(ac.current_artifact_dir())

    # ----------------------------------------------------------------- reboot
    def get_boot_id(self) -> str:
        return self.run_on_target("cat /proc/sys/kernel/random/boot_id", timeout=20).stdout.strip()

    def reboot_target_and_reconnect(
        self, method: str = "soft", timeout: float = 900, settle: float = 20
    ) -> float:
        """Reboot (``soft``: systemctl reboot, ``sysrq``: immediate reset) and reconnect.

        Returns the seconds until SSH was available again with a new boot id.
        If ``JETSON_POWER_CYCLE_CMD`` is configured and no boot happened within
        ``JETSON_POWER_CYCLE_AFTER`` seconds, the command is run once.
        """
        commands = {
            "soft": "systemctl reboot || reboot",
            "sysrq": "echo 1 > /proc/sys/kernel/sysrq; sync; echo b > /proc/sysrq-trigger",
        }
        if method not in commands:
            raise AssertionError(f"Unknown reboot method '{method}', use one of {sorted(commands)}")
        old_boot_id = self.get_boot_id()
        started = time.monotonic()
        _say(f"Rebooting target ({method}); will wait up to {float(timeout):.0f}s for it to come back")
        try:
            self.run_on_target(commands[method], timeout=20, expected_rc="any")
        except Exception as exc:  # noqa: BLE001 - connection loss is expected
            logger.info(f"Connection dropped while rebooting (expected): {exc}")
        self._drop_connection()
        _say(f"Reboot command sent, waiting {float(settle):.0f}s before polling SSH")
        time.sleep(float(settle))
        self.wait_for_target(timeout=float(timeout) - (time.monotonic() - started), previous_boot_id=old_boot_id)
        return time.monotonic() - started

    def wait_for_target(self, timeout: float = 900, previous_boot_id: str | None = None) -> None:
        """Wait until SSH works (and, if given, the boot id differs from *previous_boot_id*)."""
        deadline = time.monotonic() + float(timeout)
        start = time.monotonic()
        cycle_cmd = self._setting("JETSON_POWER_CYCLE_CMD", "") or ""
        cycle_after = float(self._setting("JETSON_POWER_CYCLE_AFTER", "300") or "300")
        cycled = False
        notice_every = 30.0
        next_notice = start + notice_every
        last_state = "no SSH connection yet"
        if cycle_cmd:
            cycle_info = f"power-cycle command runs after {cycle_after:.0f}s"
        else:
            cycle_info = "no JETSON_POWER_CYCLE_CMD set, a hung device must be reset manually"
        _say(f"Waiting for target to come back (timeout {float(timeout):.0f}s; {cycle_info})")
        while time.monotonic() < deadline:
            try:
                self._connect_once()
                boot_id = self.get_boot_id()
                if previous_boot_id is None or boot_id != previous_boot_id:
                    _say(f"Target is back after {time.monotonic() - start:.0f}s (boot id {boot_id})")
                    return
                last_state = "still running the old boot (waiting for it to go down)"
                self._drop_connection()  # still the old system, wait for it to go down
            except Exception as exc:  # noqa: BLE001
                last_state = f"SSH not available ({type(exc).__name__}: {exc})"
                logger.debug(f"waiting for target: {exc}")
                self._drop_connection()
            now = time.monotonic()
            if cycle_cmd and not cycled and now - start > cycle_after:
                _say(f"Target did not return within {cycle_after:.0f}s, running power-cycle command", "WARN")
                self.run_on_host(cycle_cmd, timeout=120, expected_rc="any")
                cycled = True
                next_notice = time.monotonic()
            if now >= next_notice:
                next_notice = now + notice_every
                _say(
                    f"Still waiting for target: {now - start:.0f}s elapsed, "
                    f"{max(deadline - now, 0):.0f}s left; {last_state}"
                )
            time.sleep(5)
        hint = "" if cycle_cmd else " (set JETSON_POWER_CYCLE_CMD to automate power-cycling a hung device)"
        _say(
            f"TIMEOUT: target did not come back within {float(timeout):.0f}s; last state: {last_state}{hint}",
            "WARN",
        )
        raise AssertionError(f"Target did not come back within {timeout:.0f}s{hint}")

    # ------------------------------------------------------------------ audit
    def _record(self, result: CommandResult, started: str, timed_out: bool) -> None:
        status = "TIMEOUT" if timed_out else f"rc={result.rc}"
        logger.info(
            f"[{result.where}] $ {result.command}\n[{status}, {result.duration:.2f}s]\n"
            f"{_tail(result.stdout, 4000)}" + (f"\n--- stderr ---\n{_tail(result.stderr, 2000)}" if result.stderr else "")
        )
        entry = {
            "kind": "command", "where": result.where, "start": started, "end": _utcnow(),
            "duration_s": round(result.duration, 3), "command": result.command,
            "rc": None if timed_out else result.rc, "timed_out": timed_out,
            "stdout": result.stdout, "stderr": result.stderr,
        }
        try:
            tdir = ac.current_artifact_dir()
            with open(tdir / "commands.log", "a", encoding="utf-8") as fh:
                fh.write(
                    f"=== {started} [{result.where}] {status} {result.duration:.2f}s\n$ {result.command}\n"
                    f"--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}\n\n"
                )
            entry["artifact_dir"] = tdir.name
        except Exception:  # noqa: BLE001 - not running inside Robot
            pass
        self._append_jsonl(entry)

    def _audit_event(self, event: dict) -> None:
        event = {"time": _utcnow(), **event}
        try:
            event["artifact_dir"] = ac.current_artifact_dir().name
            with open(ac.current_artifact_dir() / "commands.log", "a", encoding="utf-8") as fh:
                fh.write(f"=== {event['time']} [{event['kind']}] {json.dumps({k: v for k, v in event.items() if k not in ('time', 'kind', 'artifact_dir')})}\n\n")
        except Exception:  # noqa: BLE001
            pass
        self._append_jsonl(event)

    @staticmethod
    def _append_jsonl(entry: dict) -> None:
        try:
            audit = ac.output_dir() / "audit"
            audit.mkdir(exist_ok=True)
            with open(audit / "commands.jsonl", "a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry) + "\n")
        except Exception:  # noqa: BLE001
            pass


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _q(value: str) -> str:
    import shlex

    return shlex.quote(value)


def _tail(text: str, limit: int = 3000) -> str:
    return text if len(text) <= limit else "...[truncated]...\n" + text[-limit:]

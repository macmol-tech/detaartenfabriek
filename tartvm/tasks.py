"""Task management for background operations."""
import asyncio
import json
import logging
import re
import secrets
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncGenerator, Dict, List, Optional, Set, Tuple

from .config import settings
from .models import TaskModel, TaskStatus, VMConfigModel, VMModel, VMStatus

logger = logging.getLogger(__name__)

# Regex pattern to match ANSI escape sequences
ANSI_ESCAPE_PATTERN = re.compile(r'\x1b\[[0-9;?]*[a-zA-Z]|\x1b\][^\x07]*\x07|\x1b[=>]|\x1b[@-_][0-?]*[ -/]*[@-~]')


def strip_ansi_codes(text: str) -> str:
    """Remove ANSI escape sequences from text."""
    return ANSI_ESCAPE_PATTERN.sub('', text)


class TartCommandError(RuntimeError):
    def __init__(
        self,
        message: str,
        args: List[str],
        rc: int,
        stdout: str,
        stderr: str,
        timed_out: bool = False,
    ):
        super().__init__(message)
        self.message = message
        self.args = args
        self.rc = rc
        self.stdout = stdout
        self.stderr = stderr
        self.timed_out = timed_out

    def __str__(self) -> str:
        return self.message


@dataclass
class TaskManager:
    """Manages background tasks for VM operations."""

    tasks: Dict[str, TaskModel] = field(default_factory=dict)
    _task_subscribers: Dict[str, Set[asyncio.Queue]] = field(default_factory=dict)
    _task_cleanup_task: Optional[asyncio.Task] = None

    inventory: Dict[str, VMModel] = field(default_factory=dict)
    inventory_last_refresh: Optional[float] = None
    _inventory_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    _refresh_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    _inventory_monitor_task: Optional[asyncio.Task] = None

    _ip_semaphore: asyncio.Semaphore = field(default_factory=lambda: asyncio.Semaphore(3))

    vm_config_cache: Dict[str, Tuple[float, VMConfigModel]] = field(default_factory=dict)
    _vm_config_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    
    async def create_task(self, action: str, **kwargs) -> TaskModel:
        """Create a new background task."""
        task = TaskModel(
            id=secrets.token_urlsafe(8),
            action=action,
            **kwargs
        )
        self.tasks[task.id] = task
        self._task_subscribers[task.id] = set()
        return task
    
    async def update_task(
        self,
        task_id: str,
        status: Optional[TaskStatus] = None,
        command: Optional[List[str]] = None,
        exit_code: Optional[int] = None,
        result: Optional[Dict] = None,
        error: Optional[str] = None,
        stderr: Optional[str] = None,
        log: Optional[str] = None
    ) -> Optional[TaskModel]:
        """Update a task's status and notify subscribers."""
        if task_id not in self.tasks:
            return None
            
        task = self.tasks[task_id]
        
        if status:
            task.status = status
        if command is not None:
            task.command = command
        if exit_code is not None:
            task.exit_code = exit_code
        if result is not None:
            task.result = result
        if error is not None:
            task.error = error
        if stderr is not None:
            task.stderr = stderr
        if log is not None:
            task.logs.append(log)
            # Keep only the last N log lines
            if len(task.logs) > settings.MAX_TASK_LOGS:
                task.logs = task.logs[-settings.MAX_TASK_LOGS:]
                
        task.updated_at = time.time()
        
        # Notify subscribers
        await self._notify_subscribers(task_id)
        
        return task
    
    async def get_task(self, task_id: str) -> Optional[TaskModel]:
        """Get a task by ID."""
        return self.tasks.get(task_id)
    
    async def subscribe_to_task(self, task_id: str) -> AsyncGenerator[TaskModel, None]:
        """Subscribe to task updates."""
        if task_id not in self._task_subscribers:
            self._task_subscribers[task_id] = set()
            
        queue: asyncio.Queue = asyncio.Queue()
        self._task_subscribers[task_id].add(queue)
        
        try:
            while True:
                task = await queue.get()
                if task is None:  # Sentinel value to signal end of updates
                    break
                yield task
        finally:
            self._task_subscribers[task_id].discard(queue)
    
    async def _notify_subscribers(self, task_id: str) -> None:
        """Notify all subscribers of a task update."""
        if task_id not in self._task_subscribers:
            return
            
        task = self.tasks[task_id]
        for queue in self._task_subscribers[task_id]:
            await queue.put(task)
    
    async def run_command(
        self,
        command: List[str],
        task_id: Optional[str] = None,
        cwd: Optional[str] = None,
        env: Optional[Dict[str, str]] = None,
        timeout_seconds: Optional[float] = None,
    ) -> Tuple[int, str, str]:
        """Run a command and stream stdout/stderr to task logs."""
        if task_id:
            await self.update_task(task_id, command=command)

        process = await asyncio.create_subprocess_exec(
            *command,
            cwd=cwd,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        stdout_lines: List[str] = []
        stderr_lines: List[str] = []

        async def _read_stream(stream: asyncio.StreamReader, sink: List[str], prefix: str) -> None:
            while True:
                line = await stream.readline()
                if not line:
                    break
                text = line.decode(errors="replace").rstrip("\n")
                # Strip ANSI escape sequences (progress bars, colors, cursor movements)
                text = strip_ansi_codes(text)
                # Skip empty lines that were only ANSI codes
                if not text.strip():
                    continue
                sink.append(text)
                if task_id:
                    await self.update_task(task_id, log=f"{prefix}{text}")

        stdout_task = asyncio.create_task(_read_stream(process.stdout, stdout_lines, ""))
        stderr_task = asyncio.create_task(_read_stream(process.stderr, stderr_lines, "[stderr] "))

        timed_out = False
        try:
            if timeout_seconds is not None:
                await asyncio.wait_for(process.wait(), timeout=timeout_seconds)
            else:
                await process.wait()
        except asyncio.TimeoutError:
            timed_out = True
            process.kill()
            await process.wait()

        await stdout_task
        await stderr_task

        rc = process.returncode
        stdout_text = "\n".join(stdout_lines)
        stderr_text = "\n".join(stderr_lines)

        if task_id:
            if timed_out:
                await self.update_task(task_id, log=f"[stderr] timed out after {timeout_seconds}s")
            await self.update_task(task_id, exit_code=rc, stderr=stderr_text)

        return rc, stdout_text, stderr_text
    
    async def run_tart_command(
        self,
        args: List[str],
        task_id: Optional[str] = None,
        timeout_seconds: Optional[float] = None,
        **kwargs
    ) -> Tuple[int, str, str]:
        """Run a tart command with the given arguments.

        Uses configurable timeouts from settings if not explicitly provided.
        """
        if timeout_seconds is None:
            cmd0 = args[0] if args else ""
            if cmd0 in {"list"}:
                timeout_seconds = settings.TIMEOUT_LIST
            elif cmd0 in {"get"}:
                timeout_seconds = settings.TIMEOUT_GET
            elif cmd0 in {"ip"}:
                timeout_seconds = settings.TIMEOUT_IP
            elif cmd0 in {"stop"}:
                timeout_seconds = settings.TIMEOUT_STOP
            elif cmd0 in {"delete"}:
                timeout_seconds = settings.TIMEOUT_DELETE
            elif cmd0 in {"pull"}:
                timeout_seconds = settings.TIMEOUT_PULL
            elif cmd0 in {"clone"}:
                timeout_seconds = settings.TIMEOUT_CLONE

        cmd = [settings.TART_PATH] + args
        return await self.run_command(cmd, task_id, timeout_seconds=timeout_seconds, **kwargs)

    async def start_tart_run_detached(
        self,
        args: List[str],
        vm_name: str,
        task_id: Optional[str] = None,
    ) -> Tuple[int, Path]:
        """Start a tart run command as a detached process with output redirected to a log file.

        Args:
            args: Command arguments (without the tart executable path)
            vm_name: Name of the VM
            task_id: Optional task ID for logging

        Returns:
            Tuple of (process PID, log file path)

        Raises:
            RuntimeError: If the process fails to start
        """
        logs_dir = settings.TOKEN_FILE.parent / "logs"
        logs_dir.mkdir(parents=True, exist_ok=True)
        try:
            logs_dir.chmod(0o700)
        except Exception:
            pass

        safe_name = "".join(c if c.isalnum() or c in {"-", "_", "."} else "_" for c in vm_name)
        ts = time.strftime("%Y%m%d-%H%M%S")
        log_path = logs_dir / f"{safe_name}-{ts}.log"

        cmd = [settings.TART_PATH] + args
        if task_id:
            await self.update_task(task_id, command=cmd)

        # Use context manager to ensure file handle is properly closed
        process = None
        try:
            with open(log_path, "a", encoding="utf-8") as log_file:
                process = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdin=subprocess.DEVNULL,
                    stdout=log_file,
                    stderr=log_file,
                    start_new_session=True,
                )
                # Process is now running; file handle will be closed when exiting context
                # but the process keeps its own reference to the file descriptor

            return process.pid, log_path

        except Exception as e:
            # If process failed to start, ensure we clean up
            if process is not None:
                try:
                    process.kill()
                    await process.wait()
                except Exception:
                    pass
            raise RuntimeError(f"Failed to start detached tart process: {e}") from e

    async def get_inventory(self) -> List[VMModel]:
        async with self._inventory_lock:
            return [self.inventory[name] for name in sorted(self.inventory.keys())]

    async def refresh_inventory(self, task_id: Optional[str] = None) -> List[VMModel]:
        async with self._refresh_lock:
            vms = await self._inventory_from_tart(task_id=task_id)
            async with self._inventory_lock:
                self.inventory = {vm.name: vm for vm in vms}
                self.inventory_last_refresh = time.time()
            return vms

    async def refresh_inventory_best_effort(self, task_id: Optional[str] = None) -> None:
        try:
            await self.refresh_inventory(task_id=task_id)
        except Exception:
            pass

    def start_inventory_monitoring(self, interval_seconds: float = 10.0) -> None:
        if self._inventory_monitor_task and not self._inventory_monitor_task.done():
            return
        self._inventory_monitor_task = asyncio.create_task(self._inventory_monitor_loop(interval_seconds))

    async def stop_inventory_monitoring(self) -> None:
        task = self._inventory_monitor_task
        self._inventory_monitor_task = None
        if not task:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    async def _inventory_monitor_loop(self, interval_seconds: float) -> None:
        while True:
            try:
                await self.refresh_inventory_best_effort()
            except Exception:
                pass
            await asyncio.sleep(interval_seconds)

    def start_task_cleanup(self, interval_seconds: float = 300.0, ttl_seconds: float = 3600.0) -> None:
        """Start periodic cleanup of old completed/failed tasks.

        Args:
            interval_seconds: How often to run cleanup (default: 5 minutes)
            ttl_seconds: Age threshold for removing tasks (default: 1 hour)
        """
        if self._task_cleanup_task and not self._task_cleanup_task.done():
            return
        self._task_cleanup_task = asyncio.create_task(
            self._task_cleanup_loop(interval_seconds, ttl_seconds)
        )

    async def stop_task_cleanup(self) -> None:
        """Stop the task cleanup background task."""
        task = self._task_cleanup_task
        self._task_cleanup_task = None
        if not task:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    async def _task_cleanup_loop(self, interval_seconds: float, ttl_seconds: float) -> None:
        """Background loop to periodically clean up old tasks."""
        while True:
            try:
                await self._cleanup_old_tasks(ttl_seconds)
            except Exception as e:
                logger.exception("Error during task cleanup")
            await asyncio.sleep(interval_seconds)

    async def _cleanup_old_tasks(self, ttl_seconds: float) -> None:
        """Remove completed/failed tasks older than ttl_seconds."""
        now = time.time()
        tasks_to_remove = []

        for task_id, task in self.tasks.items():
            # Only clean up completed or failed tasks
            if task.status not in (TaskStatus.COMPLETED, TaskStatus.FAILED):
                continue

            # Check if task is older than TTL
            age = now - task.updated_at
            if age > ttl_seconds:
                tasks_to_remove.append(task_id)

        # Remove old tasks
        for task_id in tasks_to_remove:
            self.tasks.pop(task_id, None)
            # Also clean up subscribers
            self._task_subscribers.pop(task_id, None)

        if tasks_to_remove:
            logger.info(f"Cleaned up {len(tasks_to_remove)} old tasks")

    async def clear_vm_config_cache(self, vm_name: str) -> None:
        async with self._vm_config_lock:
            self.vm_config_cache.pop(vm_name, None)

    async def clear_all_vm_config_cache(self) -> None:
        async with self._vm_config_lock:
            self.vm_config_cache.clear()

    async def get_vm_config(
        self,
        vm_name: str,
        task_id: Optional[str] = None,
        force_refresh: bool = False,
    ) -> VMConfigModel:
        ttl_seconds = 3600

        if not force_refresh:
            async with self._vm_config_lock:
                cached = self.vm_config_cache.get(vm_name)
                if cached:
                    cached_at, cached_model = cached
                    if (time.time() - cached_at) < ttl_seconds:
                        return cached_model

        rc, stdout, stderr = await self.run_tart_command(
            ["get", vm_name, "--format", "json"],
            task_id,
            timeout_seconds=10,
        )
        if rc != 0:
            raise RuntimeError(stderr or stdout or "tart get failed")

        raw: Dict[str, Any] = json.loads(stdout)

        def _first_int(*values: Any) -> Optional[int]:
            for v in values:
                if isinstance(v, bool):
                    continue
                if isinstance(v, int):
                    return v
                if isinstance(v, str):
                    try:
                        return int(v)
                    except ValueError:
                        continue
            return None

        def _first_str(*values: Any) -> Optional[str]:
            for v in values:
                if v is None:
                    continue
                if isinstance(v, (int, float)):
                    return str(v)
                if isinstance(v, str) and v.strip() != "":
                    return v
            return None

        def _format_memory(value: Any) -> Optional[str]:
            # Tart returns Memory as an integer number of MB (e.g. 8192).
            if value is None:
                return None
            if isinstance(value, bool):
                return None
            if isinstance(value, (int, float)):
                gib = float(value) / 1024.0
                if abs(gib - round(gib)) < 1e-9:
                    return f"{int(round(gib))}G"
                return f"{gib:.1f}G"
            if isinstance(value, str):
                s = value.strip()
                if not s:
                    return None
                try:
                    mb = float(s)
                except ValueError:
                    return s
                gib = mb / 1024.0
                if abs(gib - round(gib)) < 1e-9:
                    return f"{int(round(gib))}G"
                return f"{gib:.1f}G"
            return None

        def _format_disk(value: Any) -> Optional[str]:
            # Tart returns Disk as an integer number of GB (e.g. 50).
            if value is None:
                return None
            if isinstance(value, bool):
                return None
            if isinstance(value, (int, float)):
                if abs(float(value) - round(float(value))) < 1e-9:
                    return f"{int(round(float(value)))}G"
                return f"{float(value):.1f}G"
            if isinstance(value, str):
                s = value.strip()
                if not s:
                    return None
                try:
                    gb = float(s)
                except ValueError:
                    return s
                if abs(gb - round(gb)) < 1e-9:
                    return f"{int(round(gb))}G"
                return f"{gb:.1f}G"
            return None

        cpu = _first_int(
            raw.get("CPU"),
            raw.get("cpu"),
            raw.get("cpus"),
            raw.get("Cpu"),
        )
        memory = _format_memory(
            raw.get("Memory")
            if raw.get("Memory") is not None
            else (raw.get("memory") or raw.get("mem") or raw.get("Mem"))
        )
        disk_size = _format_disk(
            raw.get("Disk")
            if raw.get("Disk") is not None
            else (raw.get("disk") or raw.get("DiskSize") or raw.get("disk_size") or raw.get("diskSize"))
        )

        model = VMConfigModel(
            name=vm_name,
            cpu=cpu,
            memory=memory,
            disk_size=disk_size,
            raw=raw,
        )

        async with self._vm_config_lock:
            self.vm_config_cache[vm_name] = (time.time(), model)

        return model
    
    async def _inventory_from_tart(self, task_id: Optional[str] = None) -> List[VMModel]:
        rc, stdout, stderr = await self.run_tart_command(["list", "--format", "json"], task_id)
        if rc != 0:
            raise RuntimeError(stderr or stdout or "tart list failed")

        vms_data = json.loads(stdout)
        vms: List[VMModel] = []

        async def _fetch_ip(vm: VMModel) -> None:
            async with self._ip_semaphore:
                rc2, ip_stdout, _ = await self.run_tart_command(
                    ["ip", "--wait", "2", vm.name],
                    task_id,
                    timeout_seconds=4,
                )
                if rc2 == 0:
                    ip = ip_stdout.strip()
                    if ip:
                        vm.ip_address = ip

        ip_tasks: List[asyncio.Task] = []
        for vm_data in vms_data:
            running = bool(vm_data.get("Running")) or vm_data.get("State") == "running"
            vm = VMModel(
                name=vm_data["Name"],
                status=VMStatus.RUNNING if running else VMStatus.STOPPED,
                source=vm_data.get("Source"),
                disk_size=vm_data.get("Disk"),
            )
            vms.append(vm)
            if running:
                ip_tasks.append(asyncio.create_task(_fetch_ip(vm)))

        if ip_tasks:
            await asyncio.gather(*ip_tasks, return_exceptions=True)

        return vms

    async def list_vms(self) -> List[VMModel]:
        task = await self.create_task("list_vms")
        try:
            await self.update_task(task.id, status=TaskStatus.RUNNING)
            vms = await self.refresh_inventory(task_id=task.id)
            await self.update_task(task.id, status=TaskStatus.COMPLETED, result={"vms": [vm.dict() for vm in vms]})
            return vms
        except Exception as e:
            logger.exception("Failed to list VMs")
            await self.update_task(task.id, status=TaskStatus.FAILED, error=str(e))
            raise


# Global task manager instance
task_manager = TaskManager()

"""Main FastAPI application for Tart VM Manager."""
import asyncio
import logging
import os
import subprocess
import shutil
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Dict, List, Optional

from fastapi import (
    Body,
    Depends,
    FastAPI,
    Header,
    HTTPException,
    Request,
    status,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from pydantic import BaseModel, Field

from . import __version__
from .config import settings
from .models import (
    CloneVMRequest,
    TaskModel,
    TaskStatus,
    VMConfigModel,
    VMModel,
    VMImageModel,
    VMImagesSummary,
    VMSummary,
)
from .tasks import task_manager


# Track background tasks for proper cleanup
background_tasks: set = set()


def create_background_task(coro):
    """Create a background task and track it for cleanup."""
    task = asyncio.create_task(coro)
    background_tasks.add(task)
    task.add_done_callback(background_tasks.discard)
    return task


class StartVMRequest(BaseModel):
    vnc: bool = False
    extra_args: List[str] = Field(default_factory=list)


class PullVMRequest(BaseModel):
    oci_url: str


class GitHubTokenRequest(BaseModel):
    token: Optional[str] = None


class CreateVMRequest(BaseModel):
    name: str
    source_vm: str  # Name of the existing pulled OCI image to clone from
    cpu: int
    memory: int  # in GB
    disk_size: int  # in GB

# Set up logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan handler."""
    # Startup
    # Verify tart is installed
    if not shutil.which(settings.TART_PATH):
        logger.error(f"Tart not found at '{settings.TART_PATH}'. Please install Tart first.")
        raise RuntimeError("Tart not found. Please install Tart first.")

    logger.info("Tart VM Manager is starting up...")
    logger.info(f"Using Tart at: {shutil.which(settings.TART_PATH)}")
    logger.info(f"Local URL: http://{settings.HOST}:{settings.PORT}")
    logger.info(f"Token file: {settings.TOKEN_FILE}")
    logger.info("To rotate token: stop server, delete token file, and restart")

    # Initial VM list refresh
    try:
        await task_manager.refresh_inventory_best_effort()
        logger.info("Initial VM list refreshed successfully")
    except Exception as e:
        logger.error(f"Failed to refresh initial VM list: {e}")

    # Background inventory monitoring (keeps running/stopped state accurate)
    task_manager.start_inventory_monitoring(interval_seconds=10.0)

    # Background task cleanup (removes old completed/failed tasks)
    task_manager.start_task_cleanup(interval_seconds=300.0, ttl_seconds=3600.0)

    yield

    # Shutdown
    # Stop inventory monitoring
    await task_manager.stop_inventory_monitoring()

    # Stop task cleanup
    await task_manager.stop_task_cleanup()

    # Cancel all background tasks
    if background_tasks:
        logger.info(f"Cancelling {len(background_tasks)} background tasks...")
        for task in background_tasks:
            task.cancel()
        # Wait for all tasks to complete cancellation
        await asyncio.gather(*background_tasks, return_exceptions=True)
        logger.info("All background tasks cancelled")


# Initialize FastAPI app
app = FastAPI(
    title="de taartenfabriek",
    description="Web interface for managing Tart VMs on macOS",
    version=__version__,
    docs_url=None,  # Disable Swagger UI by default
    redoc_url=None,  # Disable ReDoc by default
    lifespan=lifespan,
)


@app.get("/appicon.png", include_in_schema=False)
async def app_icon():
    icon_path = Path(__file__).resolve().parent.parent / "appicon.png"
    return FileResponse(icon_path)

# Set up CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        f"http://{settings.HOST}:{settings.PORT}",
        f"http://127.0.0.1:{settings.PORT}",
        f"http://localhost:{settings.PORT}",
    ],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Set up templates
templates = Jinja2Templates(directory=os.path.join(os.path.dirname(__file__), "templates"))

# Set up static files
app.mount(
    "/static",
    StaticFiles(directory=os.path.join(os.path.dirname(__file__), "static")),
    name="static",
)


# Dependency to verify API token
async def verify_token(x_local_token: Optional[str] = Header(default=None)):
    """Verify the X-Local-Token header."""
    if not x_local_token or x_local_token != settings.SECRET_KEY:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid or missing X-Local-Token header",
        )
    return x_local_token


# Health check endpoint
@app.get("/api/health")
async def health_check():
    """Health check endpoint."""
    return {"status": "ok", "version": __version__}


@app.get("/api/settings/github-token", dependencies=[Depends(verify_token)])
async def get_github_token_status():
    """Get GitHub token configuration status (without revealing the actual token)."""
    return {
        "configured": settings.GITHUB_TOKEN is not None and len(settings.GITHUB_TOKEN) > 0,
        "masked_token": f"{settings.GITHUB_TOKEN[:4]}...{settings.GITHUB_TOKEN[-4:]}" if settings.GITHUB_TOKEN and len(settings.GITHUB_TOKEN) > 8 else None
    }


@app.post("/api/settings/github-token", dependencies=[Depends(verify_token)])
async def set_github_token(payload: GitHubTokenRequest):
    """Set or clear the GitHub API token."""
    try:
        if payload.token and payload.token.strip():
            # Save token to file
            token = payload.token.strip()
            settings.GITHUB_TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
            settings.GITHUB_TOKEN_FILE.write_text(token)
            settings.GITHUB_TOKEN_FILE.chmod(0o600)
            settings.GITHUB_TOKEN = token
            logger.info("GitHub token configured")
            return {"status": "success", "message": "GitHub token configured successfully"}
        else:
            # Clear token
            if settings.GITHUB_TOKEN_FILE.exists():
                settings.GITHUB_TOKEN_FILE.unlink()
            settings.GITHUB_TOKEN = None
            logger.info("GitHub token cleared")
            return {"status": "success", "message": "GitHub token cleared"}
    except Exception as e:
        logger.exception("Failed to save GitHub token")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to save GitHub token: {str(e)}"
        )


@app.get("/api/vms/available-images", response_model=List[VMImageModel])
async def get_available_images():
    """Get list of available Cirrus Labs macOS images from GitHub API.

    Requires a GitHub personal access token to be configured.
    """
    import aiohttp

    # Require GitHub token
    if not settings.GITHUB_TOKEN:
        logger.warning("GitHub token not configured, cannot fetch available images")
        return []

    try:
        async with aiohttp.ClientSession() as session:
            # Fetch packages from Cirrus Labs organization
            url = "https://api.github.com/orgs/cirruslabs/packages"
            params = {
                "package_type": "container",
                "per_page": 100
            }
            headers = {
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "Authorization": f"Bearer {settings.GITHUB_TOKEN}"
            }

            async with session.get(url, params=params, headers=headers) as response:
                if response.status != 200:
                    error_text = await response.text()
                    logger.error(f"Failed to fetch packages from GitHub: {response.status} - {error_text}")
                    return []

                packages = await response.json()

            # Filter for macOS packages and fetch their versions
            macos_images = []
            for package in packages:
                package_name = package.get("name", "")
                if not package_name.startswith("macos-"):
                    continue

                # Fetch package versions to get tags
                versions_url = f"https://api.github.com/orgs/cirruslabs/packages/container/{package_name}/versions"
                async with session.get(versions_url, headers=headers, params={"per_page": 10}) as versions_response:
                    if versions_response.status != 200:
                        logger.warning(f"Failed to fetch versions for {package_name}")
                        continue

                    versions = await versions_response.json()

                    # Get all tags from all versions
                    all_tags = []
                    latest_updated = None

                    for version in versions:
                        metadata = version.get("metadata", {})
                        container = metadata.get("container", {})
                        tags = container.get("tags", [])
                        all_tags.extend(tags)

                        # Track the most recent update
                        updated = version.get("updated_at")
                        if updated and (not latest_updated or updated > latest_updated):
                            latest_updated = updated

                    # Prioritize "latest" tag if available, otherwise use the first tag
                    if "latest" in all_tags:
                        default_tag = "latest"
                    elif all_tags:
                        default_tag = all_tags[0]
                    else:
                        default_tag = "latest"

                    # Build the OCI URL
                    oci_url = f"ghcr.io/cirruslabs/{package_name}:{default_tag}"

                    macos_images.append(VMImageModel(
                        name=package_name,
                        url=oci_url,
                        description=package.get("description"),
                        tags=all_tags,
                        updated_at=latest_updated
                    ))

            # Sort by name
            macos_images.sort(key=lambda x: x.name)

            logger.info(f"Fetched {len(macos_images)} images from GitHub API")
            return macos_images

    except Exception as e:
        logger.exception("Failed to fetch available images from GitHub API")
        return []


# API endpoints
@app.get("/api/tart/version", dependencies=[Depends(verify_token)])
async def get_tart_version():
    """Get the installed Tart version."""
    try:
        result = await asyncio.create_subprocess_exec(
            settings.TART_PATH, "--version",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, _ = await result.communicate()
        return {"version": stdout.decode().strip()}
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to get Tart version: {str(e)}",
        )


@app.get("/api/vms", response_model=List[VMModel], dependencies=[Depends(verify_token)])
async def list_vms():
    """List all VMs."""
    try:
        return await task_manager.get_inventory()
    except Exception as e:
        logger.exception("Failed to list VMs")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(e),
        )


@app.post("/api/vms/refresh", response_model=List[VMModel], dependencies=[Depends(verify_token)])
async def refresh_vms():
    """Refresh the VM list."""
    return await task_manager.list_vms()


@app.get("/api/vms/categorized", response_model=VMImagesSummary, dependencies=[Depends(verify_token)])
async def get_categorized_vms():
    """Get VMs categorized as base images vs working VMs."""
    vms = await task_manager.get_inventory()

    base_images = []
    working_vms = []

    for vm in vms:
        # VMs are OCI images if:
        # 1. Source is "OCI" (tart's designation for registry-pulled images), OR
        # 2. VM name starts with a known registry URL
        is_oci = (
            (vm.source and (vm.source.upper() == "OCI" or vm.source.startswith(("ghcr.io", "docker.io", "gcr.io")))) or
            vm.name.startswith(("ghcr.io/", "docker.io/", "gcr.io/"))
        )

        if is_oci:
            base_images.append(vm)
        else:
            working_vms.append(vm)

    return VMImagesSummary(
        base_images=base_images,
        working_vms=working_vms
    )


@app.get("/api/vms/{vm_name}", response_model=VMModel, dependencies=[Depends(verify_token)])
async def get_vm(vm_name: str):
    """Get details for a specific VM."""
    vms = await task_manager.get_inventory()
    for vm in vms:
        if vm.name == vm_name:
            return vm
    raise HTTPException(status_code=404, detail=f"VM '{vm_name}' not found")


@app.get("/api/vms/{vm_name}/config", response_model=VMConfigModel, dependencies=[Depends(verify_token)])
async def get_vm_config(vm_name: str, force_refresh: bool = False):
    """Get a VM's configuration via `tart get --format json` (cached server-side)."""
    return await task_manager.get_vm_config(vm_name, force_refresh=force_refresh)


@app.post("/api/vms/{vm_name}/start", response_model=TaskModel, dependencies=[Depends(verify_token)])
async def start_vm(
    vm_name: str,
    payload: Optional[StartVMRequest] = Body(default=None),
):
    """Start a VM."""
    task = await task_manager.create_task("start_vm", vm_name=vm_name)

    # Always start with VNC + no-graphics so users can connect via a vnc:// URL.
    vnc = True
    extra_args = payload.extra_args if payload else []

    # Run in background
    create_background_task(_start_vm(task.id, vm_name, vnc, extra_args))

    return task


async def _start_vm(task_id: str, vm_name: str, vnc: bool, extra_args: List[str]):
    """Background task to start a VM."""
    try:
        await task_manager.update_task(task_id, status=TaskStatus.RUNNING)
        
        # Build command
        cmd: List[str] = ["run"]
        if vnc:
            cmd.append("--vnc")
            cmd.append("--no-graphics")

        if extra_args:
            cmd.extend(extra_args)

        cmd.append(vm_name)

        await task_manager.update_task(task_id, log="Starting VM (detached)...")

        pid, log_path = await task_manager.start_tart_run_detached(cmd, vm_name=vm_name, task_id=task_id)

        await task_manager.update_task(task_id, log=f"tart run started (pid={pid})")

        # Poll for an IP address so the UI can render a vnc:// link.
        ip_address: Optional[str] = None
        deadline = time.time() + 60
        last_log_at: float = 0.0

        while time.time() < deadline:
            rc, stdout, _ = await task_manager.run_tart_command(
                ["ip", vm_name],
                timeout_seconds=5,
            )
            candidate = stdout.strip() if rc == 0 else ""
            if candidate:
                ip_address = candidate
                break

            if time.time() - last_log_at > 10:
                last_log_at = time.time()
                await task_manager.update_task(task_id, log="Waiting for VM IP...")

            await asyncio.sleep(1.5)

        result: Dict[str, Optional[str]] = {
            "message": f"VM '{vm_name}' started successfully",
            "ip_address": ip_address,
            "vnc_url": f"vnc://{ip_address}" if ip_address else None,
            "pid": str(pid),
            "log_path": str(log_path),
        }

        if ip_address:
            await task_manager.update_task(task_id, log=f"VM IP: {ip_address}")
        else:
            await task_manager.update_task(task_id, log="VM started but IP was not available yet")

        await task_manager.update_task(task_id, status=TaskStatus.COMPLETED, result=result)

        await task_manager.refresh_inventory_best_effort()
    except Exception as e:
        logger.exception(f"Failed to start VM '{vm_name}'")
        await task_manager.update_task(
            task_id,
            status=TaskStatus.FAILED,
            error="Failed to start VM",
        )


@app.post("/api/vms/{vm_name}/delete", response_model=TaskModel, dependencies=[Depends(verify_token)])
async def delete_vm(vm_name: str):
    """Delete a VM."""
    task = await task_manager.create_task("delete_vm", vm_name=vm_name)

    create_background_task(_delete_vm(task.id, vm_name))

    return task


async def _delete_vm(task_id: str, vm_name: str):
    """Background task to delete a VM."""
    try:
        await task_manager.update_task(task_id, status=TaskStatus.RUNNING)

        rc, stdout, stderr = await task_manager.run_tart_command(["delete", vm_name], task_id)
        if rc != 0:
            raise RuntimeError(stderr or stdout or "Failed to delete VM")

        await task_manager.update_task(
            task_id,
            status=TaskStatus.COMPLETED,
            result={"message": f"VM '{vm_name}' deleted successfully"},
        )

        await task_manager.clear_vm_config_cache(vm_name)

        await task_manager.refresh_inventory_best_effort()
    except Exception as e:
        logger.exception(f"Failed to delete VM '{vm_name}'")
        await task_manager.update_task(
            task_id,
            status=TaskStatus.FAILED,
            error=str(e),
        )


@app.post("/api/vms/{vm_name}/stop", response_model=TaskModel, dependencies=[Depends(verify_token)])
async def stop_vm(vm_name: str):
    """Stop a VM."""
    task = await task_manager.create_task("stop_vm", vm_name=vm_name)

    # Run in background
    create_background_task(_stop_vm(task.id, vm_name))

    return task


async def _stop_vm(task_id: str, vm_name: str):
    """Background task to stop a VM."""
    try:
        await task_manager.update_task(task_id, status=TaskStatus.RUNNING)

        # Tart versions vary; this project targets: `tart stop <name> [--timeout <timeout>]`
        rc, stdout, stderr = await task_manager.run_tart_command(
            ["stop", "--timeout", "30", vm_name],
            task_id,
            timeout_seconds=40,
        )

        if rc != 0:
            # Fallback: try without timeout flag.
            rc, stdout, stderr = await task_manager.run_tart_command(
                ["stop", vm_name],
                task_id,
                timeout_seconds=40,
            )
            if rc != 0:
                raise RuntimeError(stderr or stdout or "Failed to stop VM")
        
        await task_manager.update_task(
            task_id,
            status=TaskStatus.COMPLETED,
            result={"message": f"VM '{vm_name}' stopped successfully"},
        )

        await task_manager.refresh_inventory_best_effort()
    except Exception as e:
        logger.exception(f"Failed to stop VM '{vm_name}'")
        await task_manager.update_task(
            task_id,
            status=TaskStatus.FAILED,
            error=str(e),
        )


@app.post("/api/vms/pull", response_model=TaskModel, dependencies=[Depends(verify_token)])
async def pull_vm(payload: PullVMRequest):
    """Pull a VM image from an OCI registry."""
    task = await task_manager.create_task("pull_vm", oci_url=payload.oci_url)

    # Run in background
    create_background_task(_pull_vm(task.id, payload.oci_url))

    return task


async def _pull_vm(task_id: str, oci_url: str):
    """Background task to pull a VM image."""
    try:
        await task_manager.update_task(task_id, status=TaskStatus.RUNNING)

        # Pull the image
        rc, stdout, stderr = await task_manager.run_tart_command(["pull", oci_url], task_id)

        if rc != 0:
            raise RuntimeError(stderr or stdout or "Failed to pull VM")

        await task_manager.update_task(
            task_id,
            status=TaskStatus.COMPLETED,
            result={"message": f"VM image pulled successfully: {oci_url}"},
        )

        await task_manager.refresh_inventory_best_effort()
    except Exception as e:
        logger.exception(f"Failed to pull VM: {oci_url}")
        await task_manager.update_task(
            task_id,
            status=TaskStatus.FAILED,
            error=str(e),
        )


async def _poll_for_ip(vm_name: str, task_id: str) -> Optional[str]:
    """Helper to poll for VM IP address."""
    deadline = time.time() + 60
    last_log_at: float = 0.0

    while time.time() < deadline:
        rc, stdout, _ = await task_manager.run_tart_command(
            ["ip", vm_name],
            timeout_seconds=5,
        )
        candidate = stdout.strip() if rc == 0 else ""
        if candidate:
            return candidate

        if time.time() - last_log_at > 10:
            last_log_at = time.time()
            await task_manager.update_task(task_id, log="Waiting for VM IP...")

        await asyncio.sleep(1.5)

    return None


@app.post("/api/vms/create", response_model=TaskModel, dependencies=[Depends(verify_token)])
async def create_vm(payload: CreateVMRequest):
    """Create a new VM from an already-pulled OCI image with specified configuration."""
    task = await task_manager.create_task("create_vm", vm_name=payload.name)

    create_background_task(_create_vm(
        task.id,
        payload.name,
        payload.source_vm,
        payload.cpu,
        payload.memory,
        payload.disk_size
    ))

    return task


async def _create_vm(
    task_id: str,
    vm_name: str,
    source_vm: str,
    cpu: int,
    memory: int,
    disk_size: int
):
    """Background task to create a new VM from an existing pulled OCI image."""
    try:
        await task_manager.update_task(task_id, status=TaskStatus.RUNNING)

        # Step 1: Clone the existing OCI image to the desired VM name
        await task_manager.update_task(task_id, log=f"Cloning from {source_vm} to {vm_name}...")

        rc, stdout, stderr = await task_manager.run_tart_command(
            ["clone", source_vm, vm_name],
            task_id,
        )

        if rc != 0:
            raise RuntimeError(stderr or stdout or "Failed to clone VM")

        await task_manager.update_task(task_id, log=f"VM cloned successfully")

        # Step 2: Configure CPU
        await task_manager.update_task(task_id, log=f"Setting CPU cores to {cpu}...")
        rc, stdout, stderr = await task_manager.run_tart_command(
            ["set", vm_name, "--cpu", str(cpu)],
            task_id,
        )
        if rc != 0:
            logger.warning(f"Failed to set CPU: {stderr or stdout}")
            await task_manager.update_task(task_id, log=f"Warning: Could not set CPU cores")

        # Step 3: Configure memory (convert GB to MB)
        memory_mb = memory * 1024
        await task_manager.update_task(task_id, log=f"Setting memory to {memory}GB...")
        rc, stdout, stderr = await task_manager.run_tart_command(
            ["set", vm_name, "--memory", str(memory_mb)],
            task_id,
        )
        if rc != 0:
            logger.warning(f"Failed to set memory: {stderr or stdout}")
            await task_manager.update_task(task_id, log=f"Warning: Could not set memory")

        # Step 4: Configure disk size
        await task_manager.update_task(task_id, log=f"Setting disk size to {disk_size}GB...")
        rc, stdout, stderr = await task_manager.run_tart_command(
            ["set", vm_name, "--disk-size", str(disk_size)],
            task_id,
        )
        if rc != 0:
            logger.warning(f"Failed to set disk size: {stderr or stdout}")
            await task_manager.update_task(task_id, log=f"Warning: Could not set disk size")

        await task_manager.update_task(
            task_id,
            status=TaskStatus.COMPLETED,
            result={
                "message": f"VM '{vm_name}' created successfully from {source_vm}",
                "name": vm_name,
                "source": source_vm,
                "cpu": cpu,
                "memory": f"{memory}GB",
                "disk_size": f"{disk_size}GB"
            },
        )

        await task_manager.refresh_inventory_best_effort()

    except Exception as e:
        logger.exception(f"Failed to create VM '{vm_name}'")
        await task_manager.update_task(
            task_id,
            status=TaskStatus.FAILED,
            error=str(e),
        )


@app.post("/api/vms/{vm_name}/clone", response_model=TaskModel, dependencies=[Depends(verify_token)])
async def clone_vm(vm_name: str, payload: CloneVMRequest):
    """Clone a VM and optionally start it with VNC."""
    task = await task_manager.create_task("clone_vm", vm_name=vm_name)

    create_background_task(_clone_vm(task.id, vm_name, payload.new_name, payload.start_after_clone))

    return task


async def _clone_vm(task_id: str, vm_name: str, new_name: str, start_after: bool):
    """Background task to clone a VM and optionally start it with VNC."""
    try:
        await task_manager.update_task(task_id, status=TaskStatus.RUNNING)

        # Clone the VM
        await task_manager.update_task(task_id, log=f"Cloning {vm_name} to {new_name}...")
        rc, stdout, stderr = await task_manager.run_tart_command(
            ["clone", vm_name, new_name],
            task_id,
        )

        if rc != 0:
            raise RuntimeError(stderr or stdout or "Failed to clone VM")

        await task_manager.update_task(task_id, log=f"Clone completed: {new_name}")

        result: Dict[str, Optional[str]] = {
            "message": f"VM cloned successfully: {new_name}",
            "new_vm_name": new_name,
        }

        # Start with VNC if requested
        if start_after:
            await task_manager.update_task(task_id, log="Starting cloned VM with VNC...")

            # Use same logic as _start_vm for VNC
            cmd: List[str] = ["run", "--vnc", "--no-graphics", new_name]

            pid, log_path = await task_manager.start_tart_run_detached(cmd, vm_name=new_name, task_id=task_id)

            await task_manager.update_task(task_id, log=f"tart run started (pid={pid})")

            # Poll for IP
            ip_address = await _poll_for_ip(new_name, task_id)

            result.update({
                "started": True,
                "ip_address": ip_address,
                "vnc_url": f"vnc://{ip_address}" if ip_address else None,
                "pid": str(pid),
                "log_path": str(log_path),
            })

            if ip_address:
                await task_manager.update_task(task_id, log=f"VM IP: {ip_address}")
                await task_manager.update_task(task_id, log=f"VNC URL: vnc://{ip_address}")
            else:
                await task_manager.update_task(task_id, log="VM started but IP was not available yet")

        await task_manager.update_task(
            task_id,
            status=TaskStatus.COMPLETED,
            result=result,
        )

        await task_manager.refresh_inventory_best_effort()

    except Exception as e:
        logger.exception(f"Failed to clone VM '{vm_name}'")
        await task_manager.update_task(
            task_id,
            status=TaskStatus.FAILED,
            error=str(e),
        )


@app.get("/api/tasks/active", response_model=List[TaskModel], dependencies=[Depends(verify_token)])
async def get_active_tasks():
    """Get all active (pending/running) tasks."""
    return [
        task for task in task_manager.tasks.values()
        if task.status in (TaskStatus.PENDING, TaskStatus.RUNNING)
    ]


@app.get("/api/tasks/{task_id}", response_model=TaskModel, dependencies=[Depends(verify_token)])
async def get_task(task_id: str):
    """Get the status of a background task."""
    task = await task_manager.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail=f"Task '{task_id}' not found")
    return task


@app.websocket("/ws/tasks/{task_id}")
async def websocket_task(websocket: WebSocket, task_id: str):
    """WebSocket endpoint for task updates with keepalive."""
    token = websocket.query_params.get("token")
    if not token or token != settings.SECRET_KEY:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return

    task = await task_manager.get_task(task_id)
    if not task:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return

    await websocket.accept()

    # Send current task state
    await websocket.send_json(task.dict())

    # Create a keepalive task to send pings
    keepalive_interval = 30  # seconds
    async def send_keepalive():
        try:
            while True:
                await asyncio.sleep(keepalive_interval)
                try:
                    await websocket.send_text("")  # Send empty ping
                except Exception:
                    break
        except asyncio.CancelledError:
            pass

    keepalive_task = create_background_task(send_keepalive())

    try:
        # Subscribe to task updates
        async for update in task_manager.subscribe_to_task(task_id):
            try:
                await websocket.send_json(update.dict())
                if update.status in (TaskStatus.COMPLETED, TaskStatus.FAILED):
                    await websocket.close()
                    break
            except WebSocketDisconnect:
                break
    finally:
        # Clean up keepalive task
        keepalive_task.cancel()
        try:
            await keepalive_task
        except asyncio.CancelledError:
            pass


# Frontend routes
@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    """Serve the main application page."""
    return templates.TemplateResponse("index.html", {
        "request": request,
        "api_token": settings.SECRET_KEY
    })


# Error handlers
@app.exception_handler(HTTPException)
async def http_exception_handler(request, exc):
    """Handle HTTP exceptions."""
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": exc.detail},
    )


@app.exception_handler(Exception)
async def global_exception_handler(request, exc):
    """Handle all other exceptions."""
    logger.exception("Unhandled exception")
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={"detail": "Internal server error"},
    )


# For development only
if __name__ == "__main__":
    import uvicorn
    
    uvicorn.run(
        "tartvm.main:app",
        host=settings.HOST,
        port=settings.PORT,
        reload=True,
        log_level="info",
    )

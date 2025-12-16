"""Main FastAPI application for Tart VM Manager."""
import asyncio
import logging
import os
import subprocess
import shutil
import time
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
from .models import TaskModel, TaskStatus, VMConfigModel, VMModel, VMSummary
from .tasks import task_manager


class StartVMRequest(BaseModel):
    vnc: bool = False
    extra_args: List[str] = Field(default_factory=list)


class PullVMRequest(BaseModel):
    oci_url: str

# Set up logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Initialize FastAPI app
app = FastAPI(
    title="de taartenfabriek",
    description="Web interface for managing Tart VMs on macOS",
    version=__version__,
    docs_url=None,  # Disable Swagger UI by default
    redoc_url=None,  # Disable ReDoc by default
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
    asyncio.create_task(_start_vm(task.id, vm_name, vnc, extra_args))
    
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

    asyncio.create_task(_delete_vm(task.id, vm_name))

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
    asyncio.create_task(_stop_vm(task.id, vm_name))
    
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
    asyncio.create_task(_pull_vm(task.id, payload.oci_url))
    
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


@app.get("/api/tasks/{task_id}", response_model=TaskModel, dependencies=[Depends(verify_token)])
async def get_task(task_id: str):
    """Get the status of a background task."""
    task = await task_manager.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail=f"Task '{task_id}' not found")
    return task


@app.websocket("/ws/tasks/{task_id}")
async def websocket_task(websocket: WebSocket, task_id: str):
    """WebSocket endpoint for task updates."""
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
    
    # Subscribe to task updates
    async for update in task_manager.subscribe_to_task(task_id):
        try:
            await websocket.send_json(update.dict())
            if update.status in (TaskStatus.COMPLETED, TaskStatus.FAILED):
                await websocket.close()
                break
        except WebSocketDisconnect:
            break


# Frontend routes
@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    """Serve the main application page."""
    return templates.TemplateResponse("index.html", {
        "request": request,
        "api_token": settings.SECRET_KEY
    })


# Application startup event
@app.on_event("startup")
async def startup_event():
    """Initialize the application on startup."""
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


@app.on_event("shutdown")
async def shutdown_event():
    """Cleanup on shutdown."""
    await task_manager.stop_inventory_monitoring()


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

"""Data models for the Tart VM Manager."""
import time
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class VMStatus(str, Enum):
    """VM status enum."""
    RUNNING = "running"
    STOPPED = "stopped"
    UNKNOWN = "unknown"


class VMModel(BaseModel):
    """VM model representing a Tart VM."""
    name: str
    status: VMStatus = VMStatus.UNKNOWN
    ip_address: Optional[str] = None
    source: Optional[str] = None
    os: Optional[str] = None
    cpu: Optional[int] = None
    memory: Optional[str] = None
    disk_size: Optional[int] = None
    display: Optional[str] = None


class TaskStatus(str, Enum):
    """Task status enum."""
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class TaskModel(BaseModel):
    """Task model for background operations."""
    id: str
    action: str
    status: TaskStatus = TaskStatus.PENDING
    command: Optional[List[str]] = None
    exit_code: Optional[int] = None
    result: Optional[Dict] = None
    error: Optional[str] = None
    stderr: Optional[str] = None
    created_at: float = Field(default_factory=lambda: time.time())
    updated_at: float = Field(default_factory=lambda: time.time())
    logs: List[str] = Field(default_factory=list)


class VMSummary(BaseModel):
    """Summary of VMs."""
    total: int = 0
    running: int = 0
    stopped: int = 0
    unknown: int = 0


class VMConfigModel(BaseModel):
    name: str
    cpu: Optional[int] = None
    memory: Optional[str] = None
    disk_size: Optional[str] = None
    raw: Dict[str, Any] = Field(default_factory=dict)


class VMImageModel(BaseModel):
    """Model for available VM images."""
    name: str
    url: str
    description: Optional[str] = None
    tags: List[str] = Field(default_factory=list)
    updated_at: Optional[str] = None


class CloneVMRequest(BaseModel):
    """Request model for cloning a VM."""
    new_name: str
    start_after_clone: bool = False


class VMImagesSummary(BaseModel):
    """Categorized VM list."""
    base_images: List[VMModel] = Field(default_factory=list)
    working_vms: List[VMModel] = Field(default_factory=list)

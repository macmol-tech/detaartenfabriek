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


class VMCreate(BaseModel):
    """Model for creating a new VM."""
    name: str
    source: str
    cpu: Optional[int] = 4
    memory: Optional[str] = "4G"
    disk_size: Optional[str] = "32G"


class VMUpdate(BaseModel):
    """Model for updating a VM."""
    cpu: Optional[int] = None
    memory: Optional[str] = None
    disk_size: Optional[str] = None


class VMAction(str, Enum):
    """VM actions."""
    START = "start"
    STOP = "stop"
    DELETE = "delete"
    CLONE = "clone"
    PULL = "pull"


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

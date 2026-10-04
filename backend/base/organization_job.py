"""Durable execution vocabulary. Plans contain no execution state."""

from dataclasses import dataclass
from enum import Enum
from typing import Optional, Tuple

EXECUTOR_POLICY = 'kapowarr-organization-executor/v1'


class JobState(Enum):
    PENDING = 'pending'
    RUNNING = 'running'
    COMPLETED = 'completed'
    FAILED = 'failed'
    RECOVERY = 'recovery_required'


class StepState(Enum):
    PENDING = 'pending'
    STARTED = 'started'
    SUCCEEDED = 'succeeded'


class ExecutionCode(Enum):
    CANCELLED = 'cancelled'
    NOT_AUTHORIZED = 'plan_not_ready'
    STALE = 'stale_plan_replan_required'
    SOURCE = 'source_missing_or_changed'
    OCCUPIED = 'target_occupied'
    PERMISSION = 'permission_denied'
    DISK_FULL = 'disk_full'
    UNSUPPORTED = 'unsupported_operation'
    UNSAFE_PATH = 'unsafe_path_or_symlink'
    METADATA = 'comicinfo_write_failed'
    DATABASE = 'database_update_failed'
    RECEIPT = 'receipt_write_failed'
    CONFLICT = 'reconciliation_conflict'
    BUSY = 'executor_or_path_claimed'
    CORRUPT = 'corrupt_or_unsupported_history'
    UNDO = 'undo_precondition_failed'
    IO = 'filesystem_operation_failed'


class OrganizationError(Exception):
    def __init__(self, code: ExecutionCode, detail: str = ''):
        self.code, self.detail = code, detail
        super().__init__(code.value)


@dataclass(frozen=True)
class StepReceipt:
    ordinal: int
    kind: str
    state: StepState
    # Bounded JSON evidence, deliberately not a raw exception or archive blob.
    evidence: str


@dataclass(frozen=True)
class OrganizationJob:
    id: str
    plan_digest: str
    state: JobState
    source: str
    target: str
    created_at: str
    updated_at: str
    steps: Tuple[StepReceipt, ...]
    error: Optional[str]
    inverse_of: Optional[str]
    batch_id: Optional[str]
    executor_policy: str = EXECUTOR_POLICY


@dataclass(frozen=True)
class UndoPreview:
    original_job: str
    eligible: bool
    current_path: str
    restore_path: str
    reasons: Tuple[str, ...]
    intent_digest: Optional[str] = None
    database_before: Optional[str] = None
    database_restore: Optional[str] = None
    conditional_empty_directories: Tuple[str, ...] = ()

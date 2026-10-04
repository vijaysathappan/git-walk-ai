"""
Pydantic v2 schemas for request/response validation.
"""

from typing import Any

from pydantic import BaseModel, Field
from datetime import datetime


class SyncRequest(BaseModel):
    """Schema for real-time cell sync requests from the Excel taskpane."""

    table_id: str = Field(
        ...,
        description="The dynamically provisioned SQLite table name (e.g. QUEUE_BOARD_<UUID>)",
        examples=["QUEUE_BOARD_a1b2c3d4"],
    )
    row_id: int = Field(
        ...,
        description="The ROW_ID (auto-increment PK) of the row being edited",
        ge=1,
    )
    column_name: str = Field(
        ...,
        description="The sanitized column name to update",
        examples=["EMPLOYEE_NAME"],
    )
    new_value: str | None = Field(
        None,
        description="The new cell value as a string (None for cleared cells)",
    )
    source: str = "excel"
    commit_message: str | None = None
    repository_id: str | None = None
    branch_id: str | None = None
    working_copy_id: str | None = None
    base_commit_id: str | None = None
    issued_at: str | None = None
    signature: str | None = None


class SyncResponse(BaseModel):
    """Schema for sync operation responses."""

    status: str = Field(
        ...,
        description="Operation result: SUCCESS or ERROR",
        examples=["SUCCESS"],
    )
    timestamp: str = Field(
        default_factory=lambda: datetime.utcnow().isoformat() + "Z",
        description="ISO 8601 timestamp of the operation",
    )
    message: str | None = Field(
        None,
        description="Optional detail message",
    )
    persisted_value: str | int | float | bool | None = Field(
        None,
        description="Value read back from SQLite after the commit",
    )
    changed: bool = Field(
        ...,
        description="True when this request changed the stored value",
    )


class CellValueResponse(BaseModel):
    """Read-only response used to verify a selected Excel cell."""

    table_id: str
    row_id: int
    column_name: str
    value: str | int | float | bool | None = None


class UploadResponse(BaseModel):
    """Metadata returned alongside the file download (as headers)."""

    table_id: str
    row_count: int
    column_count: int


class LoginRequest(BaseModel):
    email: str = Field(..., min_length=5, max_length=254)


class LoginVerifyRequest(LoginRequest):
    code: str = Field(..., min_length=6, max_length=6)
    machine_id: str | None = Field(default=None, max_length=200)


class WorkbookAuthRequest(BaseModel):
    table_id: str = Field(..., min_length=8, max_length=80)
    repository_id: str = Field(..., min_length=4, max_length=80)
    branch_id: str = Field(..., min_length=4, max_length=80)
    working_copy_id: str = Field(..., min_length=4, max_length=80)
    base_commit_id: str = Field(..., min_length=4, max_length=80)
    issued_at: str = Field(..., min_length=10, max_length=80)
    signature: str = Field(..., min_length=32, max_length=128)
    machine_id: str | None = Field(default=None, max_length=200)


class WorkbookVerifyRequest(BaseModel):
    repository_id: str = Field(..., min_length=4, max_length=80)
    branch_id: str = Field(..., min_length=4, max_length=80)
    working_copy_id: str = Field(..., min_length=4, max_length=80)
    email: str = Field(..., min_length=3, max_length=120)
    code: str = Field(..., min_length=6, max_length=6)
    current_file_path: str = Field(..., min_length=1, max_length=500)
    machine_id: str | None = Field(default=None, max_length=200)


class SetPasswordRequest(BaseModel):
    user_id_or_email: str = Field(..., min_length=3, max_length=120)
    password: str = Field(..., min_length=1, max_length=128)


class SanitizeLocalRequest(BaseModel):
    working_copy_id: str | None = None
    file_path: str | None = None
    table_id: str | None = None
    force: bool = False


class BulkCellChange(BaseModel):
    row_id: int = Field(..., ge=1)
    column_name: str
    new_value: Any = None


class BulkSyncRequest(BaseModel):
    table_id: str
    changes: list[BulkCellChange] = Field(..., min_length=1, max_length=5000)
    source: str = "excel"
    commit_message: str | None = Field(default=None, max_length=300)
    repository_id: str | None = None
    branch_id: str | None = None
    working_copy_id: str | None = None
    base_commit_id: str | None = None
    issued_at: str | None = None
    signature: str | None = None


class SemanticChange(BaseModel):
    operation_type: str = Field(..., min_length=3, max_length=40)
    sheet_id: str | None = None
    row_id: str | None = None
    column_id: str | None = None
    previous_row_position: int | None = Field(default=None, ge=0)
    new_row_position: int | None = Field(default=None, ge=0)
    previous_column_position: int | None = Field(default=None, ge=0)
    new_column_position: int | None = Field(default=None, ge=0)
    previous_cell_reference: str | None = None
    new_cell_reference: str | None = None
    old_value: Any = None
    new_value: Any = None
    old_formula: str | None = None
    new_formula: str | None = None
    old_data_type: str | None = None
    new_data_type: str | None = None
    old_style_hash: str | None = None
    new_style_hash: str | None = None
    old_comment: str | None = None
    new_comment: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class WorkbookCommitRequest(BaseModel):
    table_id: str
    base_version: int = Field(..., ge=0)
    updates: list[BulkCellChange] = Field(default_factory=list, max_length=5000)
    insert_rows: list[dict[str, Any]] = Field(default_factory=list, max_length=2000)
    delete_row_ids: list[int] = Field(default_factory=list, max_length=2000)
    new_columns: list[str] = Field(default_factory=list, max_length=100)
    delete_columns: list[str] = Field(default_factory=list, max_length=100)
    source: str = "excel_commit"
    commit_message: str = Field(..., min_length=1, max_length=300)
    repository_id: str | None = None
    branch_id: str | None = None
    working_copy_id: str | None = None
    base_commit_id: str | None = None
    issued_at: str | None = None
    signature: str | None = None
    expected_head_commit_id: str | None = None
    semantic_changes: list[SemanticChange] = Field(default_factory=list, max_length=10000)


class CategoryCreateRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=500)
    parent_category_id: str | None = Field(default="CAT_HOME", max_length=64)


class RepositoryCategoryRequest(BaseModel):
    category_id: str = Field(..., min_length=4, max_length=64)


class WorkingCopyRequest(BaseModel):
    mode: str = Field(default="continue", pattern="^(continue|new)$")
    branch_id: str | None = Field(default=None, min_length=4, max_length=80)
    local_download_dir: str | None = Field(default=None, max_length=500)


class EucStorageSettingsRequest(BaseModel):
    local_download_dir: str = Field(..., min_length=2, max_length=500)


class BranchCreateRequest(BaseModel):
    name: str = Field(..., min_length=3, max_length=255)
    from_commit_id: str | None = Field(default=None, min_length=4, max_length=100)


class RollbackRequest(BaseModel):
    table_id: str
    batch_id: str


class DatasetMemberRequest(BaseModel):
    email: str = Field(..., min_length=5, max_length=254)
    role: str = Field(default="viewer", pattern="^(viewer|editor)$")


class PresenceRequest(BaseModel):
    client_id: str = Field(..., min_length=8, max_length=100)
    surface: str = Field(..., pattern="^(browser|excel)$")
    activity: str = Field(default="viewing", pattern="^(viewing|editing|idle)$")
    status: str = Field(default="ONLINE", pattern="^(ONLINE|NEED_HELP)$")


class AIInsightRequest(BaseModel):
    table_id: str | None = None
    branch_id: str | None = None
    merge_request_id: str | None = None
    model: str | None = None
    question: str = Field(..., min_length=3, max_length=1000)


class AIConfigRequest(BaseModel):
    api_key: str = Field(..., min_length=10, max_length=500)
    model: str = Field(..., min_length=3, max_length=200)


class MergeRequestCreate(BaseModel):
    source_branch_id: str = Field(..., min_length=4, max_length=80)
    target_branch_id: str = Field(..., min_length=4, max_length=80)
    title: str = Field(..., min_length=3, max_length=200)
    description: str | None = Field(default=None, max_length=2000)


class ConflictResolutionRequest(BaseModel):
    resolution_type: str = Field(pattern="^(KEEP_MAIN|ACCEPT_BRANCH|CUSTOM)$")
    custom_value: Any = None


class MergeReviewRequest(BaseModel):
    decision: str = Field(pattern="^(APPROVED|REJECTED)$")
    comment: str | None = Field(default=None, max_length=1000)


class ReviewerRequestCreate(BaseModel):
    reviewer_user_id: str = Field(..., min_length=4, max_length=80)


class ReviewerResponseRequest(BaseModel):
    decision: str = Field(pattern="^(APPROVED|REJECTED)$")
    comment: str | None = Field(default=None, max_length=1000)

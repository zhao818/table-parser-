"""
Pydantic data models for table structure validation.

These models define the standardized JSON schema that the LLM must output,
ensuring every parsed table contains valid rowspan/colspan information.
"""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field, model_validator


class CellData(BaseModel):
    """A single cell in the table grid.

    Attributes:
        text: The textual content of the cell.
        rowspan: Number of rows this cell spans (default 1).
        colspan: Number of columns this cell spans (default 1).
    """

    text: str = ""
    rowspan: int = Field(default=1, ge=1, le=100)
    colspan: int = Field(default=1, ge=1, le=100)


class TableData(BaseModel):
    """The complete parsed table structure.

    Attributes:
        title: Optional table title extracted from the image.
        rows: A 2D grid of CellData objects representing the table.
    """

    title: str = ""
    rows: list[list[CellData]] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_rows_not_empty(self) -> "TableData":
        if not self.rows:
            raise ValueError("rows must contain at least one row")
        for i, row in enumerate(self.rows):
            if not row:
                raise ValueError(f"row[{i}] must contain at least one cell")
        return self

    @property
    def max_columns(self) -> int:
        """Calculate the maximum number of logical columns across all rows."""
        if not self.rows:
            return 0
        return max(sum(cell.colspan for cell in row) for row in self.rows)

    @property
    def row_count(self) -> int:
        return len(self.rows)


class GenerateRequest(BaseModel):
    """Request model for the simple table generation endpoint."""

    headers: list[str] = Field(..., min_length=1, max_length=100)
    data_rows: list[list[str]] = Field(default_factory=list)
    title: Optional[str] = None
    sheet_name: str = Field(default="Sheet1", max_length=31)


class MultiSheetRequest(BaseModel):
    """Request model for multi-sheet Excel generation."""

    sheets: dict[str, TableData] = Field(..., min_length=1, max_length=50)
    title: Optional[str] = None


class APIResponse(BaseModel):
    """Standard API response envelope."""

    success: bool
    message: str = ""
    data: Optional[dict] = None


class ErrorResponse(BaseModel):
    """Standard API error response."""

    success: bool = False
    error: str
    detail: Optional[str] = None

"""
FastAPI application entry point for the Universal Table Parsing Engine.

Provides:
- POST /api/upload        — single image upload → Excel download
- POST /api/upload/batch  — batch image upload → multi-sheet Excel
- POST /api/generate      — JSON-in → Excel-out (no LLM)
- POST /api/generate/multi — multi-sheet JSON → Excel
- GET  /api/health        — health check
- GET  /                  — serves the frontend UI
"""

from __future__ import annotations

import logging
import mimetypes
from contextlib import asynccontextmanager
from typing import Optional
from urllib.parse import quote

from fastapi import FastAPI, File, HTTPException, Query, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from .config import STATIC_DIR, Settings, get_settings  # noqa: E402
from .excel_generator import generate_excel, generate_excel_multi_sheet, generate_excel_simple, validate_table_json
from .llm_service import LLMService, create_llm_service
from .models import APIResponse, ErrorResponse, GenerateRequest, MultiSheetRequest

# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------


def _setup_logging(settings: Settings) -> None:
    """Configure structured or plain-text logging."""
    fmt = (
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
        if settings.log_format == "text"
        else '{"time":"%(asctime)s","level":"%(levelname)s","logger":"%(name)s","msg":"%(message)s"}'
    )
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format=fmt,
        datefmt="%Y-%m-%dT%H:%M:%S",
    )


# ---------------------------------------------------------------------------
# Application lifecycle
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup / shutdown hooks."""
    settings = get_settings()
    _setup_logging(settings)
    logger = logging.getLogger(__name__)
    logger.info(
        "Table Parsing Engine starting: model=%s mock=%s",
        settings.llm_model,
        not settings.llm_api_key,
    )
    # Pre-warm the LLM service singleton
    app.state.llm_service = create_llm_service()
    yield
    logger.info("Table Parsing Engine shutting down")


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------


def create_app() -> FastAPI:
    """Build and configure the FastAPI application."""
    settings = get_settings()

    app = FastAPI(
        title="通用型表格解析引擎",
        description="Upload table images, get structured Excel files via multimodal LLM.",
        version="1.0.0",
        lifespan=lifespan,
        docs_url="/docs",
        redoc_url="/redoc",
    )

    # CORS
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Exception handlers
    @app.exception_handler(ValueError)
    async def value_error_handler(request: Request, exc: ValueError) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content=ErrorResponse(error="Validation Error", detail=str(exc)).model_dump(),
        )

    @app.exception_handler(RuntimeError)
    async def runtime_error_handler(request: Request, exc: RuntimeError) -> JSONResponse:
        logging.getLogger(__name__).error("Runtime error: %s", exc)
        return JSONResponse(
            status_code=502,
            content=ErrorResponse(error="LLM Service Error", detail=str(exc)).model_dump(),
        )

    @app.exception_handler(HTTPException)
    async def http_exception_handler(request: Request, exc: HTTPException) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content=ErrorResponse(error=exc.detail or "Request Error").model_dump(),
        )

    # ------------------------------------------------------------------
    # Routes
    # ------------------------------------------------------------------

    @app.get("/api/health", response_model=APIResponse)
    async def health_check():
        """Health check endpoint."""
        svc: LLMService = app.state.llm_service
        return APIResponse(
            success=True,
            message="ok",
            data={
                "version": "1.0.0",
                "mock_mode": svc._mock,
                "model": svc._model if not svc._mock else "mock",
            },
        )

    @app.post("/api/upload")
    async def upload_single(
        file: UploadFile = File(...),
        use_mock: bool = Query(default=False, description="Force mock mode"),
        auto_width: bool = Query(default=True, description="Auto-adjust column widths"),
        first_row_as_header: bool = Query(
            default=True, description="Style first row as header"
        ),
    ):
        """Upload a single table image, receive a formatted Excel file.

        Supported formats: PNG, JPEG, WEBP, TIFF.
        Max file size is configured via MAX_UPLOAD_SIZE_MB env var (default 20 MB).
        """
        settings = get_settings()

        # --- Validation ---
        _validate_file(file, settings)

        # --- Read image ---
        image_bytes = await file.read()
        if not image_bytes:
            raise HTTPException(status_code=400, detail="Empty file uploaded")
        if len(image_bytes) > settings.max_upload_bytes:
            raise HTTPException(
                status_code=413,
                detail=f"File too large: {len(image_bytes)} bytes (max {settings.max_upload_bytes})",
            )

        # --- Determine MIME type ---
        content_type = file.content_type or mimetypes.guess_type(file.filename or "")[0] or "image/png"
        image_format = content_type.split("/")[-1].upper()

        # --- Analyze ---
        svc: LLMService = app.state.llm_service
        if use_mock:
            svc = create_llm_service(use_mock=True)

        try:
            table_data = await svc.analyze(image_bytes, image_format)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc))
        except RuntimeError:
            raise  # Let the exception handler deal with it

        # --- Get image aspect ratio ---
        import io as _io
        from PIL import Image as _PILImage
        try:
            _img = _PILImage.open(_io.BytesIO(image_bytes))
            _ratio = _img.width / _img.height if _img.height > 0 else 0
        except Exception:
            _ratio = 0.0

        # --- Generate Excel ---
        buf = generate_excel(
            table_data,
            sheet_name="Extracted",
            auto_width=auto_width,
            first_row_as_header=first_row_as_header,
            image_aspect_ratio=_ratio,
        )

        filename = _sanitize_filename(file.filename or "table") + ".xlsx"
        return StreamingResponse(
            buf,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={
                "Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}",
            },
        )

    @app.post("/api/upload/batch")
    async def upload_batch(
        files: list[UploadFile] = File(...),
        use_mock: bool = Query(default=False),
    ):
        """Upload multiple table images, get a multi-sheet Excel.

        Each image becomes a separate sheet named after its filename.
        """
        settings = get_settings()
        if len(files) > 10:
            raise HTTPException(status_code=400, detail="Maximum 10 files per batch")

        svc: LLMService = app.state.llm_service
        if use_mock:
            svc = create_llm_service(use_mock=True)

        # Read all files
        images: list[tuple[bytes, str]] = []
        for f in files:
            _validate_file(f, settings)
            content = await f.read()
            if not content:
                raise HTTPException(status_code=400, detail=f"Empty file: {f.filename}")
            if len(content) > settings.max_upload_bytes:
                raise HTTPException(
                    status_code=413,
                    detail=f"File {f.filename} exceeds size limit",
                )
            fmt = (f.content_type or "image/png").split("/")[-1].upper()
            images.append((content, fmt))

        # Analyze all concurrently
        tables = await svc.analyze_multi(images)

        # Build multi-sheet workbook
        sheets: dict[str, any] = {}
        for i, (f, table_data) in enumerate(zip(files, tables)):
            sheet_name = _sanitize_filename(f.filename or f"Sheet{i+1}")[:31]
            sheets[sheet_name] = table_data

        buf = generate_excel_multi_sheet(sheets)
        return StreamingResponse(
            buf,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={
                "Content-Disposition": "attachment; filename*=UTF-8''batch_export.xlsx",
            },
        )

    @app.post("/api/generate")
    async def generate_from_json(body: GenerateRequest):
        """Generate Excel directly from JSON data (no LLM call).

        Useful when you already have structured table data.
        """
        buf = generate_excel_simple(
            headers=body.headers,
            data_rows=body.data_rows,
            sheet_name=body.sheet_name,
            title=body.title,
        )
        filename = quote(body.sheet_name or "export") + ".xlsx"
        return StreamingResponse(
            buf,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={
                "Content-Disposition": f"attachment; filename*=UTF-8''{filename}",
            },
        )

    @app.post("/api/generate/multi")
    async def generate_multi_from_json(body: MultiSheetRequest):
        """Generate multi-sheet Excel from JSON data."""
        buf = generate_excel_multi_sheet(body.sheets, body.title)
        return StreamingResponse(
            buf,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={
                "Content-Disposition": "attachment; filename*=UTF-8''multi_export.xlsx",
            },
        )

    # --- Static files (frontend) ---
    if STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

        @app.get("/", response_class=HTMLResponse)
        async def index():
            """Serve the frontend UI."""
            index_path = STATIC_DIR / "index.html"
            if index_path.exists():
                return HTMLResponse(index_path.read_text(encoding="utf-8"))
            return HTMLResponse("<h1>Frontend not found</h1>", status_code=404)

    return app


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _validate_file(file: UploadFile, settings: Settings) -> None:
    """Validate uploaded file type and basic integrity."""
    if not file.filename:
        raise HTTPException(status_code=400, detail="File must have a filename")

    content_type = file.content_type or ""
    if content_type and content_type not in settings.allowed_image_types:
        raise HTTPException(
            status_code=415,
            detail=f"Unsupported media type: {content_type}. "
            f"Allowed: {', '.join(settings.allowed_image_types)}",
        )


def _sanitize_filename(name: str) -> str:
    """Remove path separators and extensions for safe sheet naming."""
    base = name.rsplit(".", 1)[0] if "." in name else name
    # Remove any path components (Windows or Unix style)
    base = base.replace("\\", "_").replace("/", "_")
    # Remove characters unsafe for sheet names
    unsafe = "[]:*?/"
    for ch in unsafe:
        base = base.replace(ch, "_")
    return base or "table"


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

app = create_app()

if __name__ == "__main__":
    import uvicorn

    settings = get_settings()
    uvicorn.run(
        app,
        host=settings.host,
        port=settings.port,
        reload=settings.debug,
        log_level=settings.log_level.lower(),
    )

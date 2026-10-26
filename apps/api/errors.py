from typing import Any
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException

from domain.schemas import ErrorDetail, ErrorResponse


class APIError(Exception):
    def __init__(self, status: int, code: str, message: str):
        self.status = status
        self.code = code
        self.message = message


def _response(
    request: Request, status: int, code: str, message: str, details: dict[str, Any] | None = None
) -> JSONResponse:
    body = ErrorResponse(
        error=ErrorDetail(code=code, message=message, details=details or {}),
        request_id=request.state.request_id,
    )
    return JSONResponse(status_code=status, content=body.model_dump(mode="json"))


def install_error_handlers(app: FastAPI) -> None:
    @app.middleware("http")
    async def request_context(request: Request, call_next):
        request.state.request_id = uuid4()
        response = await call_next(request)
        response.headers["X-Request-ID"] = str(request.state.request_id)
        return response

    @app.exception_handler(APIError)
    async def api_error(request: Request, exc: APIError):
        return _response(request, exc.status, exc.code, exc.message)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError):
        issues = [
            {"location": list(e["loc"]), "type": e["type"], "message": e["msg"]}
            for e in exc.errors()
        ]
        return _response(request, 422, "VALIDATION_ERROR", "Invalid request", {"issues": issues})

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException):
        return _response(request, exc.status_code, "HTTP_ERROR", str(exc.detail))

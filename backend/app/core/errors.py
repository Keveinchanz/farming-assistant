"""统一响应包与错误处理（任务 4.1：统一接口规范）。

所有接口都返回：
    {"code": 0, "msg": "ok", "data": {...}}     成功
    {"code": 非0, "msg": "中文错误说明", "data": null}   失败

前端只需判断 code 是否为 0。业务异常抛 ApiError，由全局处理器转成上面的结构；
未捕获的异常也会被兜住并返回中文提示，不会把 Python 堆栈直接吐给前端。
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from src.common.logging_utils import setup_logger

logger = setup_logger("agri.api")


class ApiError(Exception):
    """业务异常。code 建议使用下面的常量，前端可按 code 做差异化处理。"""

    def __init__(self, msg: str, code: int = 400, http_status: int = 400) -> None:
        super().__init__(msg)
        self.msg = msg
        self.code = code
        self.http_status = http_status


# 业务错误码
CODE_OK = 0
CODE_BAD_REQUEST = 400
CODE_NOT_FOUND = 404
CODE_PAYLOAD_TOO_LARGE = 413     # 请求体或解码后的文件超过上限
CODE_MODEL_UNAVAILABLE = 1001      # 模型未训练/未导出
CODE_UPSTREAM_ERROR = 1002         # 外部服务（语音识别/大模型）出错
CODE_INTERNAL = 1500


def ok(data: Any = None, msg: str = "ok") -> dict:
    return {"code": CODE_OK, "msg": msg, "data": data}


def fail(msg: str, code: int = CODE_BAD_REQUEST, data: Any = None) -> dict:
    return {"code": code, "msg": msg, "data": data}


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def _api_error(_: Request, exc: ApiError):
        logger.warning("业务异常 code=%s：%s", exc.code, exc.msg)
        return JSONResponse(status_code=exc.http_status, content=fail(exc.msg, exc.code))

    @app.exception_handler(RequestValidationError)
    async def _validation(_: Request, exc: RequestValidationError):
        detail = exc.errors()
        hint = "参数校验失败"
        if detail:
            loc = ".".join(str(x) for x in detail[0].get("loc", []) if x != "body")
            hint = f"参数校验失败：{loc} {detail[0].get('msg', '')}"
        return JSONResponse(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                            content=fail(hint, CODE_BAD_REQUEST))

    @app.exception_handler(StarletteHTTPException)
    async def _http(_: Request, exc: StarletteHTTPException):
        return JSONResponse(status_code=exc.status_code,
                            content=fail(str(exc.detail), exc.status_code))

    @app.exception_handler(FileNotFoundError)
    async def _missing(_: Request, exc: FileNotFoundError):
        # 模型/数据文件缺失是部署期最常见的问题，单独给一个明确的错误码与中文指引
        logger.error("文件缺失：%s", exc)
        return JSONResponse(status_code=503,
                            content=fail(str(exc), CODE_MODEL_UNAVAILABLE))

    @app.exception_handler(Exception)
    async def _unhandled(_: Request, exc: Exception):
        logger.exception("未捕获异常：%s", exc)
        return JSONResponse(status_code=500,
                            content=fail(f"服务器内部错误：{exc}", CODE_INTERNAL))

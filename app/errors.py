"""业务错误类型，由 FastAPI 异常处理器统一转成 HTTP 响应。"""


class ServiceError(Exception):
    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail

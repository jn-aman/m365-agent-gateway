"""Public errors that never include credentials or upstream payloads."""


class GatewayError(Exception):
    def __init__(self, message: str, code: str = "invalid_request", status: int = 400):
        super().__init__(message)
        self.code = code
        self.status = status

"""iterm-xshell: panel kênh SSH + panel duyệt file SFTP cho iTerm2 (giống Xshell/Xftp)."""

__version__ = "0.3.0"


class ApiError(Exception):
    """Lỗi trả về cho UI dưới dạng JSON {error, code}."""

    def __init__(self, status, msg, code=None):
        super().__init__(msg)
        self.status, self.msg, self.code = status, msg, code

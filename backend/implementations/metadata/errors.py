"""Safe provider failures; HTTP details and secrets stay inside clients."""

from typing import Optional

from backend.base.definitions import ApiResponse, KapowarrException


class MetadataProviderError(KapowarrException):
    def __init__(self, provider: str, reason: str, retry_at: Optional[float] = None):
        self.provider = provider
        self.reason = reason
        self.retry_at = retry_at
        super().__init__(provider + ': ' + reason)

    @property
    def api_response(self) -> ApiResponse:
        code = {'credentials': 400, 'forbidden': 403, 'not_found': 404,
                'rate_limited': 429, 'deferred': 429, 'conflict': 409}.get(self.reason, 502)
        return {'code': code, 'error': 'MetadataProviderError', 'result': {
            'provider': self.provider, 'reason': self.reason, 'retry_at': self.retry_at}}

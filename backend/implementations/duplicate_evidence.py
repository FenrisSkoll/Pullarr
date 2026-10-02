"""Explicit selected-file SHA-256 acquisition, not automatic library hashing."""

import os
import stat
from hashlib import sha256
from time import monotonic

from backend.base.duplicate_review import DuplicateReviewError
from backend.base.organization_job import OrganizationError
from backend.implementations.organization_filesystem import safe_path


def stamp(value):
    return (value.st_dev, value.st_ino, value.st_mode, value.st_size,
            value.st_mtime_ns, value.st_ctime_ns)


class HashBudget:
    """Serial, shared byte/deadline budget for one explicit review acquisition."""

    def __init__(self, maximum=1024 * 1024 * 1024, seconds=120, *, cancel=lambda: False,
                 progress=lambda count: None, clock=monotonic):
        if type(maximum) is not int or not 1 <= maximum <= 8 * 1024 * 1024 * 1024 or not 0 < seconds <= 600:
            raise DuplicateReviewError('invalid_hash_budget')
        self.maximum, self.used = maximum, 0
        self.cancel, self.progress, self.clock = cancel, progress, clock
        self.deadline = clock() + seconds

    def check(self):
        if self.cancel():
            raise DuplicateReviewError('duplicate_hash_cancelled')
        if self.clock() >= self.deadline:
            raise DuplicateReviewError('duplicate_hash_deadline')

    def inspect(self, path):
        self.check()
        try:
            safe_path(path)
            before = os.lstat(path)
            if not stat.S_ISREG(before.st_mode) or before.st_ino == 0:
                raise DuplicateReviewError('duplicate_hash_unsafe_source')
            if before.st_size > self.maximum - self.used:
                raise DuplicateReviewError('duplicate_hash_byte_limit')
            digest = sha256()
            with open(path, 'rb') as stream:
                if stamp(os.fstat(stream.fileno())) != stamp(before):
                    raise DuplicateReviewError('duplicate_hash_source_changed')
                while True:
                    self.check()
                    block = stream.read(min(1024 * 1024, self.maximum - self.used + 1))
                    if not block:
                        break
                    self.used += len(block)
                    if self.used > self.maximum:
                        raise DuplicateReviewError('duplicate_hash_byte_limit')
                    digest.update(block)
                    self.progress(self.used)
                if stamp(os.fstat(stream.fileno())) != stamp(before):
                    raise DuplicateReviewError('duplicate_hash_source_changed')
            safe_path(path)
            if stamp(os.lstat(path)) != stamp(before):
                raise DuplicateReviewError('duplicate_hash_source_changed')
            return dict(algorithm='sha256/v1', digest=digest.hexdigest(), size=before.st_size,
                        stamp=stamp(before))
        except (OSError, OrganizationError):
            raise DuplicateReviewError('duplicate_hash_unavailable') from None

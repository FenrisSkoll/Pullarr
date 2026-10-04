"""Journal-owned cross-device Library Import transfer; never overwrite or edit source."""
import os
from pathlib import Path

from backend.base.organization_job import (ExecutionCode,
                                           OrganizationError, StepState)
from backend.implementations.organization_filesystem import (_cancelled,
                                                             artifact, matches,
                                                             safe_path,
                                                             sync_directory)


def requires_copy(source, folder):
    return os.stat(source).st_dev != os.stat(folder).st_dev


def reconcile(intent, value):
    source, target = intent['source'], intent['target']
    if not os.path.lexists(target):
        if not matches(source,value['artifact_before']):
            raise OrganizationError(ExecutionCode.SOURCE)
        return False
    copied = artifact(target)
    allocated = value.get('import_copy_identity')
    if allocated != [copied['device'],copied['inode']] or any(copied[k] != value['artifact_before'][k] for k in ('size','sha256')):
        raise OrganizationError(ExecutionCode.CONFLICT)
    if os.path.lexists(source):
        if not matches(source,value['artifact_before']):
            raise OrganizationError(ExecutionCode.SOURCE)
        return False
    value['artifact_after'] = copied
    return True


def execute(executor, job, intent, ordinal, value):
    source, target = intent['source'], intent['target']
    safe_path(source); safe_path(target)
    if not matches(source,value['artifact_before']):
        raise OrganizationError(ExecutionCode.SOURCE)
    if not os.path.lexists(target):
        with open(source,'rb') as incoming, open(target,'xb') as output:
            opened=os.fstat(incoming.fileno())
            if (opened.st_dev,opened.st_ino) != (value['artifact_before']['device'],value['artifact_before']['inode']):
                raise OrganizationError(ExecutionCode.SOURCE)
            created=os.fstat(output.fileno())
            value['import_copy_identity']=[created.st_dev,created.st_ino]
            with executor.store.transaction():
                executor.store.checkpoint(job.id,ordinal,StepState.STARTED,value)
            while chunk := incoming.read(1024*1024):
                if _cancelled.get()():
                    raise OrganizationError(ExecutionCode.CANCELLED)
                output.write(chunk)
            output.flush(); os.fsync(output.fileno())
        sync_directory(str(Path(target).parent))
        executor.hook('after_import_copy',job.id,ordinal)
    reconcile(intent,value)  # exact owned target payload AND original source
    if _cancelled.get()():
        raise OrganizationError(ExecutionCode.CANCELLED)
    # Copying may take time. Recheck authority and both artifacts while holding
    # the same durable writer exclusion used by ordinary organizer relocation.
    with executor.store.transaction():
        executor._check_database(executor.store.get(job.id), intent)
        reconcile(intent, value)
        os.unlink(source)  # only this directory entry, never shared seed inode bytes
        sync_directory(str(Path(source).parent))
    executor.hook('after_effect',job.id,ordinal)
    if not reconcile(intent,value):
        raise OrganizationError(ExecutionCode.CONFLICT)
    executor._receipt(job.id,ordinal,value)

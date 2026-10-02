"""Explicit preview service: acquire read-only context, observe paths, compile."""

import os
from stat import S_ISDIR, S_ISLNK
from typing import Iterable, Tuple

from backend.base.identification import IdentificationResult
from backend.base.organization_plan import (OrganizationBatch,
                                            PathObservation, PlanningPolicy)
from backend.implementations.organization_plan import (PlanningContext,
                                                       path_key, path_module,
                                                       plan_many)
from backend.internals.organization_plan import load_planning_records


def observe_plan_paths(paths: Iterable[str], policy: PlanningPolicy) -> Tuple[PathObservation, ...]:
    """One lstat per distinct path/ancestor, no traversal or archive opening.

    Decline symlink/reparse paths, including ancestors. Errors stay unknown,
    never absence. Foreign-platform planning requires supplied observations.
    """
    if policy.windows != (os.name == 'nt'):
        raise ValueError('Cannot inspect a foreign filesystem with host path rules')
    mod = path_module(policy)
    cache = {}

    def observe(path: str) -> PathObservation:
        key = path_key(path, policy)
        if key in cache:
            return cache[key]
        parent = mod.dirname(path)
        ancestor = observe(parent) if parent and parent != path else None
        unsafe = bool(ancestor and ancestor.unsafe_link)
        if ancestor and (ancestor.exists is None or ancestor.unsafe_link):
            value = PathObservation(path, None, unsafe_link=unsafe)
        else:
            try:
                info = os.lstat(path)
                unsafe = unsafe or S_ISLNK(info.st_mode) or bool(getattr(info, 'st_file_attributes', 0) & 0x400)
                value = PathObservation(path, True, S_ISDIR(info.st_mode), unsafe, info.st_size, info.st_mtime_ns, info.st_dev)
            except FileNotFoundError:
                value = PathObservation(path, False, unsafe_link=unsafe, device=ancestor.device if ancestor else None)
            except OSError:
                value = PathObservation(path, None, unsafe_link=unsafe)
        cache[key] = value
        return value

    for path in sorted(set(paths)):
        observe(mod.normpath(path))
    return tuple(cache[key] for key in sorted(cache))


def preview_organization(results: Iterable[IdentificationResult],
                         registered_providers: Tuple[str, ...], policy: PlanningPolicy) -> OrganizationBatch:
    """No implicit matching, archive inspection, provider acquisition or apply."""
    inputs = tuple(results)
    volumes, issues, roots, files, naming = load_planning_records(registered_providers)
    context = PlanningContext.build(volumes, issues, roots, files, naming, policy)
    provisional = plan_many(inputs, context)
    paths = {p.source_path for p in provisional.plans}
    for p in provisional.plans:
        paths.update(path for path in (p.target_path, p.target_folder, p.target_root) if path is not None)
    observations = observe_plan_paths(paths, policy)
    context = PlanningContext.build(volumes, issues, roots, files, naming, policy, observations)
    return plan_many(inputs, context)

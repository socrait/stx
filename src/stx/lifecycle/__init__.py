"""The one lifecycle setup and teardown share.

`new`/`refresh` provision through this package; `rm`/`prune` and `new`'s own
rollback tear down through it. Keeping the copies here — instead of open-coded
three times across the commands — is what lets a correctness fix land once
rather than drift between them. See docs/reference.md (Provisioning, Teardown
guards).
"""

from __future__ import annotations

from .owner import current_owner, owner_alive, process_start_time
from .provision import (
    ProvisionOutcome,
    ProvisionState,
    RollbackIncomplete,
    StepAlreadyReported,
    await_box_setup,
    check_repo_compose_contract,
    collect_host_vars,
    provision_new,
    refresh_box,
    repo_compose_config,
)
from .spec import SandboxSpec
from .teardown import (
    ContainerTeardown,
    TeardownPolicy,
    TeardownResult,
    bring_down_containers,
    container_ids_for_slug,
    teardown_sandbox,
)

__all__ = [
    "ContainerTeardown",
    "TeardownPolicy",
    "TeardownResult",
    "ProvisionOutcome",
    "ProvisionState",
    "RollbackIncomplete",
    "SandboxSpec",
    "StepAlreadyReported",
    "await_box_setup",
    "bring_down_containers",
    "check_repo_compose_contract",
    "collect_host_vars",
    "container_ids_for_slug",
    "current_owner",
    "owner_alive",
    "process_start_time",
    "provision_new",
    "refresh_box",
    "repo_compose_config",
    "teardown_sandbox",
]

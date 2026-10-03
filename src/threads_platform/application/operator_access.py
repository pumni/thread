from enum import StrEnum

from threads_platform.domain.operators import OperatorRole


class OperatorAction(StrEnum):
    VIEW_WORKSPACE = "view_workspace"
    VIEW_FLEET = "view_fleet"
    VIEW_ACCOUNTS = "view_accounts"
    VIEW_DIAGNOSTICS = "view_diagnostics"
    MANAGE_OWNER_ADMIN = "manage_owner_admin"
    MANAGE_OPERATOR_VIEWER = "manage_operator_viewer"
    PROVISION_WORKER = "provision_worker"
    DRAIN_WORKER = "drain_worker"
    RESOLVE_INTERVENTION = "resolve_intervention"
    SUBMIT_COMMAND = "submit_command"
    CONTROLLER_LIFECYCLE = "controller_lifecycle"
    WORKER_LIFECYCLE = "worker_lifecycle"


_VIEW_ACTIONS = frozenset(
    {
        OperatorAction.VIEW_WORKSPACE,
        OperatorAction.VIEW_FLEET,
        OperatorAction.VIEW_ACCOUNTS,
        OperatorAction.VIEW_DIAGNOSTICS,
    }
)
_OWNER_ACTIONS = _VIEW_ACTIONS | frozenset(
    {
        OperatorAction.MANAGE_OWNER_ADMIN,
        OperatorAction.MANAGE_OPERATOR_VIEWER,
        OperatorAction.PROVISION_WORKER,
        OperatorAction.DRAIN_WORKER,
        OperatorAction.RESOLVE_INTERVENTION,
        OperatorAction.SUBMIT_COMMAND,
        OperatorAction.CONTROLLER_LIFECYCLE,
        OperatorAction.WORKER_LIFECYCLE,
    }
)

_ROLE_ACTIONS: dict[OperatorRole, frozenset[OperatorAction]] = {
    OperatorRole.OWNER: _OWNER_ACTIONS,
    OperatorRole.ADMIN: _OWNER_ACTIONS - {OperatorAction.MANAGE_OWNER_ADMIN},
    OperatorRole.OPERATOR: frozenset(
        {
            OperatorAction.VIEW_WORKSPACE,
            OperatorAction.VIEW_FLEET,
            OperatorAction.VIEW_ACCOUNTS,
            OperatorAction.VIEW_DIAGNOSTICS,
            OperatorAction.DRAIN_WORKER,
            OperatorAction.RESOLVE_INTERVENTION,
            OperatorAction.SUBMIT_COMMAND,
            OperatorAction.WORKER_LIFECYCLE,
        }
    ),
    OperatorRole.VIEWER: _VIEW_ACTIONS,
}


def role_allows(role: OperatorRole, action: OperatorAction) -> bool:
    return action in _ROLE_ACTIONS.get(role, frozenset())

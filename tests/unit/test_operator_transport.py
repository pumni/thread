import httpx

from threads_platform.app import create_app
from threads_platform.application.operator_access import OperatorAction, role_allows
from threads_platform.config.settings import Settings
from threads_platform.domain.operators import OperatorRole


def test_operator_action_matrix_is_complete_and_fixed() -> None:
    expected = {
        OperatorRole.OWNER: {
            OperatorAction.VIEW_WORKSPACE,
            OperatorAction.VIEW_FLEET,
            OperatorAction.VIEW_ACCOUNTS,
            OperatorAction.VIEW_DIAGNOSTICS,
            OperatorAction.MANAGE_OWNER_ADMIN,
            OperatorAction.MANAGE_OPERATOR_VIEWER,
            OperatorAction.PROVISION_WORKER,
            OperatorAction.DRAIN_WORKER,
            OperatorAction.RESOLVE_INTERVENTION,
            OperatorAction.SUBMIT_COMMAND,
            OperatorAction.CONTROLLER_LIFECYCLE,
            OperatorAction.WORKER_LIFECYCLE,
        },
        OperatorRole.ADMIN: {
            OperatorAction.VIEW_WORKSPACE,
            OperatorAction.VIEW_FLEET,
            OperatorAction.VIEW_ACCOUNTS,
            OperatorAction.VIEW_DIAGNOSTICS,
            OperatorAction.MANAGE_OPERATOR_VIEWER,
            OperatorAction.PROVISION_WORKER,
            OperatorAction.DRAIN_WORKER,
            OperatorAction.RESOLVE_INTERVENTION,
            OperatorAction.SUBMIT_COMMAND,
            OperatorAction.CONTROLLER_LIFECYCLE,
            OperatorAction.WORKER_LIFECYCLE,
        },
        OperatorRole.OPERATOR: {
            OperatorAction.VIEW_WORKSPACE,
            OperatorAction.VIEW_FLEET,
            OperatorAction.VIEW_ACCOUNTS,
            OperatorAction.VIEW_DIAGNOSTICS,
            OperatorAction.DRAIN_WORKER,
            OperatorAction.RESOLVE_INTERVENTION,
            OperatorAction.SUBMIT_COMMAND,
            OperatorAction.WORKER_LIFECYCLE,
        },
        OperatorRole.VIEWER: {
            OperatorAction.VIEW_WORKSPACE,
            OperatorAction.VIEW_FLEET,
            OperatorAction.VIEW_ACCOUNTS,
            OperatorAction.VIEW_DIAGNOSTICS,
        },
    }
    assert set(OperatorAction) == set().union(*expected.values())
    for role in OperatorRole:
        for action in OperatorAction:
            assert role_allows(role, action) is (action in expected[role])


async def test_invalid_operator_password_is_not_echoed() -> None:
    app = create_app(Settings(database_url=None))
    secret = "SYNTHETIC-DO-NOT-ECHO-" + ("x" * 1_100)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="https://operator.test",
    ) as client:
        response = await client.post(
            "/v1/operator/login",
            json={"username": "synthetic-user", "password": secret},
        )
    assert response.status_code == 422
    assert "SYNTHETIC-DO-NOT-ECHO" not in response.text
    assert secret not in response.text

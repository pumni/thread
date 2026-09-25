from threads_platform.infrastructure.persistence.models import OAuthCredentialRecord


def test_oauth_persistence_contains_metadata_but_no_plaintext_token() -> None:
    column_names = set(OAuthCredentialRecord.__table__.columns.keys())

    assert "credential_ref" in column_names
    assert "expires_at" in column_names
    assert "granted_scopes" in column_names
    assert "access_token" not in column_names
    assert "refresh_token" not in column_names

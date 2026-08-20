"""Tests for the Step 2 auth API: setup, local login/logout, /me, the
/auth/admin/* management surface, origin_guard (CSRF), and the OIDC
authorize/callback dance.

Uses the same TestClient/DB wiring as test_api.py (see conftest.py), but
constructs its own fresh `TestClient(app)` per test (see `client` fixture
below) rather than the shared module-level client, so each test gets an
isolated cookie jar -- session/OIDC-state cookies from one test must never
leak into another.
"""

import time

import pytest
from fastapi.testclient import TestClient
from joserfc import jwt as joserfc_jwt
from joserfc.jwk import KeySet, RSAKey
from sqlalchemy import select

import app.api.auth as auth_module
from app.main import app
from app.models.models import AuthProvider, Job, JobStatus, Session as SessionModel, User, UserRole, WorkerLogEntry
from app.services.security import encrypt_client_secret, hash_password, rate_limiter
from conftest import BROWSER_HEADERS, TestingSessionLocal


@pytest.fixture(autouse=True)
def _reset_rate_limiter():
    # /setup, /login, and the OIDC endpoints all call enforce_rate_limit,
    # which is backed by a single shared Redis counter keyed by client host
    # (TestClient always presents as "testclient"). Reset it before every
    # test in this module so test order/volume here can never trip a false
    # 429 for a later test, and so it doesn't inherit noise from test_api.py
    # if these modules run in the same process.
    rate_limiter.reset()
    yield


@pytest.fixture
def client() -> TestClient:
    """A fresh cookie jar per test. `app.dependency_overrides[get_db]` is
    already wired up once, process-wide, in conftest.py."""
    return TestClient(app, headers=BROWSER_HEADERS)


def _db():
    return TestingSessionLocal()


def _wipe_users_and_sessions() -> None:
    db = _db()
    try:
        db.query(SessionModel).delete()
        db.query(User).delete()
        db.commit()
    finally:
        db.close()


def _create_user(
    *,
    username: str,
    email: str,
    password: str | None = 'CorrectHorse1',
    role: UserRole = UserRole.USER,
    is_active: bool = True,
    oidc_provider_id: str | None = None,
    oidc_subject: str | None = None,
) -> User:
    db = _db()
    try:
        user = User(
            username=username,
            email=email,
            password_hash=hash_password(password) if password else None,
            role=role,
            is_active=is_active,
            oidc_provider_id=oidc_provider_id,
            oidc_subject=oidc_subject,
        )
        db.add(user)
        db.commit()
        db.refresh(user)
        db.expunge(user)
        return user
    finally:
        db.close()


def _login(client: TestClient, identifier: str, password: str):
    return client.post('/api/v1/auth/login', json={'identifier': identifier, 'password': password})


# --- setup --------------------------------------------------------------------

def test_setup_status_and_setup_flow_locks_after_first_admin(client: TestClient) -> None:
    _wipe_users_and_sessions()

    status_resp = client.get('/api/v1/auth/setup-status')
    assert status_resp.status_code == 200
    assert status_resp.json() == {'needs_setup': True}

    setup_resp = client.post(
        '/api/v1/auth/setup',
        json={'username': 'RootAdmin', 'email': 'root@example.com', 'password': 'SetupPassw0rd'},
    )
    assert setup_resp.status_code == 200
    body = setup_resp.json()
    assert body['username'] == 'rootadmin'  # stored lowercased
    assert body['role'] == 'admin'
    assert 'paddledoc_session' in client.cookies

    me_resp = client.get('/api/v1/auth/me')
    assert me_resp.status_code == 200
    assert me_resp.json()['role'] == 'admin'

    second_setup = client.post(
        '/api/v1/auth/setup',
        json={'username': 'someoneelse', 'email': 'someone@example.com', 'password': 'AnotherPassw0rd'},
    )
    assert second_setup.status_code == 409

    final_status = client.get('/api/v1/auth/setup-status')
    assert final_status.json() == {'needs_setup': False}


# --- local login ----------------------------------------------------------------

def test_login_success_sets_session_and_me_works(client: TestClient) -> None:
    _create_user(username='loginuser', email='loginuser@example.com', password='CorrectHorse1')

    resp = _login(client, 'loginuser', 'CorrectHorse1')
    assert resp.status_code == 200
    assert resp.json()['username'] == 'loginuser'
    assert 'paddledoc_session' in client.cookies

    me_resp = client.get('/api/v1/auth/me')
    assert me_resp.status_code == 200
    assert me_resp.json()['email'] == 'loginuser@example.com'


def test_login_by_email_case_insensitive(client: TestClient) -> None:
    _create_user(username='mixedcaseuser', email='MixedCase@Example.com', password='CorrectHorse1')

    resp = _login(client, 'MIXEDCASE@EXAMPLE.COM', 'CorrectHorse1')
    assert resp.status_code == 200


def test_login_wrong_password_is_generic_401(client: TestClient) -> None:
    _create_user(username='wrongpassuser', email='wrongpass@example.com', password='CorrectHorse1')

    resp = _login(client, 'wrongpassuser', 'not-the-password')
    assert resp.status_code == 401
    assert 'paddledoc_session' not in client.cookies


def test_login_unknown_identifier_is_generic_401(client: TestClient) -> None:
    resp = _login(client, 'nobody-with-this-username', 'whatever12')
    assert resp.status_code == 401


def test_login_inactive_user_rejected(client: TestClient) -> None:
    _create_user(username='inactiveuser', email='inactive@example.com', password='CorrectHorse1', is_active=False)

    resp = _login(client, 'inactiveuser', 'CorrectHorse1')
    assert resp.status_code == 401


def test_login_oidc_only_account_rejected(client: TestClient) -> None:
    # password=None => OIDC-only account, no local password login possible.
    _create_user(username='oidconlyuser', email='oidconly@example.com', password=None)

    resp = _login(client, 'oidconlyuser', 'anything12')
    assert resp.status_code == 401


# --- logout / me ------------------------------------------------------------------

def test_logout_revokes_session(client: TestClient) -> None:
    _create_user(username='logoutuser', email='logoutuser@example.com', password='CorrectHorse1')
    _login(client, 'logoutuser', 'CorrectHorse1')
    assert client.get('/api/v1/auth/me').status_code == 200

    logout_resp = client.post('/api/v1/auth/logout')
    assert logout_resp.status_code == 200

    # The cookie the client still has on file is now invalid server-side --
    # the session row backing it was deleted, not merely expired locally.
    me_resp = client.get('/api/v1/auth/me')
    assert me_resp.status_code == 401


def test_me_requires_authentication(client: TestClient) -> None:
    resp = client.get('/api/v1/auth/me')
    assert resp.status_code == 401


def test_protected_business_route_requires_authentication(client: TestClient) -> None:
    # The global secure-by-default gate applies to the pre-existing
    # job/folder router too, not just /auth/*.
    resp = client.get('/api/v1/jobs')
    assert resp.status_code == 401


# --- admin CRUD authz -------------------------------------------------------------

def test_admin_users_endpoint_requires_admin_role(client: TestClient) -> None:
    _create_user(username='plainuser', email='plainuser@example.com', password='CorrectHorse1', role=UserRole.USER)
    _login(client, 'plainuser', 'CorrectHorse1')

    resp = client.get('/api/v1/auth/admin/users')
    assert resp.status_code == 403


def test_admin_users_endpoint_works_for_admin(client: TestClient) -> None:
    _create_user(username='workingadmin', email='workingadmin@example.com', password='CorrectHorse1', role=UserRole.ADMIN)
    _login(client, 'workingadmin', 'CorrectHorse1')

    resp = client.get('/api/v1/auth/admin/users')
    assert resp.status_code == 200
    usernames = [item['username'] for item in resp.json()['items']]
    assert 'workingadmin' in usernames


def test_admin_can_create_team_and_assign_user(client: TestClient) -> None:
    _create_user(username='teamadmin', email='teamadmin@example.com', password='CorrectHorse1', role=UserRole.ADMIN)
    target = _create_user(username='teammember', email='teammember@example.com', password='CorrectHorse1')
    _login(client, 'teamadmin', 'CorrectHorse1')

    team_resp = client.post('/api/v1/auth/admin/teams', json={'name': 'Finance Team'})
    assert team_resp.status_code == 201
    team_id = team_resp.json()['id']

    update_resp = client.patch(f'/api/v1/auth/admin/users/{target.id}', json={'team_id': team_id})
    assert update_resp.status_code == 200
    assert update_resp.json()['team_id'] == team_id


def test_admin_cannot_demote_or_delete_last_active_admin(client: TestClient) -> None:
    _wipe_users_and_sessions()
    sole_admin = _create_user(username='soleadmin', email='soleadmin@example.com', password='CorrectHorse1', role=UserRole.ADMIN)
    _login(client, 'soleadmin', 'CorrectHorse1')

    demote_resp = client.patch(f'/api/v1/auth/admin/users/{sole_admin.id}', json={'role': 'user'})
    assert demote_resp.status_code == 409

    deactivate_resp = client.patch(f'/api/v1/auth/admin/users/{sole_admin.id}', json={'is_active': False})
    assert deactivate_resp.status_code == 409

    delete_resp = client.delete(f'/api/v1/auth/admin/users/{sole_admin.id}')
    assert delete_resp.status_code == 409

    # A second active admin makes the demotion/deletion legal again.
    _create_user(username='secondadmin', email='secondadmin@example.com', password='CorrectHorse1', role=UserRole.ADMIN)
    demote_resp_ok = client.patch(f'/api/v1/auth/admin/users/{sole_admin.id}', json={'role': 'user'})
    assert demote_resp_ok.status_code == 200


def test_admin_providers_list_never_exposes_secret(client: TestClient) -> None:
    admin = _create_user(username='provideradmin', email='provideradmin@example.com', password='CorrectHorse1', role=UserRole.ADMIN)
    db = _db()
    try:
        provider = AuthProvider(
            slug='keycloak-secret-test',
            display_name='Keycloak',
            issuer_url='https://idp.example.com',
            client_id='client123',
            client_secret_encrypted=encrypt_client_secret('super-secret-value'),
            enabled=True,
        )
        db.add(provider)
        db.commit()
    finally:
        db.close()
    _login(client, 'provideradmin', 'CorrectHorse1')

    resp = client.get('/api/v1/auth/admin/providers')
    assert resp.status_code == 200
    raw_body = resp.text
    assert 'super-secret-value' not in raw_body
    matching = [item for item in resp.json()['items'] if item['slug'] == 'keycloak-secret-test']
    assert matching and matching[0]['client_secret_set'] is True
    assert 'client_secret' not in matching[0]
    assert 'client_secret_encrypted' not in matching[0]
    del admin  # only needed to create the session above


def test_public_providers_endpoint_hides_disabled_and_secrets(client: TestClient) -> None:
    db = _db()
    try:
        db.add(
            AuthProvider(
                slug='public-enabled-provider',
                display_name='Enabled IdP',
                issuer_url='https://idp.example.com',
                client_id='cid',
                client_secret_encrypted=encrypt_client_secret('shh'),
                enabled=True,
            )
        )
        db.add(
            AuthProvider(
                slug='public-disabled-provider',
                display_name='Disabled IdP',
                issuer_url='https://idp2.example.com',
                client_id='cid2',
                client_secret_encrypted=encrypt_client_secret('shh2'),
                enabled=False,
            )
        )
        db.commit()
    finally:
        db.close()

    resp = client.get('/api/v1/auth/providers')
    assert resp.status_code == 200
    slugs = {item['slug'] for item in resp.json()['items']}
    assert 'public-enabled-provider' in slugs
    assert 'public-disabled-provider' not in slugs
    assert 'shh' not in resp.text


def test_admin_claim_ownerless_jobs(client: TestClient) -> None:
    admin = _create_user(username='claimadmin', email='claimadmin@example.com', password='CorrectHorse1', role=UserRole.ADMIN)
    db = _db()
    try:
        db.add(
            Job(
                id='ownerless-job-1',
                original_filename='legacy.pdf',
                upload_path='/tmp/legacy.pdf',
                status=JobStatus.FINISHED,
                owner_id=None,
            )
        )
        db.commit()
    finally:
        db.close()
    _login(client, 'claimadmin', 'CorrectHorse1')

    resp = client.post('/api/v1/auth/admin/jobs/claim-ownerless', json={'owner_id': admin.id})
    assert resp.status_code == 200
    assert resp.json()['claimed'] >= 1

    db = _db()
    try:
        job = db.get(Job, 'ownerless-job-1')
        assert job.owner_id == admin.id
    finally:
        db.close()


# --- origin_guard (CSRF) -----------------------------------------------------------

def test_origin_guard_rejects_foreign_origin_on_public_login(client: TestClient) -> None:
    resp = client.post(
        '/api/v1/auth/login',
        json={'identifier': 'whoever', 'password': 'whatever12'},
        headers={'Origin': 'https://evil.example'},
    )
    assert resp.status_code == 403


def test_origin_guard_allows_configured_origin(client: TestClient) -> None:
    # Passes origin_guard and reaches the real handler -- proven by getting
    # a 401 (bad credentials) rather than a 403 (blocked origin).
    resp = client.post(
        '/api/v1/auth/login',
        json={'identifier': 'whoever', 'password': 'whatever12'},
        headers={'Origin': 'http://localhost:3000'},
    )
    assert resp.status_code == 401


def test_origin_guard_ignores_get_requests(client: TestClient) -> None:
    resp = client.get('/api/v1/auth/setup-status', headers={'Origin': 'https://evil.example'})
    assert resp.status_code == 200


# --- OIDC ---------------------------------------------------------------------------

def _rsa_keypair_and_jwks():
    key = RSAKey.generate_key(2048, parameters={'kid': 'test-kid'})
    jwks = {'keys': [key.as_dict(private=False)]}
    return key, KeySet.import_key_set(jwks)


def _sign_id_token(key: RSAKey, **claim_overrides) -> str:
    now = int(time.time())
    claims = {
        'iss': 'https://idp.example.com',
        'aud': 'test-client-id',
        'sub': 'idp-subject-1',
        'exp': now + 300,
        'iat': now,
        'email': 'newoidcuser@example.com',
        'preferred_username': 'newoidcuser',
    }
    claims.update(claim_overrides)
    return joserfc_jwt.encode({'alg': 'RS256', 'kid': 'test-kid'}, claims, key)


def _make_oidc_provider(slug: str = 'test-oidc', *, use_email_as_username: bool = False) -> AuthProvider:
    db = _db()
    try:
        provider = AuthProvider(
            slug=slug,
            display_name='Test OIDC',
            issuer_url='https://idp.example.com',
            client_id='test-client-id',
            client_secret_encrypted=encrypt_client_secret('idp-client-secret'),
            enabled=True,
            use_email_as_username=use_email_as_username,
        )
        db.add(provider)
        db.commit()
        db.refresh(provider)
        db.expunge(provider)
        return provider
    finally:
        db.close()


def _oidc_login(client: TestClient, monkeypatch: pytest.MonkeyPatch, slug: str, **claim_overrides):
    """Drives the full authorize -> callback dance for `slug` and returns the
    callback response. `claim_overrides` go straight into the signed ID
    token (see `_sign_id_token`) -- e.g. sub=..., email=..., preferred_username=...
    """
    signing_key, key_set = _rsa_keypair_and_jwks()
    monkeypatch.setattr(auth_module, 'generate_token', lambda length=30: 'fixed-oidc-test-token')
    monkeypatch.setattr(auth_module, 'get_discovery_document', lambda issuer_url: _DISCOVERY_DOCUMENT)

    authorize_resp = client.get(f'/api/v1/auth/oidc/{slug}/authorize', follow_redirects=False)
    assert authorize_resp.status_code == 302

    id_token = _sign_id_token(signing_key, nonce='fixed-oidc-test-token', **claim_overrides)
    monkeypatch.setattr(
        auth_module, 'exchange_code_for_tokens', lambda token_endpoint, **kw: {'id_token': id_token, 'access_token': 'at'}
    )
    monkeypatch.setattr(auth_module, 'fetch_jwks', lambda jwks_uri: key_set)

    return client.get(
        f'/api/v1/auth/oidc/{slug}/callback',
        params={'code': 'auth-code-123', 'state': 'fixed-oidc-test-token'},
        follow_redirects=False,
    )


_DISCOVERY_DOCUMENT = {
    'issuer': 'https://idp.example.com',
    'authorization_endpoint': 'https://idp.example.com/auth',
    'token_endpoint': 'https://idp.example.com/token',
    'jwks_uri': 'https://idp.example.com/jwks',
}


def test_oidc_callback_happy_path_creates_user_and_logs_in(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    _make_oidc_provider('test-oidc-happy-path')
    signing_key, key_set = _rsa_keypair_and_jwks()

    # Fixed state/nonce/code_verifier so the test can pre-compute a signed
    # ID token whose `nonce` claim will match what /authorize embeds in the
    # signed state cookie.
    monkeypatch.setattr(auth_module, 'generate_token', lambda length=30: 'fixed-oidc-test-token')
    monkeypatch.setattr(auth_module, 'get_discovery_document', lambda issuer_url: _DISCOVERY_DOCUMENT)

    authorize_resp = client.get('/api/v1/auth/oidc/test-oidc-happy-path/authorize', follow_redirects=False)
    assert authorize_resp.status_code == 302
    location = authorize_resp.headers['location']
    assert location.startswith('https://idp.example.com/auth?')
    assert 'paddledoc_oidc_state' in client.cookies

    id_token = _sign_id_token(signing_key, nonce='fixed-oidc-test-token')
    monkeypatch.setattr(
        auth_module, 'exchange_code_for_tokens', lambda token_endpoint, **kw: {'id_token': id_token, 'access_token': 'at'}
    )
    monkeypatch.setattr(auth_module, 'fetch_jwks', lambda jwks_uri: key_set)

    callback_resp = client.get(
        '/api/v1/auth/oidc/test-oidc-happy-path/callback',
        params={'code': 'auth-code-123', 'state': 'fixed-oidc-test-token'},
        follow_redirects=False,
    )
    assert callback_resp.status_code == 302
    assert 'paddledoc_session' in client.cookies

    me_resp = client.get('/api/v1/auth/me')
    assert me_resp.status_code == 200
    me_body = me_resp.json()
    assert me_body['email'] == 'newoidcuser@example.com'
    assert me_body['username'] == 'newoidcuser'

    db = _db()
    try:
        created = db.scalar(select(User).where(User.oidc_subject == 'idp-subject-1'))
        assert created is not None
        assert created.password_hash is None
        assert created.role == UserRole.USER
    finally:
        db.close()


def test_oidc_callback_rejects_idp_initiated_login(client: TestClient) -> None:
    _make_oidc_provider()
    # No prior call to /authorize -- no OIDC state cookie is present, which
    # is exactly the IdP-initiated pattern this must fail closed on.
    resp = client.get(
        '/api/v1/auth/oidc/test-oidc/callback',
        params={'code': 'whatever', 'state': 'whatever'},
        follow_redirects=False,
    )
    assert resp.status_code == 400


def test_oidc_authorize_unknown_provider_404(client: TestClient) -> None:
    resp = client.get('/api/v1/auth/oidc/does-not-exist/authorize', follow_redirects=False)
    assert resp.status_code == 404


# --- OIDC: use_email_as_username -----------------------------------------------------

def test_oidc_callback_use_email_as_username_provisions_with_email(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Switch on + a real email claim: the new user's username is the full
    email address, not the (UPN-shaped) preferred_username."""
    _make_oidc_provider('test-oidc-email-username', use_email_as_username=True)
    resp = _oidc_login(
        client,
        monkeypatch,
        'test-oidc-email-username',
        sub='idp-subject-email-username',
        email='entrauser@example.com',
        preferred_username='entrauser-upn-garbage',
    )
    assert resp.status_code == 302

    db = _db()
    try:
        created = db.scalar(select(User).where(User.oidc_subject == 'idp-subject-email-username'))
        assert created is not None
        assert created.email == 'entrauser@example.com'
        assert created.username == 'entrauser@example.com'
    finally:
        db.close()


def test_oidc_callback_use_email_as_username_off_keeps_preferred_username(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression guard: switch off (the default) keeps the pre-existing
    preferred_username-based derivation untouched."""
    _make_oidc_provider('test-oidc-username-off', use_email_as_username=False)
    resp = _oidc_login(
        client,
        monkeypatch,
        'test-oidc-username-off',
        sub='idp-subject-username-off',
        email='someone@example.com',
        preferred_username='someoneupn',
    )
    assert resp.status_code == 302

    db = _db()
    try:
        created = db.scalar(select(User).where(User.oidc_subject == 'idp-subject-username-off'))
        assert created is not None
        assert created.username == 'someoneupn'
    finally:
        db.close()


def test_oidc_callback_use_email_as_username_renames_existing_user_on_login(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Switch on: an existing user's username is nudged to their current
    email claim on login, not just at first provisioning."""
    provider = _make_oidc_provider('test-oidc-rename', use_email_as_username=True)
    _create_user(
        username='old-upn-username',
        email='renameuser@example.com',
        password=None,
        oidc_provider_id=provider.id,
        oidc_subject='idp-subject-rename',
    )
    resp = _oidc_login(
        client,
        monkeypatch,
        'test-oidc-rename',
        sub='idp-subject-rename',
        email='renameuser@example.com',
        preferred_username='old-upn-username',
    )
    assert resp.status_code == 302

    db = _db()
    try:
        user = db.scalar(select(User).where(User.oidc_subject == 'idp-subject-rename'))
        assert user is not None
        assert user.username == 'renameuser@example.com'
    finally:
        db.close()


def test_oidc_callback_use_email_as_username_skips_rename_on_collision(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A different, unrelated user already owns the email-shaped username:
    the rename must be skipped silently and login must still succeed."""
    provider = _make_oidc_provider('test-oidc-collision', use_email_as_username=True)
    other_user = _create_user(username='collideuser@example.com', email='someoneelse@example.com', password=None)
    renaming_user = _create_user(
        username='old-username',
        email='collideuser@example.com',
        password=None,
        oidc_provider_id=provider.id,
        oidc_subject='idp-subject-collision',
    )
    resp = _oidc_login(
        client,
        monkeypatch,
        'test-oidc-collision',
        sub='idp-subject-collision',
        email='collideuser@example.com',
        preferred_username='old-username',
    )
    assert resp.status_code == 302
    assert 'paddledoc_session' in client.cookies

    db = _db()
    try:
        renamed = db.get(User, renaming_user.id)
        assert renamed is not None
        assert renamed.username == 'old-username'
        unaffected = db.get(User, other_user.id)
        assert unaffected is not None
        assert unaffected.username == 'collideuser@example.com'
    finally:
        db.close()


def test_oidc_callback_use_email_as_username_ignores_synthetic_fallback_email_on_provisioning(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No `email` claim at all: provisioning still synthesizes
    `sub@slug.oidc.invalid` as the user's *email* (existing behavior), but
    must never use that synthetic address as the username -- even with the
    switch on it falls back to preferred_username, same as the switch-off
    case."""
    _make_oidc_provider('test-oidc-synthetic-email', use_email_as_username=True)
    resp = _oidc_login(
        client,
        monkeypatch,
        'test-oidc-synthetic-email',
        sub='idp-subject-synthetic-email',
        email=None,
        preferred_username='realupnuser',
    )
    assert resp.status_code == 302

    db = _db()
    try:
        created = db.scalar(select(User).where(User.oidc_subject == 'idp-subject-synthetic-email'))
        assert created is not None
        assert created.email == 'idp-subject-synthetic-email@test-oidc-synthetic-email.oidc.invalid'
        assert created.username == 'realupnuser'
    finally:
        db.close()


def test_oidc_callback_use_email_as_username_no_email_claim_skips_rename_on_login(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Existing user, switch on, but this login's ID token carries no email
    claim: must not rename onto a synthesized address, must not rename at
    all."""
    provider = _make_oidc_provider('test-oidc-no-email-claim', use_email_as_username=True)
    _create_user(
        username='kept-username',
        email='keptuser@example.com',
        password=None,
        oidc_provider_id=provider.id,
        oidc_subject='idp-subject-no-email-claim',
    )
    resp = _oidc_login(
        client,
        monkeypatch,
        'test-oidc-no-email-claim',
        sub='idp-subject-no-email-claim',
        email=None,
        preferred_username='old-username',
    )
    assert resp.status_code == 302

    db = _db()
    try:
        user = db.scalar(select(User).where(User.oidc_subject == 'idp-subject-no-email-claim'))
        assert user is not None
        assert user.username == 'kept-username'
    finally:
        db.close()


# --- admin: worker logs -------------------------------------------------------

def test_admin_worker_logs_endpoint_requires_admin_role(client: TestClient) -> None:
    _create_user(username='plainuser2', email='plainuser2@example.com', password='CorrectHorse1', role=UserRole.USER)
    _login(client, 'plainuser2', 'CorrectHorse1')

    resp = client.get('/api/v1/auth/admin/worker-logs')
    assert resp.status_code == 403


def test_admin_worker_logs_filters_by_level_floor_and_worker(client: TestClient) -> None:
    _create_user(username='logsadmin', email='logsadmin@example.com', password='CorrectHorse1', role=UserRole.ADMIN)
    db = _db()
    try:
        db.add_all([
            WorkerLogEntry(
                level='INFO', logger_name='app.workers.tasks', worker_name='worker-a',
                message='Task process_job[1] succeeded',
            ),
            WorkerLogEntry(
                level='WARNING', logger_name='app.workers.tasks', worker_name='worker-a',
                message='Stopping redelivered job',
            ),
            WorkerLogEntry(
                level='ERROR', logger_name='billiard.pool', worker_name='worker-b',
                message="Process 'ForkPoolWorker-1' pid:47 exited with 'signal 9 (SIGKILL)'",
            ),
        ])
        db.commit()
    finally:
        db.close()
    _login(client, 'logsadmin', 'CorrectHorse1')

    resp = client.get('/api/v1/auth/admin/worker-logs')
    assert resp.status_code == 200
    body = resp.json()
    assert body['total'] == 3
    assert len(body['items']) == 3
    # newest first
    assert body['items'][0]['message'].startswith("Process 'ForkPoolWorker-1'")

    resp = client.get('/api/v1/auth/admin/worker-logs', params={'level': 'WARNING'})
    assert resp.status_code == 200
    body = resp.json()
    assert body['total'] == 2
    assert {item['level'] for item in body['items']} == {'WARNING', 'ERROR'}

    resp = client.get('/api/v1/auth/admin/worker-logs', params={'worker': 'worker-b'})
    assert resp.status_code == 200
    body = resp.json()
    assert body['total'] == 1
    assert body['items'][0]['worker_name'] == 'worker-b'

    resp = client.get('/api/v1/auth/admin/worker-logs', params={'q': 'SIGKILL'})
    assert resp.status_code == 200
    assert resp.json()['total'] == 1

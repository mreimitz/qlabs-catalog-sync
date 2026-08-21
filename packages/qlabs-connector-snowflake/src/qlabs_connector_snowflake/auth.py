"""Snowflake auth + config.

WP6 / T6.1 (Sonnet). Key-pair JWT auth (RS-05 section 3.2) and connector config, built
on the SDK's ``auth``/``http`` helpers.

Design decisions:

* **``SnowflakeConfig`` lives here, not in a separate ``config.py``** — the board gives
  this task no ``config.py`` file, so the ``ConnectorConfig`` subclass is declared in
  this module (mirroring ``qlabs_connector_databricks.config.DatabricksConfig`` in every
  other respect: field shape, validators, secrecy).
* **The JWT is minted with the SDK's own ``JWTAuthProvider``, not hand-rolled.** The
  SDK's ``auth.py`` module docstring already names this exact scenario:
  "Snowflake key-pair auth is the eventual consumer" of ``JWTAuthProvider``'s
  direct-use mode (no ``token_url``: the signed assertion *is* the bearer token, which
  is exactly RS-05 section 3.2's ``KEYPAIR_JWT`` flow — no token exchange, no request-a-
  token-then-use-it-elsewhere-round-trip). ``JWTAuthProvider`` already gives this
  connector everything the task asks for "for free": lazy minting, in-memory caching,
  refresh-before-expiry with a safety margin, and an injectable ``Clock`` for
  deterministic tests (:class:`~qlabs_catalog_sync_sdk.config.ManualClock`) — all
  inherited from :class:`~qlabs_catalog_sync_sdk.auth.AuthProvider`. Hand-rolling a
  second cache on top of that would just be a second, untested copy of the same logic.
* **``snowflake.connector.auth.AuthByKeyPair`` supplies exactly one thing this module
  cannot get more safely any other way: the public-key fingerprint.**
  :func:`compute_public_key_fingerprint` delegates to
  ``AuthByKeyPair.calculate_public_key_fingerprint`` — a SHA-256 hash of the
  DER-encoded ``SubjectPublicKeyInfo``, base64-encoded and ``SHA256:``-prefixed
  (RS-05 section 3.2) — rather than reimplementing that exact byte-for-byte encoding
  ourselves and risking a fingerprint Snowflake's server-side ``RSA_PUBLIC_KEY_FP``
  comparison silently rejects. This is the one piece of ``AuthByKeyPair`` this module
  actually calls; the JWT itself is minted by ``JWTAuthProvider`` (see above) because
  ``AuthByKeyPair.prepare()`` reads real wall-clock time internally
  (``datetime.now(timezone.utc)``, confirmed by reading the installed package) and has
  no clock-injection seam, which would make the "accept an injectable clock" and
  "refresh before expiry" requirements untestable without real sleeping.
* **Wire shape (RS-05 section 3.2).** Every request carries ``Authorization: Bearer
  <jwt>`` *and* ``X-Snowflake-Authorization-Token-Type: KEYPAIR_JWT``. The provider
  built here only supplies the first header (that is what every :class:`AuthProvider`
  does); the second is a static, connector-wide header wired onto the
  :class:`~qlabs_catalog_sync_sdk.http.HttpEndpoint` in ``__init__.py``, not something
  the auth provider itself carries.
* **Lifetime cap.** RS-05 section 3.2: "A JWT is valid at most one hour."
  :func:`build_key_pair_jwt_provider` rejects a ``lifetime`` above
  :data:`SNOWFLAKE_JWT_MAX_LIFETIME` before ever touching the private key, and
  :data:`DEFAULT_JWT_LIFETIME` (59 minutes) stays safely under the cap with a margin
  for clock skew between this process and Snowflake's.
* **Error mapping.** Every Snowflake surface this connector talks to (the SQL REST API,
  the resource REST APIs) is reached through :class:`~qlabs_catalog_sync_sdk.http.
  HttpEndpoint`, which — per its own module docstring — raises plain
  ``httpx.HTTPStatusError``/``httpx.TransportError`` rather than SDK-typed exceptions.
  :func:`translate_snowflake_error` is this connector's layer on top, switching on
  ``exc.response.status_code`` exactly the way ``http.py``'s docstring anticipates:
  401/403 -> :class:`AuthError` (not retryable; the engine quarantines the endpoint),
  404 -> :class:`NotFound`, 409 -> :class:`ConflictError`, 5xx/transport failures and
  the passing 4xx conditions in :data:`_TRANSIENT_4XX` (408/423/425/429) ->
  :class:`TransientError` (retryable, honoring a ``Retry-After`` delta-seconds hint when
  Snowflake sends one), and any other 4xx -> :class:`CapabilityError` as the honest
  fallback for "the request itself was rejected as invalid" (Snowflake's SQL REST API
  returns 400 for a malformed/unsupported statement) rather than silently folding it
  into "transient" the way an unmapped status would be tempting to do.
  TENANT-UNVERIFIED: there is no live Snowflake tenant for this build (see the agent
  guide's escalation section) — the exact error JSON payload shapes, and whether every
  4xx this connector could realistically encounter fits the "capability mismatch" bucket
  rather than something needing its own case, are unconfirmed against a real tenant and
  are called out in this task's report.
"""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING

import httpx
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.serialization import load_pem_private_key
from pydantic import Field, SecretStr, field_validator, model_validator
from snowflake.connector.auth import AuthByKeyPair

from qlabs_catalog_sync_sdk.auth import Clock as AuthClock
from qlabs_catalog_sync_sdk.auth import JWTAuthProvider
from qlabs_catalog_sync_sdk.config import ConnectorConfig
from qlabs_catalog_sync_sdk.exceptions import (
    AuthError,
    CapabilityError,
    ConflictError,
    ConnectorError,
    NotFound,
    TransientError,
)

if TYPE_CHECKING:
    from cryptography.hazmat.primitives.asymmetric.types import PrivateKeyTypes

__all__ = [
    "DEFAULT_JWT_LIFETIME",
    "KEYPAIR_JWT_TOKEN_TYPE",
    "SNOWFLAKE_JWT_MAX_LIFETIME",
    "AuthClock",
    "SnowflakeConfig",
    "build_key_pair_jwt_provider",
    "compute_public_key_fingerprint",
    "load_private_key",
    "translate_snowflake_error",
]

#: RS-05 section 3.2: "A JWT is valid at most one hour." Enforced in
#: :func:`build_key_pair_jwt_provider` before any key material is touched.
SNOWFLAKE_JWT_MAX_LIFETIME = timedelta(hours=1)

#: A safe default under the one-hour cap, leaving margin for clock skew between this
#: process and Snowflake's own clock.
DEFAULT_JWT_LIFETIME = timedelta(minutes=59)

#: RS-05 section 3.2: the header that tells Snowflake the bearer token is a key-pair
#: JWT assertion rather than an OAuth/PAT/WIF token. Wired onto the connector's
#: ``HttpEndpoint`` in ``__init__.py`` as a static header, not part of the auth
#: provider's per-request headers.
KEYPAIR_JWT_TOKEN_TYPE = "KEYPAIR_JWT"


# --------------------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------------------


class SnowflakeConfig(ConnectorConfig):
    """Organization/account/user identity, the key-pair credential, and connection
    defaults for one Snowflake endpoint.

    Loaded via :meth:`~qlabs_catalog_sync_sdk.config.ConnectorConfig.for_endpoint`, so a
    deployment configures this through ``<ENDPOINT>__ORGANIZATION``,
    ``<ENDPOINT>__ACCOUNT``, ``<ENDPOINT>__USER``, ``<ENDPOINT>__PRIVATE_KEY``, and the
    optional ``<ENDPOINT>__PRIVATE_KEY_PASSPHRASE`` / ``<ENDPOINT>__ROLE`` /
    ``<ENDPOINT>__WAREHOUSE`` / ``<ENDPOINT>__BASE_URL`` — never read from
    ``os.environ`` directly by connector code.
    """

    organization: str = Field(
        min_length=1,
        description="The Snowflake organization name, e.g. 'myorg' in 'myorg-myaccount'.",
    )
    account: str = Field(
        min_length=1,
        description="The Snowflake account name within the organization, e.g. 'myaccount'.",
    )
    user: str = Field(
        min_length=1,
        description="The Snowflake user (service account) this connector authenticates as.",
    )
    private_key: SecretStr = Field(
        description=(
            "The user's RSA private key, PEM-encoded (PKCS#8), matching a public key "
            "already assigned to the user (`ALTER USER ... SET RSA_PUBLIC_KEY = ...`). "
            "May itself be encrypted; pair with `private_key_passphrase` if so."
        )
    )
    private_key_passphrase: SecretStr | None = Field(
        default=None,
        description="Passphrase for an encrypted private_key. Omit for an unencrypted key.",
    )
    role: str | None = Field(
        default=None,
        description=(
            "Optional Snowflake role to assume for reads. Omit to use the user's default role."
        ),
    )
    warehouse: str | None = Field(
        default=None,
        description=(
            "Optional virtual warehouse for SQL REST API statements that need compute "
            "(RS-05 section 3.8). Metadata-only calls this connector makes do not "
            "require one; see manifest.py for which reads are unconditional."
        ),
    )
    base_url: str | None = Field(
        default=None,
        description=(
            "Override for the account base URL. Derived as "
            "'https://<organization>-<account>.snowflakecomputing.com' when omitted "
            "(RS-05 section 3.1)."
        ),
    )
    account_usage_safety_margin_seconds: int = Field(
        default=10_800,
        ge=0,
        description=(
            "How far behind the account clock a change-detection watermark is allowed to "
            "advance, in seconds. SNOWFLAKE.ACCOUNT_USAGE lags (RS-05 section 1.4: up to "
            "roughly two hours for many views, worse for some), and a watermark advanced "
            "to 'now' against a lagging view silently loses every change that had not yet "
            "propagated. The 3-hour default is deliberately pessimistic: assuming too "
            "little loses changes silently, assuming too much only widens a query. Raise "
            "it if a tenant measures a worse lag. Must match read.py's "
            "DEFAULT_WATERMARK_SAFETY_MARGIN, which test_lag_settings.py pins."
        ),
    )
    rescan_overlap_seconds: int = Field(
        default=900,
        ge=0,
        description=(
            "How far below the stored watermark each change-detection poll re-scans, in "
            "seconds. Re-delivery is harmless -- the poll compares every re-scanned row "
            "against a per-object checksum carried in the watermark -- so this costs a "
            "wider query rather than duplicate work, and it absorbs the boundary case "
            "the safety margin alone does not. Must match read.py's "
            "DEFAULT_RESCAN_OVERLAP, which test_lag_settings.py pins."
        ),
    )

    @field_validator("organization", "account", "user")
    @classmethod
    def _reject_blank_identifier(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("must not be blank")
        return value

    @field_validator("role", "warehouse")
    @classmethod
    def _reject_blank_optional(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("must not be blank; omit the field instead")
        return value

    @field_validator("private_key")
    @classmethod
    def _reject_non_pem_private_key(cls, value: SecretStr) -> SecretStr:
        pem = value.get_secret_value().strip()
        if not pem:
            raise ValueError("private_key must not be blank")
        if "PRIVATE KEY" not in pem:
            raise ValueError(
                "private_key must be a PEM-encoded private key "
                "(missing a '-----BEGIN ... PRIVATE KEY-----' header)"
            )
        return value

    @field_validator("base_url")
    @classmethod
    def _validate_base_url(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value:
            raise ValueError("base_url must not be blank; omit the field instead")
        if not value.startswith("https://"):
            raise ValueError(f"base_url must be an https:// URL (got {value!r})")
        return value.rstrip("/")

    @model_validator(mode="after")
    def _fill_default_base_url(self) -> SnowflakeConfig:
        if self.base_url is None:
            self.base_url = self._derive_base_url()
        return self

    def _derive_base_url(self) -> str:
        """``https://<org>-<account>.snowflakecomputing.com`` (RS-05 section 3.1)."""
        return f"https://{self.organization.lower()}-{self.account.lower()}.snowflakecomputing.com"

    @property
    def resolved_base_url(self) -> str:
        """The effective account base URL: the explicit override, or the derived default.

        Never ``None`` after validation — :meth:`_fill_default_base_url` guarantees
        :attr:`base_url` is always populated by the time a config instance exists.
        """
        assert self.base_url is not None  # noqa: S101 - guaranteed by the model validator
        return self.base_url

    @property
    def account_identifier(self) -> str:
        """The upper-cased ``<ORG>-<ACCOUNT>`` identifier the JWT ``iss``/``sub`` claims
        use (RS-05 section 3.2: "Snowflake upper-cases unquoted identifiers").

        TENANT-UNVERIFIED: RS-05 paraphrases the claim shape as ``<account>.<user>``
        without specifying whether "account" there is the bare account name or the
        hyphenated org-account identifier used for the connection URL. This connector
        uses the hyphenated org-account form — consistent with what the account base
        URL and the value an operator would pass as "account" to a Snowflake session
        both already are — and flags the choice as unconfirmed against a real tenant.
        """
        return f"{self.organization}-{self.account}".upper()

    @property
    def user_identifier(self) -> str:
        """The upper-cased user name the JWT ``iss``/``sub`` claims use."""
        return self.user.upper()


# --------------------------------------------------------------------------------------
# Key-pair JWT
# --------------------------------------------------------------------------------------


def load_private_key(config: SnowflakeConfig, *, endpoint: str) -> PrivateKeyTypes:
    """Parse :attr:`SnowflakeConfig.private_key` into a ``cryptography`` key object.

    Raises :class:`AuthError` (never a raw ``cryptography``/``ValueError`` exception) on
    a malformed key or a wrong/missing passphrase, matching the DoD's "bad key" case —
    an operator sees the SDK's own typed exception, not a vendor-library traceback.
    """
    pem_bytes = config.private_key.get_secret_value().encode("utf-8")
    passphrase = (
        config.private_key_passphrase.get_secret_value().encode("utf-8")
        if config.private_key_passphrase is not None
        else None
    )
    try:
        return load_pem_private_key(pem_bytes, password=passphrase)
    except (ValueError, TypeError) as exc:
        raise AuthError(
            "Snowflake private key could not be loaded: invalid PEM data, unsupported "
            "key type, or a wrong/missing passphrase",
            endpoint=endpoint,
            cause=exc,
        ) from exc


def compute_public_key_fingerprint(private_key: PrivateKeyTypes) -> str:
    """The ``SHA256:<base64>`` public-key fingerprint Snowflake expects in ``iss``.

    Delegates to ``AuthByKeyPair.calculate_public_key_fingerprint`` — see the module
    docstring for why this is the one piece of ``AuthByKeyPair`` this module calls.
    """
    # snowflake-connector-python ships no inline type annotations/py.typed marker for
    # this staticmethod, so mypy sees it as untyped; the return is a plain `str` by
    # reading the implementation (a "SHA256:" + base64 string).
    fingerprint: str = AuthByKeyPair.calculate_public_key_fingerprint(  # type: ignore[no-untyped-call]
        private_key
    )
    return fingerprint


def build_key_pair_jwt_provider(
    config: SnowflakeConfig,
    *,
    endpoint: str,
    lifetime: timedelta = DEFAULT_JWT_LIFETIME,
    clock: AuthClock | None = None,
) -> JWTAuthProvider:
    """Build the key-pair JWT auth provider for ``config`` (RS-05 section 3.2).

    Loads and validates the private key, computes its fingerprint via
    :func:`compute_public_key_fingerprint`, builds the ``iss``/``sub`` claims from the
    upper-cased account/user identifiers, and returns a
    :class:`~qlabs_catalog_sync_sdk.auth.JWTAuthProvider` in direct-use mode (no
    ``token_url``: the signed assertion is sent as the bearer token as-is, matching
    ``KEYPAIR_JWT`` — there is no token-exchange round trip for this auth type).

    Raises ``ValueError`` if ``lifetime`` exceeds :data:`SNOWFLAKE_JWT_MAX_LIFETIME`,
    checked *before* touching the private key so a misconfigured lifetime fails cheaply.
    Raises :class:`AuthError` (via :func:`load_private_key`) for a bad key/passphrase.
    """
    if lifetime > SNOWFLAKE_JWT_MAX_LIFETIME:
        raise ValueError(
            f"Snowflake key-pair JWT lifetime must not exceed {SNOWFLAKE_JWT_MAX_LIFETIME} "
            f"(RS-05 section 3.2: 'A JWT is valid at most one hour'); got {lifetime}"
        )

    private_key = load_private_key(config, endpoint=endpoint)
    if not isinstance(private_key, rsa.RSAPrivateKey):
        raise AuthError(
            f"Snowflake private key must be an RSA key for RS256 key-pair JWT auth; "
            f"got {type(private_key).__name__}",
            endpoint=endpoint,
        )

    fingerprint = compute_public_key_fingerprint(private_key)
    account_identifier = config.account_identifier
    user_identifier = config.user_identifier
    issuer = f"{account_identifier}.{user_identifier}.{fingerprint}"
    subject = f"{account_identifier}.{user_identifier}"

    return JWTAuthProvider(
        private_key=config.private_key.get_secret_value(),
        issuer=issuer,
        subject=subject,
        algorithm="RS256",
        assertion_ttl=lifetime,
        clock=clock,
    )


# --------------------------------------------------------------------------------------
# Error translation
# --------------------------------------------------------------------------------------


def _extract_error_detail(response: httpx.Response) -> str:
    """A short, human-readable detail string from a Snowflake REST API error body.

    Snowflake's REST error bodies are JSON objects carrying (at least) a ``message`` and
    often a ``code`` (RS-05 does not pin the exact shape down further — see the module
    docstring's TENANT-UNVERIFIED note). Falls back to raw response text, then to a bare
    status line, so this never raises on an unexpected body shape.
    """
    try:
        body = response.json()
    except ValueError:
        text = response.text.strip()
        return text[:200] if text else f"HTTP {response.status_code}"
    if isinstance(body, dict):
        message = body.get("message")
        if isinstance(message, str) and message:
            code = body.get("code")
            return f"{message} (code={code})" if code else message
    text = response.text.strip()
    return text[:200] if text else f"HTTP {response.status_code}"


def _parse_retry_after_seconds(response: httpx.Response) -> float | None:
    """Delta-seconds ``Retry-After`` parsing only.

    RS-05 does not document Snowflake's ``Retry-After`` format (HTTP-date vs
    delta-seconds); delta-seconds is the common case and is what this parses.
    TENANT-UNVERIFIED: the HTTP-date form is not handled here.
    """
    header = response.headers.get("retry-after")
    if not header:
        return None
    try:
        return max(float(header), 0.0)
    except ValueError:
        return None


#: 4xx statuses that describe a passing condition rather than a rejected request, so
#: they must not be folded into the generic "the plan was wrong" bucket below. The SDK's
#: ``HttpEndpoint`` only auto-retries 429 and 5xx, so anything listed here arrives
#: un-retried and would otherwise be written off permanently: 408 Request Timeout and
#: 425 Too Early are the client-side analogues of a 503, and 423 Locked reports an object
#: another session is holding, which clears on its own.
_TRANSIENT_4XX = frozenset({408, 423, 425, 429})


def translate_snowflake_error(
    exc: httpx.HTTPStatusError | httpx.TransportError,
    *,
    endpoint: str,
    entity_type: str | None = None,
) -> ConnectorError:
    """Map an ``HttpEndpoint``-raised error onto an SDK typed exception.

    See the module docstring's "Error mapping" section for the full status-code table
    and the TENANT-UNVERIFIED caveat on exact payload shapes.
    """
    if isinstance(exc, httpx.HTTPStatusError):
        response = exc.response
        status = response.status_code
        detail = _extract_error_detail(response)
        message = f"Snowflake request failed ({status}): {detail}"

        if status in (401, 403):
            return AuthError(message, endpoint=endpoint, entity_type=entity_type, cause=exc)
        if status == 404:
            return NotFound(message, endpoint=endpoint, entity_type=entity_type, cause=exc)
        if status == 409:
            return ConflictError(message, endpoint=endpoint, entity_type=entity_type, cause=exc)
        if status in _TRANSIENT_4XX:
            return TransientError(
                message,
                endpoint=endpoint,
                entity_type=entity_type,
                cause=exc,
                retry_after_seconds=_parse_retry_after_seconds(response),
            )
        if status >= 500:
            return TransientError(message, endpoint=endpoint, entity_type=entity_type, cause=exc)
        # Any other 4xx (400 Bad Request is the common shape for a malformed or
        # unsupported SQL REST API statement) is treated as a capability mismatch —
        # the request itself was rejected as invalid, not merely unauthenticated,
        # missing, conflicting, or overloaded.
        return CapabilityError(
            message, endpoint=endpoint, entity_type=entity_type, operation="request"
        )

    return TransientError(
        f"Snowflake request failed before a response was received: {exc}",
        endpoint=endpoint,
        entity_type=entity_type,
        cause=exc,
    )

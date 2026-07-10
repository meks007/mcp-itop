"""
Authentication: OIDC JWT verifier and per-request iTop token resolver.

Flow:
  1. MCP client sends Authorization: Bearer <JWT> (issued by Keycloak/Entra/...).
  2. OAuthTokenVerifier.verify_token() validates the JWT:
       - fetches the OIDC discovery document to resolve the real JWKS URI
       - fetches/caches signing keys from that JWKS URI via PyJWKClient
       - verifies signature, iss, aud, exp
       - extracts UPN from the configured claim
  3. The UPN is stored in AccessToken.client_id (SDK convention for passing
     identity through the auth context).
  4. get_itop_token() reads the UPN from the auth context and looks up the
     corresponding iTop REST API token in the TokenStore.
  5. client.py uses that token as auth_token on every iTop REST call.
"""

from __future__ import annotations

import ssl

import httpx
import jwt
from jwt import PyJWKClient

from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.provider import AccessToken, TokenVerifier

from config import logger
from oauth_config import oauth_cfg
from token_store import TokenStore

# -- Shared JWKS client (singleton) ---------------------------------------
# PyJWKClient expects the JWKS URI directly (the endpoint that returns
# {"keys": [...]}). We resolve it by fetching the OIDC discovery document
# at <issuer_url>/.well-known/openid-configuration and reading its
# "jwks_uri" field. This works for any OIDC-compliant provider
# (Keycloak, Entra ID, etc.) without hardcoding provider-specific paths.

_jwks_client: PyJWKClient | None = None
_token_store: TokenStore | None = None


def _resolve_jwks_uri(issuer_url: str, verify_ssl: bool) -> str:
    """Fetch the OIDC discovery document and return the jwks_uri value."""
    discovery_url = f"{issuer_url}/.well-known/openid-configuration"
    try:
        response = httpx.get(discovery_url, verify=verify_ssl, timeout=10)
        response.raise_for_status()
    except httpx.HTTPError as exc:
        raise RuntimeError(
            f"Failed to fetch OIDC discovery document from {discovery_url}: {exc}"
        ) from exc

    doc = response.json()
    jwks_uri = doc.get("jwks_uri", "")
    if not jwks_uri:
        raise RuntimeError(
            f"OIDC discovery document at {discovery_url} does not contain a 'jwks_uri' field."
        )
    logger.info("Resolved JWKS URI from discovery doc: %s", jwks_uri)
    return jwks_uri


def _get_jwks_client() -> PyJWKClient:
    global _jwks_client
    if _jwks_client is None:
        if oauth_cfg is None:
            raise RuntimeError(
                "OAuth configuration not loaded. Check oauth_config.yaml."
            )
        jwks_uri = _resolve_jwks_uri(oauth_cfg.issuer_url, oauth_cfg.verify_ssl)
        ssl_context = ssl.create_default_context() if oauth_cfg.verify_ssl else False
        _jwks_client = PyJWKClient(
            jwks_uri,
            lifespan=oauth_cfg.jwks_cache_ttl,
            ssl_context=ssl_context,
        )
        logger.info("JWKS client initialised: %s", jwks_uri)
    return _jwks_client


def _get_token_store() -> TokenStore:
    global _token_store
    if _token_store is None:
        if oauth_cfg is None:
            raise RuntimeError(
                "OAuth configuration not loaded. Check oauth_config.yaml."
            )
        _token_store = TokenStore(oauth_cfg.token_store_path)
    return _token_store


class OAuthTokenVerifier(TokenVerifier):
    """Validates an OIDC JWT and extracts the caller UPN.

    The UPN is stored in AccessToken.client_id so that get_itop_token()
    can retrieve it from the MCP auth context on every tool call.

    A failed or missing token causes the MCP handshake to return 401.
    A valid token with an unmapped UPN is accepted here -- the 403 is
    raised later in get_itop_token() at actual tool-call time, keeping
    the error surface close to the operation that needs the iTop token.
    """

    async def verify_token(self, token: str) -> AccessToken | None:
        if not token or not token.strip():
            return None

        if oauth_cfg is None:
            logger.error("verify_token called but oauth_cfg is None -- check oauth_config.yaml")
            return None

        try:
            jwks_client = _get_jwks_client()
            signing_key = jwks_client.get_signing_key_from_jwt(token)
            payload = jwt.decode(
                token,
                signing_key.key,
                algorithms=["RS256", "ES256"],
                audience=oauth_cfg.audience,
                issuer=oauth_cfg.issuer_url,
                options={"verify_exp": True},
            )
        except jwt.ExpiredSignatureError:
            logger.warning("JWT rejected: token expired")
            return None
        except jwt.InvalidAudienceError:
            logger.warning("JWT rejected: audience mismatch (expected: %s)", oauth_cfg.audience)
            return None
        except jwt.InvalidIssuerError:
            logger.warning("JWT rejected: issuer mismatch (expected: %s)", oauth_cfg.issuer_url)
            return None
        except jwt.PyJWTError as exc:
            logger.warning("JWT rejected: %s", exc)
            return None
        except Exception as exc:
            logger.error("Unexpected error during JWT validation: %s", exc)
            return None

        upn = payload.get(oauth_cfg.upn_claim) or ""
        if not upn:
            logger.warning(
                "JWT accepted but claim '%s' is missing or empty -- token rejected",
                oauth_cfg.upn_claim,
            )
            return None

        logger.debug("JWT accepted for UPN: %s", upn)
        # Store UPN in client_id -- the only identity field the SDK exposes
        # through the auth context that survives into tool-call scope.
        return AccessToken(token=token, client_id=upn, scopes=[])


def get_itop_token() -> str:
    """Resolve the iTop REST API token for the currently authenticated caller.

    Reads the UPN stored in the MCP auth context (put there by
    OAuthTokenVerifier.verify_token), then looks it up in the TokenStore.

    Raises ValueError (results in an MCP error response) if:
    - No auth context is present (should not happen after a valid handshake).
    - The UPN has no entry in the token store.
    """
    access_token = get_access_token()
    if access_token is None or not access_token.client_id:
        raise ValueError(
            "No authenticated identity found on this connection. "
            "Connect with a valid OAuth bearer token."
        )

    upn = access_token.client_id
    store = _get_token_store()
    itop_token = store.get_itop_token(upn)

    if itop_token is None:
        raise ValueError(
            f"No iTop token configured for identity '{upn}'. "
            "Ask an administrator to add your account to the token store."
        )

    return itop_token

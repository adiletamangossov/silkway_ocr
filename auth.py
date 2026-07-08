import json
import os
import secrets

# per-integrator API tokens for the OCR endpoint. each integrator (an external
# backend, a mobile app's server, ...) gets its own named token, so one can be
# revoked without affecting the others.
#
# tokens live as a json map in the API_TOKENS env var (delivered by the Modal
# secret):
#     {"acme-backend": "<token>", "warehouse-app": "<token>"}
# a legacy single API_TOKEN is still honored, as the consumer "default", so an
# existing integration keeps working while you migrate. when NO tokens are
# configured at all, the endpoint runs open (local dev).
#
# revoking an integrator = drop its entry from the map and re-sync the secret;
# every other integrator's token keeps working.


def load_tokens(env=None) -> dict:
    # build the {name: token} map from the environment. malformed API_TOKENS is
    # ignored rather than locking everyone out over a typo; the legacy token still
    # applies. never raises.
    env = os.environ if env is None else env
    tokens: dict[str, str] = {}

    raw = env.get("API_TOKENS")
    if raw:
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            data = None
        if isinstance(data, dict):
            tokens.update({str(k): str(v) for k, v in data.items() if v})

    legacy = env.get("API_TOKEN")
    if legacy:
        tokens.setdefault("default", legacy)

    return tokens


def authorize(authorization: str | None, env=None) -> tuple[bool, str | None]:
    # decide one request. returns (allowed, consumer): consumer is the integrator
    # whose token matched, or None. when no tokens are configured the endpoint is
    # open (allowed, None). token comparison is constant-time.
    tokens = load_tokens(env)
    if not tokens:
        return True, None

    prefix = "Bearer "
    if not authorization or not authorization.startswith(prefix):
        return False, None
    presented = authorization[len(prefix):]

    for name, value in tokens.items():
        if secrets.compare_digest(presented, value):
            return True, name
    return False, None

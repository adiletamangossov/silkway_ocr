"""Manage per-integrator API tokens for the OCR endpoint.

Each integrator gets its own named token, so one can be revoked without touching
the others (see auth.py). The tokens live as a JSON map in the API_TOKENS env var;
this script edits that map in your local .env (the editable source of truth), and
you then sync it to the deployed endpoint's Modal secret.

  python manage_tokens.py list                 # show integrator names (values masked)
  python manage_tokens.py add <name>           # generate + add a token, print it once
  python manage_tokens.py revoke <name>        # remove an integrator's token
  python manage_tokens.py sync                  # push the current .env to the Modal secret

Typical flow:
  python manage_tokens.py add acme-backend      # copy the printed token, share it securely
  python manage_tokens.py sync                   # then: modal deploy modal_app.py
  ...later...
  python manage_tokens.py revoke acme-backend    # cut off that integrator only
  python manage_tokens.py sync                    # then redeploy

Revoking takes effect once the secret is synced and new containers pick it up
(redeploy to force it immediately).
"""

import json
import os
import re
import secrets
import subprocess
import sys

ENV_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")

# keys that make up the Modal secret, mirrored from .env on sync.
SECRET_KEYS = ["DB_HOST", "DB_PORT", "DB_USER", "DB_PASSWORD", "DB_NAME", "API_TOKEN", "API_TOKENS"]


def _read_env() -> dict:
    env = {}
    if os.path.exists(ENV_PATH):
        for line in open(ENV_PATH, encoding="utf-8"):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    return env


def _tokens(env: dict) -> dict:
    raw = env.get("API_TOKENS")
    if not raw:
        return {}
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except ValueError:
        return {}


def _write_tokens(tokens: dict) -> None:
    # store the map as compact JSON on a single API_TOKENS line in .env, replacing
    # any existing one (or appending if absent). other lines are left untouched.
    value = json.dumps(tokens, ensure_ascii=False, separators=(",", ":"))
    line = f"API_TOKENS={value}"
    text = open(ENV_PATH, encoding="utf-8").read() if os.path.exists(ENV_PATH) else ""
    if re.search(r"^API_TOKENS=.*$", text, flags=re.M):
        text = re.sub(r"^API_TOKENS=.*$", line, text, flags=re.M)
    else:
        text = text.rstrip("\n") + f"\n# per-integrator API tokens (managed by manage_tokens.py)\n{line}\n"
    open(ENV_PATH, "w", encoding="utf-8").write(text)


def cmd_list():
    tokens = _tokens(_read_env())
    if not tokens:
        print("no per-integrator tokens configured (API_TOKENS empty).")
        return
    for name, tok in tokens.items():
        masked = (tok[:4] + "…" + tok[-2:]) if len(tok) > 6 else "…"
        print(f"  {name:<24} {masked}")


def cmd_add(name: str):
    env = _read_env()
    tokens = _tokens(env)
    if name in tokens:
        print(f"'{name}' already exists — revoke it first to rotate.")
        return
    token = secrets.token_urlsafe(24)
    tokens[name] = token
    _write_tokens(tokens)
    print(f"added integrator '{name}'. token (share securely, shown once):\n\n  {token}\n")
    print("now run:  python manage_tokens.py sync   then:  modal deploy modal_app.py")


def cmd_revoke(name: str):
    env = _read_env()
    tokens = _tokens(env)
    if name not in tokens:
        print(f"no integrator named '{name}'.")
        return
    del tokens[name]
    _write_tokens(tokens)
    print(f"revoked '{name}'. remaining: {', '.join(tokens) or '(none)'}")
    print("now run:  python manage_tokens.py sync   then:  modal deploy modal_app.py")


def cmd_sync():
    # push the current .env secret keys to the Modal secret (overwrites it whole).
    env = _read_env()
    args = ["modal", "secret", "create", "silkway-secrets", "--force"]
    for k in SECRET_KEYS:
        if env.get(k):
            args.append(f"{k}={env[k]}")
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    print("syncing .env -> Modal secret silkway-secrets ...")
    subprocess.run(args, check=True)
    print("synced. redeploy to apply immediately:  modal deploy modal_app.py")


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return
    cmd = sys.argv[1]
    if cmd == "list":
        cmd_list()
    elif cmd == "add" and len(sys.argv) == 3:
        cmd_add(sys.argv[2])
    elif cmd == "revoke" and len(sys.argv) == 3:
        cmd_revoke(sys.argv[2])
    elif cmd == "sync":
        cmd_sync()
    else:
        print(__doc__)


if __name__ == "__main__":
    main()

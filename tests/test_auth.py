from auth import authorize, load_tokens


def test_open_when_no_tokens_configured():
    assert authorize("Bearer whatever", env={}) == (True, None)
    assert authorize(None, env={}) == (True, None)


def test_legacy_single_token():
    env = {"API_TOKEN": "secret"}
    assert authorize("Bearer secret", env) == (True, "default")
    assert authorize("Bearer nope", env) == (False, None)
    assert authorize(None, env) == (False, None)


def test_per_integrator_map():
    env = {"API_TOKENS": '{"acme": "a-tok", "warehouse": "w-tok"}'}
    assert authorize("Bearer a-tok", env) == (True, "acme")
    assert authorize("Bearer w-tok", env) == (True, "warehouse")
    assert authorize("Bearer stranger", env) == (False, None)


def test_revoking_one_integrator_leaves_others_working():
    # revocation = drop the entry from the map. the revoked token stops working;
    # every other integrator is unaffected.
    env = {"API_TOKENS": '{"warehouse": "w-tok"}'}  # acme removed
    assert authorize("Bearer a-tok", env) == (False, None)
    assert authorize("Bearer w-tok", env) == (True, "warehouse")


def test_map_and_legacy_coexist():
    env = {"API_TOKENS": '{"acme": "a"}', "API_TOKEN": "leg"}
    assert authorize("Bearer a", env) == (True, "acme")
    assert authorize("Bearer leg", env) == (True, "default")


def test_malformed_map_does_not_lock_out_legacy():
    env = {"API_TOKENS": "{ not valid json", "API_TOKEN": "leg"}
    assert authorize("Bearer leg", env) == (True, "default")
    assert authorize("Bearer a", env) == (False, None)


def test_missing_bearer_prefix_rejected():
    assert authorize("secret", {"API_TOKEN": "secret"}) == (False, None)


def test_empty_token_entries_ignored():
    # a blank value must not become an accept-anything hole.
    env = {"API_TOKENS": '{"acme": ""}'}
    assert load_tokens(env) == {}
    assert authorize("Bearer ", env) == (True, None)  # no tokens -> open, not a match

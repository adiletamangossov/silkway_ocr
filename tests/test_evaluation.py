from evaluation import score, aggregate, format_table


def test_score_correct_auto_accept():
    decision = {"status": "accept", "member_id": "960662"}
    s = score("960662", decision)
    assert s["correct"] and s["accepted"] and not s["false_accept"]


def test_score_correct_manual_prefill():
    # right id, but routed to manual (low confidence). still correct, not accepted.
    decision = {"status": "manual", "member_id": "960662"}
    s = score("960662", decision)
    assert s["correct"] and not s["accepted"] and not s["false_accept"]


def test_score_no_id_label_resolved_to_nothing():
    # label has no client id; pipeline returns manual/None. both None -> correct.
    decision = {"status": "manual", "member_id": None}
    s = score(None, decision)
    assert s["correct"] and not s["accepted"] and not s["false_accept"]


def test_score_wrong_manual_is_incorrect_but_not_false_accept():
    decision = {"status": "manual", "member_id": "111111"}
    s = score("960662", decision)
    assert not s["correct"] and not s["false_accept"]


def test_score_false_accept_is_flagged():
    # the dangerous case: auto-accepted the wrong id.
    decision = {"status": "accept", "member_id": "111111"}
    s = score("960662", decision)
    assert s["accepted"] and not s["correct"] and s["false_accept"]


def test_aggregate_groups_by_platform():
    rows = [
        {"platform": "taobao", "correct": True, "accepted": True, "false_accept": False},
        {"platform": "taobao", "correct": False, "accepted": False, "false_accept": False},
        {"platform": "pinduoduo", "correct": True, "accepted": False, "false_accept": False},
    ]
    groups, total = aggregate(rows)
    assert groups["taobao"] == {"n": 2, "correct": 1, "accepted": 1, "false_accept": 0}
    assert groups["pinduoduo"]["n"] == 1
    assert total == {"n": 3, "correct": 2, "accepted": 1, "false_accept": 0}


def test_format_table_has_a_row_per_platform_and_total():
    rows = [
        {"platform": "taobao", "correct": True, "accepted": True, "false_accept": False},
        {"platform": "poizon", "correct": False, "accepted": False, "false_accept": False},
    ]
    groups, total = aggregate(rows)
    table = format_table(groups, total)
    assert "taobao" in table and "poizon" in table and "ALL" in table

# pure scoring helpers for the per-platform eval harness. no model, no db, no
# modal imports, so the logic is unit-testable on its own. the harness in
# modal_app.py does the IO (read photos, call the model) and hands the resulting
# decisions here to be scored and aggregated.


def score(expected: str | None, decision: dict) -> dict:
    # compare one resolution against ground truth.
    #   expected   the true member_id, or None when the label genuinely has none
    #   decision   the dict returned by resolve_member_id
    #
    # correct      did we land on the right id? (both None counts as correct: a
    #              no-id label that we routed to manual with nothing prefilled.)
    # accepted     did we auto-accept it?
    # false_accept the dangerous case: auto-accepted, but the id is wrong. this is
    #              what must stay at zero; a wrong manual prefill is recoverable,
    #              a wrong auto-accept ships a parcel to the wrong client.
    got = decision.get("member_id")
    accepted = decision.get("status") == "accept"
    correct = got == expected
    return {
        "expected": expected,
        "got": got,
        "correct": correct,
        "accepted": accepted,
        "false_accept": accepted and not correct,
    }


def aggregate(rows: list[dict]) -> tuple[dict, dict]:
    # roll per-sample score rows up by platform. each row must carry a "platform"
    # key plus the boolean fields from score(). returns (per_platform, total).
    groups: dict[str, dict] = {}
    for r in rows:
        g = groups.setdefault(
            r["platform"], {"n": 0, "correct": 0, "accepted": 0, "false_accept": 0}
        )
        g["n"] += 1
        g["correct"] += int(r["correct"])
        g["accepted"] += int(r["accepted"])
        g["false_accept"] += int(r["false_accept"])

    total = {"n": 0, "correct": 0, "accepted": 0, "false_accept": 0}
    for g in groups.values():
        for k in total:
            total[k] += g[k]
    return groups, total


def format_table(groups: dict, total: dict) -> str:
    # plain-text report: one row per platform, then an ALL total. correct and
    # accept are shown as count/n so a small sample reads honestly.
    def row(name: str, g: dict) -> str:
        correct = f"{g['correct']}/{g['n']}"
        accept = f"{g['accepted']}/{g['n']}"
        return f"{name:<14}{g['n']:>4}{correct:>10}{accept:>10}{g['false_accept']:>14}"

    lines = [f"{'platform':<14}{'n':>4}{'correct':>10}{'accept':>10}{'false_accept':>14}"]
    for name in sorted(groups):
        lines.append(row(name, groups[name]))
    lines.append(row("ALL", total))
    return "\n".join(lines)

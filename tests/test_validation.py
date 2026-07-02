import pytest

from validation import SqliteUserIDStore, resolve_member_id, validate


@pytest.fixture
def store(tmp_path):
    # isolated db file per test; seeded with one known client id.
    s = SqliteUserIDStore(db_path=str(tmp_path / "user_ids.db"))
    s.add("960662")
    return s


def test_store_roundtrip(store):
    assert store.exists("960662") is True
    assert store.exists("123456") is False


def test_add_is_idempotent(store):
    # adding the same id twice must not raise (primary-key conflict ignored).
    store.add("960662")
    assert store.exists("960662") is True


def test_accept_when_parsed_and_in_db(store):
    decision = validate("960662", store)
    assert decision["status"] == "accept"
    assert decision["user_id"] == "960662"


def test_manual_when_parsed_but_not_in_db(store):
    decision = validate("123456", store)
    assert decision["status"] == "manual"
    # the guess is still carried so the manual queue can pre-fill it.
    assert decision["user_id"] == "123456"


def test_manual_when_nothing_parsed(store):
    decision = validate(None, store)
    assert decision["status"] == "manual"
    assert decision["user_id"] is None


# resolve_member_id: marker-first with db-assisted fallback


def test_resolve_accepts_marker_match_in_db(store):
    decision = resolve_member_id("库区首都波960662号", store)
    assert decision["status"] == "accept"
    assert decision["member_id"] == "960662"
    assert decision["confidence"] == "high"
    assert decision["source"] == "marker"


def test_resolve_manual_when_marker_id_not_in_db(store):
    # marker locates an id, but it is not a real client (likely OCR digit error).
    decision = resolve_member_id("库区首都波123456号", store)
    assert decision["status"] == "manual"
    assert decision["member_id"] == "123456"
    assert decision["confidence"] == "low"
    assert decision["source"] == "marker"


def test_resolve_fallback_single_db_valid_candidate(store):
    # no marker, but one digit run is a real member id (here it is not even
    # 号-terminated, which the old fallback required).
    decision = resolve_member_id("库区 960662 大道东一路3号", store)
    assert decision["status"] == "manual"
    assert decision["member_id"] == "960662"
    assert decision["confidence"] == "low"
    assert decision["source"] == "db_scan"


def test_resolve_scans_bare_id_on_real_label(store):
    # the real hided.jpg transcript: china post label, no marker, member_id 96638
    # as a bare run among a barcode and phone decoy. seed it and confirm the scan
    # recovers it where the marker/号 paths returned None on the live run.
    store.add("96638")
    transcript = (
        "中国邮政 CHINA POST 9817442083870 收 17284427248转9789\n"
        "广东省佛山市南海区里水镇 新联工业区工业大道东一路3号院25-04厂区\n"
        "96638 寄"
    )
    decision = resolve_member_id(transcript, store)
    assert decision["status"] == "manual"
    assert decision["member_id"] == "96638"
    assert decision["confidence"] == "low"
    assert decision["source"] == "db_scan"


def test_resolve_fallback_no_db_valid_candidate(store):
    decision = resolve_member_id("库区111111号", store)
    assert decision["status"] == "manual"
    assert decision["member_id"] is None
    assert decision["confidence"] == "none"
    assert decision["source"] is None


def test_resolve_manual_on_wrong_carrier_label(store):
    # a real yunda routing sticker (problem_photo.jpg): no 首都波 marker and no
    # 号-terminated id at all, just barcodes / sort codes / a masked phone. the
    # member_id is genuinely absent, so the pipeline must refuse to invent one.
    transcript = (
        "韵达快递 全国服务热线:95546 www.yundaex.com WB-8 2602W 第1/2个\n"
        "2026/06/23 15:53:45 620A G500-00 B6\n"
        "4654 4767 1475 579 南京-长线\n"
        "隐私号码 1728409311 5转9968 收"
    )
    decision = resolve_member_id(transcript, store)
    assert decision["status"] == "manual"
    assert decision["member_id"] is None
    assert decision["confidence"] == "none"
    assert decision["source"] is None


def test_resolve_fallback_multiple_db_valid_candidates(store):
    store.add("100200")  # a second real id, so the fallback is ambiguous
    decision = resolve_member_id("库区960662号 别的100200号", store)
    assert decision["status"] == "manual"
    assert decision["member_id"] is None
    assert decision["confidence"] == "none"
    assert sorted(decision["candidates"]) == ["100200", "960662"]


# find_matching: LIKE-pattern lookup behind the wildcard resolution


def test_find_matching_returns_like_hits(store):
    assert store.find_matching(["960_62"]) == ["960662"]


def test_find_matching_empty_patterns_no_query(store):
    assert store.find_matching([]) == []


# wildcard resolution: recover an id that is one obscured/misread digit off


def test_resolve_wildcard_recovers_obscured_digit(store):
    # the real hided.jpg story: the red sorting-pen hides one digit, so OCR reads
    # the 5-digit "96638". no exact db hit, but exactly one real client is one
    # inserted digit away (966238) -> recover it and pre-fill the manual queue.
    store.add("966238")
    decision = resolve_member_id("新联工业区工业大道东一路3号 96638 寄", store)
    assert decision["status"] == "manual"
    assert decision["member_id"] == "966238"
    assert decision["confidence"] == "low"
    assert decision["source"] == "db_wildcard"


def test_resolve_wildcard_recovers_misread_digit(store):
    # right length, one wrong digit (6 misread as 8): 966838 -> 966238.
    store.add("966238")
    decision = resolve_member_id("库区 966838", store)
    assert decision["member_id"] == "966238"
    assert decision["source"] == "db_wildcard"


def test_resolve_wildcard_ambiguous_surfaces_candidates(store):
    # two real clients are each one digit away -> too risky to pick, list both.
    store.add("966238")
    store.add("966938")
    decision = resolve_member_id("库区 96638", store)
    assert decision["status"] == "manual"
    assert decision["member_id"] is None
    assert decision["confidence"] == "none"
    assert decision["source"] == "db_wildcard"
    assert sorted(decision["candidates"]) == ["966238", "966938"]


def test_resolve_wildcard_ignores_non_digit_like_hit(store):
    # a `_` matches any char, so LIKE could land on a non-numeric row; the digit
    # filter must drop it rather than pre-fill garbage.
    store.add("966a38")
    decision = resolve_member_id("库区 96638", store)
    assert decision["member_id"] is None
    assert decision["source"] is None


def test_resolve_wildcard_dense_range_is_unrecoverable(store):
    # the real hided.jpg outcome: many clients sit one digit from "96638", so the
    # obscured digit can't be guessed. don't dump a long list; route to plain
    # manual with no prefill and no candidates.
    for d in range(10):
        store.add(f"966{d}38")  # 966038..966938, all real clients
    decision = resolve_member_id("库区 96638", store)
    assert decision["status"] == "manual"
    assert decision["member_id"] is None
    assert decision["source"] is None
    assert "candidates" not in decision
    assert "unrecoverable" in decision["reason"]


def test_resolve_exact_hit_preempts_wildcard(store):
    # 960662 is exact in the fixture; the fuzzy path must not even run.
    decision = resolve_member_id("库区 960662 大道", store)
    assert decision["source"] == "db_scan"
    assert decision["member_id"] == "960662"

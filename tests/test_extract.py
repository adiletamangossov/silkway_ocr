from extraction import (
    extract_user_id,
    find_id_candidates,
    find_member_id_candidates,
    wildcard_patterns,
)

# what qwen3-vl actually returned for hided.jpg: a china post label with no
# 首都波 marker and the member_id "96638" sitting as a bare digit run, not 号-
# terminated. it also carries a 13-digit barcode and an 11-digit phone as decoys.
HIDED_TRANSCRIPT = (
    "中国邮政 CHINA POST 第10/50个 197-197-A39-260\n"
    "9817442083870\n"
    "收 17284427248转9789\n"
    "广东省佛山市南海区里水镇新联工业区工业大道东一路3号院25-04厂区青松路\n"
    "96638\n"
    "寄 【纱布防晒衣】鸢鸢果园100"
)

# a realistic transcript: the real id sits after the 首都波 marker, but the
# label also carries decoy numbers (street 3号, district 5号, tracking code).
REAL_TRANSCRIPT = (
    "收 广东省佛山市南海区里水镇新联工业区工业大道东一路3号航达B04库区首都波960662号\n"
    "寄 林北 18025599791 广东省汕头市澄海区凤翔街道凤翔工业区5号区大\n"
    "79112591399586"
)


def test_extracts_id_after_marker():
    assert extract_user_id(REAL_TRANSCRIPT) == "960662"


def test_ignores_other_numbers_before_marker():
    # the street number 3号 appears before the marker; it must not be picked.
    assert extract_user_id("大道东一路3号首都波960662号") == "960662"


def test_tolerates_whitespace_around_digits():
    # the VLM sometimes inserts spaces between glyphs.
    assert extract_user_id("首都波 960662 号") == "960662"


def test_returns_none_when_marker_absent():
    assert extract_user_id("大道东一路3号没有标记") is None


def test_returns_none_on_empty_string():
    assert extract_user_id("") is None


def test_accepts_five_and_seven_digit_ids():
    # the db has rare 5- and 7-digit member ids, so both must parse.
    assert extract_user_id("库区首都波90322号") == "90322"
    assert extract_user_id("库区首都波1100455号") == "1100455"


def test_rejects_overlong_digit_run():
    # if the 号 terminator is misread the run can blow past a real id; an
    # 8+ digit capture is implausible and must not be returned.
    assert extract_user_id("库区首都波123456789号") is None


def test_candidates_collect_id_shaped_runs_before_hao():
    # marker-less line: pick up the 5-7 digit run ending in 号, skip the 3号
    # street number (too short) and the tracking code (not before 号).
    text = "大道东一路3号 库区960662号 79112591399586"
    assert find_id_candidates(text) == ["960662"]


def test_candidates_dedupe_in_reading_order():
    text = "前 100200号 后 100200号 又 305070号"
    assert find_id_candidates(text) == ["100200", "305070"]


def test_candidates_ignore_tail_of_longer_number():
    # the (?<!\d) lookbehind must stop a 7-digit tail of a long code from
    # being mistaken for a candidate.
    assert find_id_candidates("79112591399586号") == []


# find_member_id_candidates: the marker-less, 号-less digit-run net


def test_member_candidates_pick_bare_id_run():
    # the real hided.jpg case: 96638 is a bare run, not after a marker or 号.
    # the find_id_candidates net would miss it; this one must catch it.
    assert find_id_candidates(HIDED_TRANSCRIPT) == []
    assert find_member_id_candidates(HIDED_TRANSCRIPT) == ["96638"]


def test_member_candidates_drop_overlong_runs():
    # the 13-digit barcode and 11-digit phone are single maximal runs, so the
    # length filter drops them whole rather than slicing a 6-digit hit out.
    assert find_member_id_candidates("9817442083870 17284427248") == []


def test_member_candidates_keep_5_to_7_digits():
    assert find_member_id_candidates("a 1234 b 90322 c 960662 d 1100455 e") == [
        "90322",
        "960662",
        "1100455",
    ]


def test_member_candidates_dedupe_in_reading_order():
    assert find_member_id_candidates("960662 then 100200 then 960662") == [
        "960662",
        "100200",
    ]


# wildcard_patterns: single-blank LIKE patterns for an obscured/misread digit


def test_wildcard_covers_misread_and_obscured():
    # for the hided.jpg run "96638": blank-in-place (misread, len 5) plus
    # blank-inserted (an obscured digit dropped by OCR, len 6). the real id's
    # pattern "966_38" must be among them.
    patterns = wildcard_patterns("96638")
    misread = ["_6638", "9_638", "96_38", "966_8", "9663_"]
    obscured = ["_96638", "9_6638", "96_638", "966_38", "9663_8", "96638_"]
    assert patterns == misread + obscured
    assert "966_38" in patterns


def test_wildcard_skips_insertion_past_max_length():
    # a 7-digit run can only be misread, never lengthened: an inserted blank
    # would make an 8-digit id, which no client has.
    patterns = wildcard_patterns("1234567")
    assert all(len(p) == 7 for p in patterns)
    assert len(patterns) == 7


def test_wildcard_patterns_are_unique():
    assert len(wildcard_patterns("96638")) == len(set(wildcard_patterns("96638")))

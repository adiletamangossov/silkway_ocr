import re

# pure text->id parsing, no modal / no db imports, so both the modal entrypoint
# and the validation layer can use it freely.

# the receiver line embeds the client user id as the digits between the
# warehouse zone marker and the char 号, e.g. "...首都波960662号" -> "960662".
# anchoring on the marker avoids the other numbers on the label
# (street number 3号, phone, tracking code).
WAREHOUSE_MARKER = "首都波"

# member_id is 5-7 digits in the db (6 digits for 99.9% of active clients).
# bounding the run to that range rejects an over-long capture if the 号
# terminator is misread; the db lookup is still the final arbiter.
USER_ID_RE = re.compile(WAREHOUSE_MARKER + r"\s*(\d{5,7})\s*号")

# fallback pattern for when the marker is missing: any 5-7 digit run that ends
# in 号 and is not part of a longer number. the (?<!\d) lookbehind stops it from
# grabbing a 7-digit tail of, say, a tracking code that happens to precede 号.
CANDIDATE_RE = re.compile(r"(?<!\d)(\d{5,7})\s*号")

# looser "this is a SilkWay warehouse label" signal. OCR frequently mangles the
# exact 首都波 glyphs (seen live: 首都表, 首部城) or splits them with spaces, so the
# exact USER_ID_RE misses even on a genuine warehouse label. these tokens survive
# that noise: 库区 (the storage-zone word right before the id block) and 航达 (the
# building name) are fixed warehouse identifiers, and 首[都部] catches any near-
# spelling of the 首都波 marker itself. presence of any of them means the address
# block is the warehouse's, so a 号-terminated id run on the label can be trusted.
FUZZY_MARKER_RE = re.compile(r"库区|航达|首[都部]")

# broadest fallback: every *maximal* digit run on the label, with no 号 or marker
# requirement. real labels turned out not to follow the 首都波...号 layout, yet the
# member_id is still a standalone 5-7 digit run. matching whole runs (the
# lookarounds force the run to be bounded by non-digits) means long numbers stay
# long: a 13-digit barcode or 11-digit phone is captured as one over-length run
# and dropped by the length filter, never sliced into a spurious 6-digit hit.
DIGIT_RUN_RE = re.compile(r"(?<!\d)(\d+)(?!\d)")


def extract_user_id(transcript: str) -> str | None:
    # primary path: the id sitting right after the warehouse marker.
    match = USER_ID_RE.search(transcript)
    if match:
        return match.group(1)
    return None


def has_warehouse_marker(transcript: str) -> bool:
    # true when the transcript carries a warehouse-address signal in any form,
    # even one too mangled for the exact-marker regex. lets the resolver trust a
    # 号-terminated id run on a label it can recognize as the warehouse's.
    return FUZZY_MARKER_RE.search(transcript) is not None


def find_id_candidates(transcript: str) -> list[str]:
    # fallback path: every plausible id-shaped run ending in 号, de-duplicated
    # and kept in reading order. the caller decides which (if any) is real by
    # checking them against the database.
    seen: list[str] = []
    for match in CANDIDATE_RE.finditer(transcript):
        candidate = match.group(1)
        if candidate not in seen:
            seen.append(candidate)
    return seen


def find_member_id_candidates(transcript: str) -> list[str]:
    # broadest fallback: every maximal digit run whose length matches a real
    # member_id (5-7 digits). de-duplicated, kept in reading order. this is the
    # net we cast when neither the marker nor a 号 terminator is present; the db
    # lookup is what separates the real id from same-length decoys.
    seen: list[str] = []
    for match in DIGIT_RUN_RE.finditer(transcript):
        run = match.group(1)
        if 5 <= len(run) <= 7 and run not in seen:
            seen.append(run)
    return seen


# the lowest member_id length we will ever resolve to. mirrors the db profiling:
# 5-7 digit ids only, 6 for ~all active clients.
MIN_ID_LEN = 5
MAX_ID_LEN = 7


def wildcard_patterns(run: str) -> list[str]:
    # a scanned run that the db rejects may be one digit off: either a digit was
    # OBSCURED so OCR dropped it (red sorting-pen ink over the id -> the run is a
    # digit too short), or a digit was MISREAD (right length, one wrong digit,
    # e.g. 6 read as 8). turn the run into SQL LIKE patterns where `_` is exactly
    # one unknown character, so the db can tell us which real id it must have been.
    #
    #   misread   -> blank each position in place      (length unchanged)
    #   obscured  -> insert one blank at each position  (length + 1)
    #
    # patterns are kept only if their length is a real id length, so we never ask
    # the db for an implausibly short or long id. de-duplicated, order preserved.
    patterns: list[str] = []

    def add(p: str):
        if MIN_ID_LEN <= len(p) <= MAX_ID_LEN and p not in patterns:
            patterns.append(p)

    # misread digit: one position unknown, same length.
    for i in range(len(run)):
        add(run[:i] + "_" + run[i + 1:])

    # obscured digit dropped by OCR: one extra unknown, length grows by one.
    for i in range(len(run) + 1):
        add(run[:i] + "_" + run[i:])

    return patterns

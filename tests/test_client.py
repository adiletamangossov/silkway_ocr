from client import _encode_multipart


def _parse_boundary(content_type: str) -> str:
    assert content_type.startswith("multipart/form-data; boundary=")
    return content_type.split("boundary=", 1)[1]


def test_encodes_file_part():
    ct, body = _encode_multipart({}, {"file": ("label.jpg", b"\xff\xd8jpegbytes")})
    boundary = _parse_boundary(ct)

    # the boundary is present, the file field is named and carries its filename,
    # and the raw (binary) content survives intact.
    assert f"--{boundary}".encode() in body
    assert b'name="file"; filename="label.jpg"' in body
    assert b"Content-Type: application/octet-stream" in body
    assert b"\xff\xd8jpegbytes" in body
    # body terminates with the closing boundary marker.
    assert body.rstrip(b"\r\n").endswith(f"--{boundary}--".encode())


def test_includes_text_field():
    ct, body = _encode_multipart({"platform": "taobao"}, {"file": ("x.jpg", b"x")})
    assert b'name="platform"' in body
    assert b"taobao" in body


def test_boundary_is_unique_per_call():
    ct1, _ = _encode_multipart({}, {"file": ("a", b"a")})
    ct2, _ = _encode_multipart({}, {"file": ("a", b"a")})
    assert _parse_boundary(ct1) != _parse_boundary(ct2)


def test_unicode_field_value_is_utf8_encoded():
    # platform tags are ascii today, but a text value must still round-trip as utf-8
    # bytes rather than crash the encoder.
    _, body = _encode_multipart({"note": "首都波"}, {"file": ("x", b"x")})
    assert "首都波".encode("utf-8") in body

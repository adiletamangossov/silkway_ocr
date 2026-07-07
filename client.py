"""Client for the deployed SilkWay OCR endpoint.

Send a courier-sticker photo to the service and get the resolved member_id back.
This is the caller a parcel-processing backend imports (recognize(...)) or a person
runs from the terminal (python client.py photo.jpg). It keeps the bearer token
server-side — read from the environment, never hardcoded — so nothing sensitive
lives in a browser or in code.

Zero third-party dependencies (stdlib only), so it drops straight into any backend.

Config via env (the CLI loads .env for you):
    OCR_ENDPOINT_URL   base url of the service (defaults to the deployed app)
    API_TOKEN          bearer token, if the endpoint enforces one

Run:  python client.py path/to/label.jpg [--platform taobao]
"""

import json
import os
import urllib.error
import urllib.request

# the deployed service. override per call (url=...) or via OCR_ENDPOINT_URL.
DEFAULT_URL = "https://adilet-amangossov--silkway-ocr-web.modal.run"


def _encode_multipart(
    fields: dict[str, str], files: dict[str, tuple[str, bytes]]
) -> tuple[str, bytes]:
    # build a multipart/form-data body by hand so the client needs no requests
    # dependency. fields are plain text form values; files map a field name to
    # (filename, content). returns the Content-Type header value and the raw body.
    # pure and deterministic given the boundary, so it is unit-testable.
    boundary = "----SilkwayOCR" + os.urandom(16).hex()
    crlf = b"\r\n"
    out: list[bytes] = []

    for name, value in fields.items():
        out.append(b"--" + boundary.encode())
        out.append(f'Content-Disposition: form-data; name="{name}"'.encode())
        out.append(b"")
        out.append(str(value).encode("utf-8"))

    for name, (filename, content) in files.items():
        out.append(b"--" + boundary.encode())
        out.append(
            f'Content-Disposition: form-data; name="{name}"; filename="{filename}"'.encode()
        )
        out.append(b"Content-Type: application/octet-stream")
        out.append(b"")
        out.append(content)

    out.append(b"--" + boundary.encode() + b"--")
    out.append(b"")

    body = crlf.join(out)
    return f"multipart/form-data; boundary={boundary}", body


def recognize(
    image,
    platform: str | None = None,
    *,
    url: str | None = None,
    token: str | None = None,
    filename: str | None = None,
    timeout: float = 120,
) -> dict:
    # POST one photo to the endpoint and return the resolver decision dict
    # (status / member_id / confidence / source / reason / candidates / transcript).
    #
    #   image      a path (str / os.PathLike) or the raw image bytes
    #   platform   optional origin tag stored with the decision (taobao / pdd / ...)
    #   url        base url; defaults to OCR_ENDPOINT_URL env, then DEFAULT_URL
    #   token      bearer token; defaults to the API_TOKEN env var
    #   filename   name to send when `image` is bytes (defaults to upload.jpg)
    url = (url or os.environ.get("OCR_ENDPOINT_URL") or DEFAULT_URL).rstrip("/")
    token = token or os.environ.get("API_TOKEN")

    if isinstance(image, (str, os.PathLike)):
        path = os.fspath(image)
        with open(path, "rb") as f:
            content = f.read()
        fname = filename or os.path.basename(path)
    else:
        content = image
        fname = filename or "upload.jpg"

    fields = {"platform": platform} if platform else {}
    content_type, body = _encode_multipart(fields, {"file": (fname, content)})

    headers = {"Content-Type": content_type}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    req = urllib.request.Request(
        url + "/recognize", data=body, headers=headers, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        # surface the server's error body (e.g. 401 auth, 400 empty file) instead
        # of a bare status, so the caller sees why.
        detail = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"endpoint returned HTTP {e.code}: {detail}") from e


def main():
    import argparse

    from dotenv import load_dotenv

    # load OCR_ENDPOINT_URL / API_TOKEN (and DB creds, harmlessly) from .env, same
    # as the other scripts. done here, not at import, so importing this as a library
    # never has a side effect.
    load_dotenv()

    parser = argparse.ArgumentParser(
        description="Send a courier-sticker photo to the SilkWay OCR endpoint."
    )
    parser.add_argument("image", help="path to the label photo")
    parser.add_argument("--platform", help="origin tag stored with the decision")
    parser.add_argument("--url", help="override the endpoint base url")
    args = parser.parse_args()

    decision = recognize(args.image, platform=args.platform, url=args.url)
    print(json.dumps(decision, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

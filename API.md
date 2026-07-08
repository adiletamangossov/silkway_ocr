# SilkWay OCR — API integration guide

Read a client's **member_id** off a courier-sticker photo. Send one photo, get back
a structured decision: either an auto-accepted id, or a manual-review result with a
pre-filled guess.

This guide is for backend developers integrating from an external service.

---

## 1. Endpoint

```
POST  https://adilet-amangossov--silkway-ocr-web.modal.run/recognize
```

| | |
|---|---|
| Method | `POST` |
| Body | `multipart/form-data` |
| Auth | `Authorization: Bearer <API_TOKEN>` (required) |
| Response | `application/json` |

Health check (no auth): `GET /health` → `{"status":"ok","model":"..."}`.
Interactive docs (token-gated): open `/docs?token=<API_TOKEN>` in a browser.

Ask the SilkWay team for your `API_TOKEN`. Keep it server-side — never ship it in a
browser or mobile client. Put your own service between the token and the public.

---

## 2. Request

Send the photo as multipart form fields:

| Field | Type | Required | Description |
|---|---|---|---|
| `file` | file | yes | The sticker photo. JPEG or PNG. Any size; ~0.3–2 MB is typical. |
| `platform` | text | no | Free-text origin tag stored with the decision (e.g. `taobao`, `pdd`). Useful for your own analytics; does not change the result. |

Nothing else is needed — the service does the OCR and the database lookup itself.

---

## 3. Response

`200 OK`, JSON:

```json
{
  "transcript": "ZTO 中通快递 ... 库区首都波960662号 ...",
  "status": "accept",
  "member_id": "960662",
  "confidence": "high",
  "source": "marker",
  "reason": "marker match confirmed in db"
}
```

### Fields

| Field | Type | Description |
|---|---|---|
| `status` | string | `accept` or `manual`. **This is the field you branch on.** |
| `member_id` | string \| null | The resolved / suggested id. `null` when nothing usable was found. |
| `confidence` | string | `high`, `low`, or `none`. |
| `source` | string \| null | Which path produced the result (see §5). `null` when unresolved. |
| `reason` | string | Human-readable explanation, for logs / a review UI. |
| `candidates` | string[] | **Present only** when several ids are plausible (ambiguous). |
| `transcript` | string | The full OCR text of the label. Useful for a review screen or audit. |

### The one rule that always holds

> The service **never returns a wrong id as `accept`.** If it is not sure, it returns
> `status: "manual"`. A `manual` result is safe to route to a person; an `accept` is
> safe to use automatically.

So your integration only needs two branches: **`accept` → use it**, **`manual` →
review it** (pre-fill your form with `member_id` / `candidates`).

---

## 4. How to handle each result

| `status` | `member_id` | `candidates` | Meaning | Recommended action |
|---|---|---|---|---|
| `accept` | a value | — | Database-confirmed id (high confidence). | Use it automatically. |
| `manual` | a value | — | Strong guess, not auto-trusted. | Pre-fill your field; one-click confirm. |
| `manual` | a value | — | Weaker guess (`confidence: "low"`). | Pre-fill; ask a person to verify. |
| `manual` | `null` | a list | Several real ids are plausible. | Show the shortlist to pick from. |
| `manual` | `null` | — | No usable id in the photo. | Manual entry; consider re-shooting the photo. |

Minimal pseudo-logic:

```
resp = POST /recognize (file)
if resp.status == "accept":
    assign(member_id = resp.member_id)          # hands-off
else:
    queue_for_review(prefill = resp.member_id,  # may be null
                     options = resp.candidates)  # may be absent
```

---

## 5. `source` values (for reference / logging)

| `source` | Path | Auto-accept? |
|---|---|---|
| `marker` | Exact warehouse marker `首都波…号` read cleanly, id confirmed in DB. | **Yes** |
| `marker_fuzzy` | Warehouse marker present but OCR-mangled; a unique DB-valid id before `号`. | No — returned as `manual` with the guess pre-filled (zero-misdelivery policy). |
| `db_scan` | No marker; a single DB-valid digit run on the label. | No — `manual` pre-fill. |
| `db_wildcard` | No exact hit; one real id within a single misread/obscured digit. | No — `manual` pre-fill. |
| `null` | Nothing resolvable. | No. |

You do not need to special-case `source` — branching on `status` is enough. It is
provided for your logs and for tuning your review UI.

---

## 6. Errors

| HTTP | Body | Cause | What to do |
|---|---|---|---|
| `401` | `{"detail":"missing or invalid bearer token"}` | Missing/wrong `Authorization`. | Check the token. |
| `400` | `{"detail":"empty image file"}` | The `file` field was empty. | Send real image bytes. |
| `422` | FastAPI validation error | The `file` field is missing entirely. | Send `file` as multipart. |
| `5xx` / timeout | — | Transient (e.g. a cold start, see §7). | Retry with backoff. |

---

## 7. Operational notes (please read before going live)

- **Cold starts.** The service scales to zero when idle. The **first** request after
  an idle period spins up a GPU and loads the model — this can take **60–120 s** and
  may hit your client timeout. Set your **HTTP client timeout to ≥ 120 s** and retry
  transient failures with backoff. Warm requests return in a few seconds.
- **Retries are safe.** The endpoint is not idempotent (each call is logged), but a
  photo can be re-sent freely — re-sending never causes a wrong auto-assignment.
- **Concurrency.** The service handles concurrent requests and scales under load;
  there is no fixed rate limit beyond your token. It is GPU-bound, so throughput is
  finite — batch politely.
- **Accuracy depends on the photo, not the call.** The id must be legible in the
  frame. In review, the reader is correct **~81% of the time when the id is clearly
  in the photo** and effectively 0% when it isn't (too small / wrong label face / cut
  off). If you control capture, frame the **receiver-address label** close and fully
  (see `SilkWay_ID_photo_guide.pdf`). The service is safe regardless — poor photos
  just fall to `manual`.
- **Optional filename convention.** If you can name the uploaded file
  `parcel_<id>.jpg`, it lets SilkWay reconcile the decision against ground truth
  later. Purely optional.

---

## 8. Examples

All examples send one photo with the bearer token. Replace `$TOKEN` / `label.jpg`.

### curl

```bash
curl -X POST https://adilet-amangossov--silkway-ocr-web.modal.run/recognize \
  -H "Authorization: Bearer $TOKEN" \
  -F "file=@label.jpg" \
  -F "platform=taobao"
```

### Python (requests)

```python
import requests

def recognize(path, token, platform=None):
    with open(path, "rb") as f:
        files = {"file": (path, f, "image/jpeg")}
        data = {"platform": platform} if platform else {}
        r = requests.post(
            "https://adilet-amangossov--silkway-ocr-web.modal.run/recognize",
            headers={"Authorization": f"Bearer {token}"},
            files=files, data=data, timeout=150,
        )
    r.raise_for_status()
    return r.json()

d = recognize("label.jpg", TOKEN)
if d["status"] == "accept":
    assign_member_id(d["member_id"])
else:
    queue_for_review(prefill=d.get("member_id"), options=d.get("candidates"))
```

### Node.js (18+, built-in fetch)

```js
import { readFile } from "node:fs/promises";

async function recognize(path, token, platform) {
  const form = new FormData();
  form.append("file", new Blob([await readFile(path)]), "label.jpg");
  if (platform) form.append("platform", platform);

  const r = await fetch(
    "https://adilet-amangossov--silkway-ocr-web.modal.run/recognize",
    { method: "POST", headers: { Authorization: `Bearer ${token}` }, body: form,
      signal: AbortSignal.timeout(150000) },
  );
  if (!r.ok) throw new Error(`OCR ${r.status}: ${await r.text()}`);
  return r.json();
}

const d = await recognize("label.jpg", TOKEN);
d.status === "accept"
  ? assignMemberId(d.member_id)
  : queueForReview({ prefill: d.member_id, options: d.candidates });
```

### PHP (cURL)

```php
$ch = curl_init("https://adilet-amangossov--silkway-ocr-web.modal.run/recognize");
curl_setopt_array($ch, [
  CURLOPT_POST => true,
  CURLOPT_HTTPHEADER => ["Authorization: Bearer {$token}"],
  CURLOPT_POSTFIELDS => [
    "file" => new CURLFile("label.jpg", "image/jpeg"),
    "platform" => "taobao",
  ],
  CURLOPT_RETURNTRANSFER => true,
  CURLOPT_TIMEOUT => 150,
]);
$body = curl_exec($ch);
$d = json_decode($body, true);
// $d["status"] === "accept" ? use $d["member_id"] : review with $d["member_id"] / $d["candidates"]
```

---

## 9. Quick checklist

- [ ] Store `API_TOKEN` server-side; send it as `Authorization: Bearer`.
- [ ] POST the photo as multipart `file` (JPEG/PNG).
- [ ] Branch on `status`: `accept` → use `member_id`; `manual` → review with
      `member_id` / `candidates` pre-filled.
- [ ] Client timeout ≥ 120 s; retry `5xx`/timeout with backoff.
- [ ] Never treat `manual` as a final id, and never expect a wrong id under `accept`.

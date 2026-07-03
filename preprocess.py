from PIL import Image, ImageOps

# image preprocessing for the courier photos. the real-data eval showed most
# failing photos are severely UNDEREXPOSED — a small bright sticker on a large
# dark background — so the id digits sit in near-black pixels the VLM can't read.
#
# two steps, in order:
#   1. crop to the sticker (the bright region), so the dark background stops
#      dominating the global brightness math in step 2.
#   2. stretch contrast and, if still dark, gamma-brighten the crop.
#
# PIL only (no numpy/opencv), so the Modal image needs no new deps. every step
# falls back to the original image if it can't find a sane result, so preprocess
# can never make a photo worse than leaving it untouched.


def _histogram_stats(gray):
    # mean intensity and total pixel count from an 8-bit grayscale histogram.
    hist = gray.histogram()
    total = sum(hist)
    if total == 0:
        return 0.0, 0, hist
    mean = sum(i * c for i, c in enumerate(hist)) / total
    return mean, total, hist


def _percentile_intensity(hist, total, pct):
    # smallest intensity whose cumulative share reaches `pct` (0..1).
    target = total * pct
    cum = 0
    for value, count in enumerate(hist):
        cum += count
        if cum >= target:
            return value
    return 255


# a crop is only trusted if the bright region is a real sticker: not a tiny
# specular glint, not basically the whole frame.
MIN_CROP_AREA_FRAC = 0.02
MAX_CROP_AREA_FRAC = 0.95


def crop_to_sticker(img, pad_frac=0.06):
    # find the bright label on a dark background and crop to it (with padding).
    gray = img.convert("L")
    mean, total, hist = _histogram_stats(gray)

    # key on the brightest tenth of pixels — that is the white label regardless
    # of how dark the overall exposure is — but require it to sit clearly above
    # the background mean so an evenly-lit photo isn't sliced arbitrarily.
    thresh = max(_percentile_intensity(hist, total, 0.90), int(mean) + 10)
    mask = gray.point(lambda p: 255 if p >= thresh else 0)

    bbox = mask.getbbox()
    if bbox is None:
        return img

    left, top, right, bottom = bbox
    w, h = img.size
    area_frac = ((right - left) * (bottom - top)) / (w * h)
    if not (MIN_CROP_AREA_FRAC <= area_frac <= MAX_CROP_AREA_FRAC):
        # degenerate: a glint (too small) or the whole frame (too big). leave it.
        return img

    px, py = int(w * pad_frac), int(h * pad_frac)
    box = (
        max(0, left - px),
        max(0, top - py),
        min(w, right + px),
        min(h, bottom + py),
    )
    return img.crop(box)


# above this mean luminance the crop is bright enough; below it we gamma-correct.
BRIGHT_ENOUGH_MEAN = 110
# where we aim to land the mean when brightening, and how far we let gamma go.
TARGET_MEAN = 150
MIN_GAMMA = 0.4


def _apply_gamma(img, gamma):
    # out = 255 * (in/255)^gamma. gamma < 1 brightens. one lookup table reused
    # across every band.
    table = [min(255, int((i / 255.0) ** gamma * 255 + 0.5)) for i in range(256)]
    return img.point(table * len(img.getbands()))


def brighten(img):
    # stretch the histogram, then gamma-brighten if the result is still dark.
    out = ImageOps.autocontrast(img, cutoff=1)

    mean, _, _ = _histogram_stats(out.convert("L"))
    if mean >= BRIGHT_ENOUGH_MEAN or mean <= 0:
        return out

    # pick the gamma that maps the current mean toward TARGET_MEAN, clamped so an
    # almost-black frame doesn't get pushed to a washed-out grey.
    import math

    gamma = math.log(TARGET_MEAN / 255.0) / math.log(mean / 255.0)
    gamma = max(MIN_GAMMA, min(1.0, gamma))
    return _apply_gamma(out, gamma)


def enhance(img, crop=False):
    # full preprocess: brighten, optionally after a sticker crop. RGB in/out.
    #
    # crop defaults OFF: on the real photos the sticker is NOT the brightest thing
    # (metal-plate highlights are), and at mean-luminance ~12/255 the bright-region
    # threshold can't isolate the label — a bad crop could drop the id entirely.
    # brightness alone makes these near-black frames fully legible, so we take the
    # safe win and leave crop opt-in for a future local-contrast detector.
    img = img.convert("RGB")
    if crop:
        img = crop_to_sticker(img)
    return brighten(img)


def enhance_bytes(image_bytes, crop=False, quality=95):
    # convenience for the pipeline: JPEG bytes in, preprocessed JPEG bytes out.
    import io

    img = Image.open(io.BytesIO(image_bytes))
    out = enhance(img, crop=crop)
    buf = io.BytesIO()
    out.save(buf, format="JPEG", quality=quality)
    return buf.getvalue()

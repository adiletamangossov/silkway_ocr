# -*- coding: utf-8 -*-
"""Generate the warehouse "how to photograph parcels" guide as a 3-page PDF.

One page per language, in order: English, Chinese, Russian. The content is the
same case the before/after eval makes (framing_split.py) — read 81% when the id is
in frame, 0% when it isn't — written for floor staff, not engineers. All artwork is
schematic SVG (no client photos / no PII), so the PDF is safe to share and print.

The link to the hosted version 404s for anyone not signed in to the author's
account; a PDF is the portable hand-out (attach in chat, print and post at the
station).

Renders with a headless Chromium/Edge (auto-detected). The intermediate HTML goes
to a temp dir, so only this script and the PDF live in the repo.

Run:  python make_guide_pdf.py [output.pdf]
      default output: SilkWay_ID_photo_guide.pdf (next to this script)
"""

import os
import subprocess
import sys
import tempfile

# ordered: English, then Chinese, then Russian — one page each.
LANGS = ["en", "zh", "ru"]

STR = {
  "en": {
    "eyebrow": "Parcel ID reader · Warehouse operations",
    "h1": "The reader gets the client ID right <b>81% of the time</b> — when the photo actually shows it.",
    "lede": "One in four parcels never shows the ID number to the camera. That — not the software — is what holds the numbers back. The fix is how we photograph the parcel.",
    "statGood": "read <b>correctly</b> when the ID is clearly in the photo",
    "statBad": "when the ID is <b>too small, cut off, or on the wrong side</b>",
    "secFraming": "The difference is framing, not the camera",
    "badTag": "✕ How parcels look now",
    "badH3": "Parcel lost in a wide shot",
    "badP": "The ID ends up a few pixels wide — or on the routing side, or off the edge. Nothing to read.",
    "svgBad": "can't read",
    "goodTag": "✓ How to photograph them",
    "goodH3": "Receiver label fills the frame",
    "goodP": "The number after 首都波 is big and sharp. This is the read the system needs.",
    "sec3": "Three things when you photograph a parcel",
    "c1b": "Get close — fill the frame with the address label.",
    "c1s": "Hold the phone about a hand's length away so the label covers most of the photo.",
    "c2b": "Show the receiver side — the one with 首都波…号.",
    "c2s": "Not the routing label (广州转 / 航达) and not the product/merchant label. The client's number lives on the receiver-address line.",
    "c3b": "Keep the whole number in the photo.",
    "c3s": "Don't let the parcel or the number run off the edge of the frame.",
    "aside": "Brightness matters less than you'd expect — in our review, well-read and unreadable parcels were equally dark. What separated them was <b>size and the right side of the label</b>, not lighting.",
    "secPayoff": "What better photos would change",
    "m1": "Read correctly",
    "m2": "Sorted automatically — no typing",
    "payoffNote": "Same software, same lighting — just a clearer photo of the number. The grey bar is today; the green line is where better framing takes us.",
    "footer": "Based on <b>50 recent parcels</b> (July 2026). <b>Read correctly</b> = the reader matched the parcel to the right client in the database; <b>sorted automatically</b> = filed with no specialist typing. Print this and post it at the capture station.",
  },
  "zh": {
    "eyebrow": "包裹ID识别 · 仓库操作",
    "h1": "只要照片拍到号码，系统识别客户ID的正确率就有 <b>81%</b>。",
    "lede": "但每四个包裹就有一个根本没把号码拍给相机。真正拖后腿的是这一点，而不是软件。解决办法在于我们如何拍摄包裹。",
    "statGood": "号码清楚拍进照片时，<b>正确</b>识别",
    "statBad": "当号码<b>太小、被裁掉或拍错面</b>时",
    "secFraming": "差别在于取景，而不是相机",
    "badTag": "✕ 现在的拍法",
    "badH3": "包裹淹没在大场景里",
    "badP": "号码只有几个像素宽——或者拍到了转运面，或者被边缘裁掉。没有可识别的内容。",
    "svgBad": "无法识别",
    "goodTag": "✓ 应该这样拍",
    "goodH3": "收件标签占满画面",
    "goodP": "首都波 后面的号码又大又清晰。这正是系统需要的。",
    "sec3": "拍摄包裹时的三条要求",
    "c1b": "靠近——让地址标签占满画面。",
    "c1s": "手机大约放在一掌远，让标签占据照片的大部分。",
    "c2b": "拍收件人一面——有 首都波…号 的那面。",
    "c2s": "不要拍转运标签（广州转 / 航达），也不要拍商品标签。客户号码在收件地址那一行。",
    "c3b": "号码要完整拍进照片。",
    "c3s": "不要让包裹或号码超出画面边缘。",
    "aside": "亮度没有你想的那么重要——在我们的复核中，能识别和不能识别的包裹一样暗。区别在于<b>号码的大小和标签的正确一面</b>，而不是光线。",
    "secPayoff": "更清晰的照片能带来什么",
    "m1": "正确识别",
    "m2": "自动分拣——无需手动输入",
    "payoffNote": "同样的软件，同样的光线——只是把号码拍得更清楚。灰条是现在；绿线是改进取景后能达到的水平。",
    "footer": "基于<b>最近50个包裹</b>（2026年7月）。<b>正确识别</b>＝系统在数据库中把包裹匹配到了正确的客户；<b>自动分拣</b>＝无需专员手动输入即可归档。请打印并张贴在拍摄工位。",
  },
  "ru": {
    "eyebrow": "Считыватель ID посылки · Складские операции",
    "h1": "Система правильно распознаёт ID клиента в <b>81% случаев</b> — когда номер виден на фото.",
    "lede": "Каждая четвёртая посылка вообще не показывает номер камере. Именно это, а не программа, ограничивает результат. Решение — в том, как мы фотографируем посылку.",
    "statGood": "распознаётся <b>правильно</b>, когда ID чётко виден на фото",
    "statBad": "когда ID <b>слишком мелкий, обрезан или снят не с той стороны</b>",
    "secFraming": "Дело в кадре, а не в камере",
    "badTag": "✕ Как фотографируют сейчас",
    "badH3": "Посылка теряется в общем плане",
    "badP": "Номер получается шириной в несколько пикселей — снят со стороны маршрутизации или обрезан краем. Читать нечего.",
    "svgBad": "не читается",
    "goodTag": "✓ Как надо фотографировать",
    "goodH3": "Этикетка получателя заполняет кадр",
    "goodP": "Номер после 首都波 крупный и чёткий. Именно это нужно системе.",
    "sec3": "Три правила при съёмке посылки",
    "c1b": "Подойдите ближе — заполните кадр адресной этикеткой.",
    "c1s": "Держите телефон примерно на расстоянии ладони, чтобы этикетка занимала большую часть кадра.",
    "c2b": "Снимайте сторону получателя — ту, где 首都波…号.",
    "c2s": "Не этикетку маршрутизации (广州转 / 航达) и не этикетку товара. Номер клиента — в строке адреса получателя.",
    "c3b": "Номер должен полностью попадать в кадр.",
    "c3s": "Не допускайте, чтобы посылка или номер выходили за край кадра.",
    "aside": "Яркость важна меньше, чем кажется — в нашем анализе хорошо распознанные и нечитаемые посылки были одинаково тёмными. Разницу давали <b>размер и правильная сторона этикетки</b>, а не освещение.",
    "secPayoff": "Что изменят более чёткие фото",
    "m1": "Распознано правильно",
    "m2": "Отсортировано автоматически — без ручного ввода",
    "payoffNote": "Та же программа, то же освещение — просто более чёткое фото номера. Серая полоса — сегодня; зелёная линия — куда выведет правильный кадр.",
    "footer": "На основе <b>50 недавних посылок</b> (июль 2026). <b>Распознано правильно</b> — система сопоставила посылку с нужным клиентом в базе; <b>отсортировано автоматически</b> — оформлено без ручного ввода. Распечатайте и повесьте на месте съёмки.",
  },
}

CSS = """
<style>
  :root {
    --bg:#fff; --surface:#fff; --surface-2:#f3f5f8; --ink:#172029; --muted:#566270; --line:#d8dde3;
    --accent:#1f5fd6; --accent-soft:#e5edfb; --good:#12793f; --good-soft:#e2f3e9; --good-line:#b7e0c6;
    --bad:#b1421c; --bad-soft:#fae7de; --bad-line:#eec3b1; --desk:#3a4048; --kraft:#c8a879;
    --mono:"SF Mono","Cascadia Code","JetBrains Mono",Menlo,Consolas,monospace;
  }
  @page { size: A4; margin: 10mm; }
  * { box-sizing:border-box; -webkit-print-color-adjust:exact; print-color-adjust:exact; }
  body { margin:0; background:#fff; color:var(--ink);
    font:12.5px/1.44 system-ui,-apple-system,"Segoe UI",Roboto,"PingFang SC","Microsoft YaHei","Noto Sans CJK SC",sans-serif; }
  .sheet { max-width:190mm; margin:0 auto; page-break-after:always; }
  .sheet:last-child { page-break-after:auto; }
  .pad { padding:2px 0 6px; }
  .tnum { font-variant-numeric:tabular-nums; }
  .brand { display:flex; align-items:center; gap:9px; margin-bottom:9px; padding-bottom:8px; border-bottom:1px solid var(--line); }
  .brand .mark { width:24px; height:24px; border-radius:7px; background:var(--accent); display:grid; place-items:center; flex:none; }
  .brand .name { font-size:16px; font-weight:800; letter-spacing:-.015em; }
  .brand .name span { color:var(--accent); }
  .eyebrow { font-size:9.5px; letter-spacing:.16em; text-transform:uppercase; color:var(--accent); font-weight:700; }
  h1 { font-size:19px; line-height:1.18; margin:6px 0 4px; letter-spacing:-.01em; }
  h1 b { color:var(--accent); }
  .lede { color:var(--muted); font-size:12.5px; margin:0; }
  .hero { display:grid; grid-template-columns:1fr 1fr; gap:10px; margin-top:10px; }
  .stat { border-radius:10px; padding:10px 13px; border:1px solid; }
  .stat.good { background:var(--good-soft); border-color:var(--good-line); }
  .stat.bad { background:var(--bad-soft); border-color:var(--bad-line); }
  .stat .num { font:800 30px/1 var(--mono); font-variant-numeric:tabular-nums; letter-spacing:-1px; }
  .stat.good .num { color:var(--good); } .stat.bad .num { color:var(--bad); }
  .stat .cap { margin-top:4px; font-size:11.5px; }
  .section-label { font-size:9.5px; letter-spacing:.14em; text-transform:uppercase; color:var(--muted); font-weight:700; margin:11px 0 7px; }
  .panels { display:grid; grid-template-columns:1fr 1fr; gap:10px; }
  .panel { border:1px solid var(--line); border-radius:10px; overflow:hidden; background:var(--surface-2); }
  .panel .art { width:100%; display:block; }
  .panel .body { padding:9px 11px 11px; }
  .panel .tag { display:inline-block; font-size:11px; font-weight:700; padding:3px 9px; border-radius:999px; }
  .panel.bad .tag { color:var(--bad); background:var(--bad-soft); border:1px solid var(--bad-line); }
  .panel.good .tag { color:var(--good); background:var(--good-soft); border:1px solid var(--good-line); }
  .panel h3 { font-size:13px; margin:7px 0 3px; } .panel p { margin:0; font-size:11.5px; color:var(--muted); }
  .rule { background:var(--surface-2); border:1px solid var(--line); border-radius:10px; padding:11px 13px; margin-top:11px; }
  .rule .section-label { margin-top:0; }
  .check { display:grid; grid-template-columns:22px 1fr; gap:10px; align-items:start; margin-bottom:7px; }
  .check .mk { width:20px; height:20px; border-radius:6px; background:var(--good); color:#fff; display:grid; place-items:center; font-weight:800; font-size:12px; }
  .check .t b { font-size:13px; } .check .t span { display:block; color:var(--muted); font-size:11.5px; margin-top:1px; }
  .aside { margin-top:8px; font-size:11.5px; color:var(--muted); padding:8px 11px; border-left:3px solid var(--accent); background:var(--accent-soft); border-radius:0 8px 8px 0; }
  .aside b { color:var(--ink); }
  .metric { margin-bottom:9px; } .metric:last-child { margin-bottom:0; }
  .metric .lab { display:flex; justify-content:space-between; gap:10px; font-size:11.5px; margin-bottom:4px; }
  .metric .lab b { font-weight:700; white-space:nowrap; }
  .track { position:relative; height:20px; border-radius:6px; background:var(--surface-2); border:1px solid var(--line); overflow:hidden; }
  .fill { position:absolute; top:0; left:0; bottom:0; border-radius:5px 0 0 5px; display:flex; align-items:center; padding-left:9px; color:#fff; font:700 11px/1 var(--mono); font-variant-numeric:tabular-nums; background:var(--muted); }
  .goalmark { position:absolute; top:-3px; bottom:-3px; width:3px; background:var(--good); }
  .payoffnote { margin:7px 0 0; color:var(--muted); font-size:11.5px; }
  footer { font-size:10.5px; color:var(--muted); margin-top:10px; padding-top:8px; border-top:1px solid var(--line); }
  footer b { color:var(--ink); }
</style>
"""

BRAND = ('<div class="brand"><span class="mark">'
  '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="#fff" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">'
  '<path d="M3 8l9-4 9 4-9 4-9-4z"/><path d="M3 8v8l9 4 9-4V8"/><path d="M12 12v8"/></svg>'
  '</span><span class="name">Silk<span>Way</span></span></div>')

BAD_SVG = ('<svg class="art" viewBox="0 0 400 175"><rect width="400" height="175" fill="var(--desk)"/>'
  '<rect x="8" y="8" width="384" height="159" fill="none" stroke="var(--bad)" stroke-width="2" stroke-dasharray="6 5" opacity=".7"/>'
  '<g transform="translate(292 92) rotate(-9)"><rect x="0" y="0" width="96" height="72" rx="4" fill="var(--kraft)"/>'
  '<rect x="11" y="42" width="74" height="24" rx="2" fill="#f4f1ea"/><rect x="15" y="47" width="42" height="3.5" fill="#9a938a"/>'
  '<g fill="#3a3a3a"><rect x="15" y="54" width="1.5" height="8"/><rect x="18" y="54" width="1" height="8"/>'
  '<rect x="20" y="54" width="2" height="8"/><rect x="23.5" y="54" width="1" height="8"/><rect x="26" y="54" width="2.5" height="8"/>'
  '<rect x="30" y="54" width="1" height="8"/><rect x="32.5" y="54" width="1.5" height="8"/><rect x="35.5" y="54" width="2" height="8"/></g></g>'
  '<circle cx="330" cy="128" r="24" fill="none" stroke="#888" stroke-width="1.5"/>'
  '<line x1="313" y1="111" x2="250" y2="78" stroke="#888" stroke-width="1.5"/>'
  '<circle cx="212" cy="70" r="36" fill="#20242a" stroke="#fff" stroke-width="1.5"/>'
  '<text x="212" y="66" text-anchor="middle" font-family="var(--mono,monospace)" font-size="15" fill="var(--bad)" font-weight="700">9??0?</text>'
  '<text x="212" y="84" text-anchor="middle" font-family="system-ui" font-size="8.5" fill="#c9ced6">%(svgBad)s</text></svg>')

GOOD_SVG = ('<svg class="art" viewBox="0 0 400 175"><rect width="400" height="175" fill="var(--desk)"/>'
  '<rect x="24" y="12" width="352" height="151" rx="6" fill="#f7f5ef"/><rect x="24" y="12" width="352" height="26" rx="6" fill="#e7e2d6"/>'
  '<text x="40" y="31" font-family="system-ui" font-size="14" font-weight="800" fill="#2a2a2a">ZTO 中通快递</text>'
  '<g fill="#2a2a2a"><rect x="40" y="50" width="2" height="22"/><rect x="44" y="50" width="1.5" height="22"/>'
  '<rect x="47.5" y="50" width="3" height="22"/><rect x="52" y="50" width="1.5" height="22"/><rect x="55.5" y="50" width="2.5" height="22"/>'
  '<rect x="60" y="50" width="1.5" height="22"/><rect x="63.5" y="50" width="3.5" height="22"/><rect x="69" y="50" width="1.5" height="22"/>'
  '<rect x="73" y="50" width="2" height="22"/><rect x="77" y="50" width="3" height="22"/></g>'
  '<text x="200" y="65" font-family="var(--mono,monospace)" font-size="10" fill="#7a736a" letter-spacing="1">7912 0075 34B4</text>'
  '<text x="40" y="90" font-family="system-ui" font-size="10.5" fill="#8a8378">收 广东省佛山市南海区里水镇</text>'
  '<rect x="32" y="100" width="336" height="48" rx="8" fill="#eaf5ee" stroke="var(--good)" stroke-width="2"/>'
  '<text x="46" y="131" font-family="system-ui" font-size="16" fill="#2a2a2a">库区首都波'
  '<tspan font-family="var(--mono,monospace)" font-size="24" font-weight="800" fill="var(--good)" letter-spacing="1"> 960662 </tspan>号</text>'
  '<circle cx="346" cy="124" r="12" fill="var(--good)"/>'
  '<path d="M340 124 l4 4 l7 -8" fill="none" stroke="#fff" stroke-width="2.3" stroke-linecap="round" stroke-linejoin="round"/></svg>')


def sheet(s):
    bad_svg = BAD_SVG % {"svgBad": s["svgBad"]}
    return f"""
<div class="sheet"><div class="pad">
  {BRAND}
  <div class="eyebrow">{s['eyebrow']}</div>
  <h1>{s['h1']}</h1>
  <p class="lede">{s['lede']}</p>
  <div class="hero">
    <div class="stat good"><div class="num tnum">81%</div><div class="cap">{s['statGood']}</div></div>
    <div class="stat bad"><div class="num tnum">0%</div><div class="cap">{s['statBad']}</div></div>
  </div>
  <p class="section-label">{s['secFraming']}</p>
  <div class="panels">
    <div class="panel bad">{bad_svg}<div class="body"><span class="tag">{s['badTag']}</span><h3>{s['badH3']}</h3><p>{s['badP']}</p></div></div>
    <div class="panel good">{GOOD_SVG}<div class="body"><span class="tag">{s['goodTag']}</span><h3>{s['goodH3']}</h3><p>{s['goodP']}</p></div></div>
  </div>
  <div class="rule">
    <p class="section-label">{s['sec3']}</p>
    <div class="check"><div class="mk">✓</div><div class="t"><b>{s['c1b']}</b><span>{s['c1s']}</span></div></div>
    <div class="check"><div class="mk">✓</div><div class="t"><b>{s['c2b']}</b><span>{s['c2s']}</span></div></div>
    <div class="check"><div class="mk">✓</div><div class="t"><b>{s['c3b']}</b><span>{s['c3s']}</span></div></div>
    <div class="aside">{s['aside']}</div>
  </div>
  <p class="section-label">{s['secPayoff']}</p>
  <div class="metric"><div class="lab"><span>{s['m1']}</span><b class="tnum">60% → ~81%</b></div>
    <div class="track"><div class="fill" style="width:60%">60%</div><div class="goalmark" style="left:81%"></div></div></div>
  <div class="metric"><div class="lab"><span>{s['m2']}</span><b class="tnum">40% → ~54%</b></div>
    <div class="track"><div class="fill" style="width:40%">40%</div><div class="goalmark" style="left:54%"></div></div></div>
  <p class="payoffnote">{s['payoffNote']}</p>
  <footer>{s['footer']}</footer>
</div></div>"""


def build_html() -> str:
    sheets = "".join(sheet(STR[lang]) for lang in LANGS)
    return ("<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
            "<title>SilkWay — how to photograph parcels</title>" + CSS
            + "</head><body>" + sheets + "</body></html>")


def find_browser():
    # a headless Chromium/Edge to render the PDF; common install paths per OS.
    candidates = [
        os.environ.get("BROWSER_PATH"),
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/usr/bin/google-chrome", "/usr/bin/chromium", "/usr/bin/chromium-browser",
    ]
    for c in candidates:
        if c and os.path.exists(c):
            return c
    return None


def main():
    out_pdf = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "SilkWay_ID_photo_guide.pdf")

    # write the print HTML to a temp file; only the script + PDF belong in the repo.
    with tempfile.NamedTemporaryFile("w", suffix=".html", delete=False, encoding="utf-8") as f:
        f.write(build_html())
        html_path = f.name

    browser = find_browser()
    if not browser:
        # no headless browser found — leave the HTML and print how to render it.
        keep = os.path.splitext(out_pdf)[0] + ".html"
        os.replace(html_path, keep)
        print(f"no Chromium/Edge found. wrote HTML: {keep}")
        print("render it with:  <chrome-or-edge> --headless --print-to-pdf=out.pdf "
              "--no-pdf-header-footer file:///<path-to-html>")
        return

    subprocess.run(
        [browser, "--headless", "--disable-gpu", "--no-pdf-header-footer",
         f"--print-to-pdf={out_pdf}", "file:///" + html_path.replace("\\", "/")],
        check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    os.unlink(html_path)
    print(f"wrote {out_pdf} ({os.path.getsize(out_pdf) // 1024} KB) — English, Chinese, Russian")


if __name__ == "__main__":
    main()

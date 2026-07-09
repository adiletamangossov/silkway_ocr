"""Benchmark Qwen3-VL served via vLLM (vs the transformers .generate path).

Same model, same prompt — but vLLM's paged attention + optimized kernels are
typically 3-5x faster at generation than HuggingFace transformers. This is a
standalone Modal app (own image) so the deployed modal_app.py is untouched; it
only measures latency.

Run:  modal run vllm_bench.py::bench
      SILKWAY_GPU=A10G modal run vllm_bench.py::bench   (default A10G)
"""

import os

import modal

MODEL_ID = "Qwen/Qwen3-VL-8B-Instruct"
GPU = os.environ.get("SILKWAY_GPU", "A10G")

# same read-only prompt the prod pipeline uses.
TRANSCRIBE_PROMPT = (
    "Transcribe all text on this shipping label exactly as printed, "
    "including every Chinese character and every digit, line by line. "
    "Do not translate, summarize, reorder, or correct anything. "
    "If a character is unclear, output your best literal reading of what is visible."
)

# vLLM JIT-compiles CUDA components at engine init, so the base image must ship the
# CUDA toolkit (nvcc) — debian_slim doesn't. use the official CUDA *devel* image.
vllm_image = (
    modal.Image.from_registry(
        "nvidia/cuda:12.8.1-devel-ubuntu22.04", add_python="3.12"
    )
    .entrypoint([])
    .pip_install("vllm", "transformers>=4.57", "pillow", "accelerate")
    .env({"HF_HOME": "/cache"})
)
hf_cache = modal.Volume.from_name("silkway-hf-cache", create_if_missing=True)

app = modal.App("silkway-vllm-bench")


@app.cls(
    image=vllm_image,
    gpu=GPU,
    volumes={"/cache": hf_cache},
    scaledown_window=300,
    timeout=1800,
    memory=16384,
)
class QwenVLLM:
    @modal.enter()
    def load(self):
        from transformers import AutoProcessor
        from vllm import LLM

        self.processor = AutoProcessor.from_pretrained(MODEL_ID)
        # 8B in bf16 (~16GB) + KV cache + vision fits a 24GB A10G with room to spare.
        self.llm = LLM(
            model=MODEL_ID,
            max_model_len=8192,
            gpu_memory_utilization=0.92,
            limit_mm_per_prompt={"image": 1},
            trust_remote_code=True,
            # the slim image has no CUDA toolkit (nvcc), so disable torch.compile /
            # CUDA-graph capture that would try to JIT-compile at startup. slightly
            # slower than graphs, but avoids the nvcc dependency and still fast.
            enforce_eager=True,
        )

    @modal.method()
    def transcribe(self, image_bytes: bytes, prompt: str) -> str:
        import io

        from PIL import Image
        from vllm import SamplingParams

        img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
        max_side = int(os.environ.get("MAX_IMAGE_SIDE", "0"))
        if max_side and max(img.size) > max_side:
            img.thumbnail((max_side, max_side))

        messages = [
            {"role": "user", "content": [
                {"type": "image"},
                {"type": "text", "text": prompt},
            ]}
        ]
        text = self.processor.apply_chat_template(
            messages, tokenize=False, add_generation_prompt=True
        )
        # greedy (temperature 0), repetition penalty to avoid the dark-image loops.
        sp = SamplingParams(
            temperature=0.0,
            max_tokens=int(os.environ.get("MAX_NEW_TOKENS", "512")),
            repetition_penalty=1.1,
        )
        out = self.llm.generate(
            {"prompt": text, "multi_modal_data": {"image": img}}, sp
        )
        return out[0].outputs[0].text.strip()


@app.local_entrypoint()
def bench(image_path: str = "image_silkway.jpeg", n: int = 6):
    import time

    with open(image_path, "rb") as f:
        image_bytes = f.read()

    ocr = QwenVLLM()
    times = []
    for i in range(n):
        t0 = time.time()
        text = ocr.transcribe.remote(image_bytes, TRANSCRIBE_PROMPT)
        dt = time.time() - t0
        times.append(dt)
        print(f"call {i + 1}: {dt:5.2f}s  ({len(text)} chars)")

    warm = times[1:] or times
    ws = sorted(warm)
    print(f"\n[vLLM on {GPU}]  cold: {times[0]:.2f}s   warm median: {ws[len(ws)//2]:.2f}s   "
          f"warm min: {min(warm):.2f}s   warm max: {max(warm):.2f}s")

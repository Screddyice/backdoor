"""
Modal app: vLLM OpenAI-compatible endpoint serving Qwen3-32B on 1x H100.

Deploy:    modal deploy qwen3_vllm.py
Prewarm:   modal run qwen3_vllm.py::download_model     # cache weights to the Volume first
Endpoint:  https://<workspace>--qwen3-vllm-serve.modal.run/v1   (OpenAI-compatible; bearer-token auth)

Scale-to-zero: GPU spins up on first request, drops after ~5 min idle (you pay only while warm).
"""

import modal

MODEL_NAME = "Qwen/Qwen3-32B"        # dense 32B; swap to "Qwen/Qwen3-30B-A3B" for the faster/cheaper MoE
SERVED_NAME = "qwen3-32b"
VLLM_PORT = 8000
MIN = 60  # seconds

vllm_image = (
    modal.Image.debian_slim(python_version="3.12")
    .pip_install("vllm", "huggingface_hub[hf_transfer]")
    # hf_transfer = fast parallel weight download; v1 engine
    .env({"VLLM_USE_FLASHINFER_SAMPLER": "0", "VLLM_USE_DEEP_GEMM": "0", "HF_XET_HIGH_PERFORMANCE": "1"})
)

hf_cache = modal.Volume.from_name("huggingface-cache", create_if_missing=True)
vllm_cache = modal.Volume.from_name("vllm-cache", create_if_missing=True)

app = modal.App("qwen3-vllm")


@app.function(
    image=vllm_image,
    volumes={"/root/.cache/huggingface": hf_cache},
    timeout=60 * MIN,
)
def download_model():
    """Cache the weights to the Volume once so the first real request isn't a cold download."""
    from huggingface_hub import snapshot_download

    snapshot_download(MODEL_NAME, ignore_patterns=["*.pt", "*.pth"])
    hf_cache.commit()
    print(f"cached {MODEL_NAME} to huggingface-cache volume")


@app.function(
    image=vllm_image,
    gpu="H100",
    scaledown_window=5 * MIN,          # idle window before scale-to-zero
    timeout=60 * MIN,
    volumes={"/root/.cache/huggingface": hf_cache, "/root/.cache/vllm": vllm_cache},
    secrets=[modal.Secret.from_name("vllm-api-key")],
)
@modal.concurrent(max_inputs=8)
@modal.web_server(port=VLLM_PORT, startup_timeout=30 * MIN)
def serve():
    import os
    import subprocess

    cmd = [
        "vllm", "serve", MODEL_NAME,
        "--host", "0.0.0.0",
        "--port", str(VLLM_PORT),
        "--api-key", os.environ["VLLM_API_KEY"],
        "--served-model-name", SERVED_NAME,
        "--max-model-len", "16384",
        "--gpu-memory-utilization", "0.92",
        "--enable-auto-tool-choice",    # function/tool calling for the agent
        "--tool-call-parser", "hermes",
        "--reasoning-parser", "qwen3",  # separates <think> into reasoning_content -> clean answer text
    ]
    subprocess.Popen(" ".join(cmd), shell=True)

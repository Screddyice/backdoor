from __future__ import annotations

from pathlib import Path

import pytest

from src.proxy.context_tokenizer import ContextLimitError, QwenTokenGate


@pytest.fixture
def fake_tokenizer(tmp_path) -> Path:
    executable = tmp_path / "llama-tokenize"
    executable.write_text(
        "#!/bin/sh\n"
        "printf '%s\\n' \"${FAKE_TOKEN_OUTPUT:-tokens: ${FAKE_TOKEN_COUNT:-7}}\"\n",
        encoding="utf-8",
    )
    executable.chmod(0o755)
    return executable


@pytest.fixture
def model_path(tmp_path) -> Path:
    path = tmp_path / "model.gguf"
    path.write_bytes(b"GGUF")
    return path


def test_token_gate_uses_configured_tokenizer_when_its_gguf_is_available(fake_tokenizer, model_path):
    gate = QwenTokenGate(executable=str(fake_tokenizer), model_path=str(model_path))

    result = gate.count({"messages": [{"role": "user", "content": "hello"}]})

    assert result.tokens == 7
    assert result.exact is True
    assert result.method == "llama-tokenize"


def test_token_gate_parses_llama_tokenize_completion_output(fake_tokenizer, model_path, monkeypatch):
    monkeypatch.setenv("FAKE_TOKEN_OUTPUT", "tokenized 12 tokens in 1.0ms")
    gate = QwenTokenGate(executable=str(fake_tokenizer), model_path=str(model_path))

    result = gate.count({"messages": [{"role": "user", "content": "hello"}]})

    assert result.tokens == 12
    assert result.exact is True


def test_token_gate_counts_full_utf8_bytes_when_exact_tokenizer_is_unavailable():
    payload = {"messages": [{"role": "user", "content": "é"}]}
    gate = QwenTokenGate(executable="/missing/llama-tokenize", model_path="/missing/model.gguf")

    result = gate.count(payload)

    assert result.exact is False
    assert result.method == "utf8_bytes"
    assert result.tokens == len(gate.render(payload).encode("utf-8"))


def test_token_gate_rejects_payload_above_hard_limit(fake_tokenizer, model_path, monkeypatch):
    monkeypatch.setenv("FAKE_TOKEN_COUNT", "22001")
    gate = QwenTokenGate(executable=str(fake_tokenizer), model_path=str(model_path))

    with pytest.raises(ContextLimitError):
        gate.require_fit({"messages": [{"content": "large"}]}, hard_tokens=22_000)


def test_token_gate_rejects_an_unproven_byte_fallback_above_the_hard_limit():
    gate = QwenTokenGate(executable="/missing/llama-tokenize", model_path="/missing/model.gguf")

    with pytest.raises(ContextLimitError):
        gate.require_fit({"messages": [{"content": "x" * 100}]}, hard_tokens=10)

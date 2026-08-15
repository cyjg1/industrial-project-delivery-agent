from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import tiktoken
from tiktoken.load import load_tiktoken_bpe


_CL100K_HASH = "223921b76ee99bde995b7ff738513eef100fb51d18c93597a113bcffe865b2a7"
_CL100K_PATTERN = (
    r"'(?i:[sdmt]|ll|ve|re)|[^\r\n\p{L}\p{N}]?+\p{L}++|"
    r"\p{N}{1,3}+| ?[^\s\p{L}\p{N}]++[\r\n]*+|"
    r"\s++$|\s*[\r\n]|\s+(?!\S)|\s"
)


@dataclass(frozen=True)
class TokenCounter:
    """Counts and truncates text with an explicit tokenizer."""

    encoding_name: str

    def __post_init__(self) -> None:
        _load_encoding(self.encoding_name)

    def count(self, text: str) -> int:
        return len(self._encoding().encode(text or "", disallowed_special=()))

    def truncate(self, text: str, max_tokens: int) -> str:
        value = text or ""
        budget = max(0, int(max_tokens))
        if budget == 0:
            return ""
        if self.count(value) <= budget:
            return value
        low = 0
        high = len(value)
        while low < high:
            midpoint = (low + high + 1) // 2
            if self.count(value[:midpoint]) <= budget:
                low = midpoint
            else:
                high = midpoint - 1
        return value[:low].rstrip()

    def _encoding(self):
        return _load_encoding(self.encoding_name)


@lru_cache(maxsize=8)
def _load_encoding(encoding_name: str):
    if encoding_name != "cl100k_base":
        return tiktoken.get_encoding(encoding_name)
    vocabulary_path = (
        Path(__file__).resolve().parent
        / "encodings"
        / "cl100k_base.tiktoken"
    )
    if not vocabulary_path.is_file():
        raise RuntimeError(
            f"Bundled tokenizer vocabulary is missing: {vocabulary_path}"
        )
    mergeable_ranks = load_tiktoken_bpe(
        str(vocabulary_path),
        expected_hash=_CL100K_HASH,
    )
    return tiktoken.Encoding(
        name="cl100k_base",
        pat_str=_CL100K_PATTERN,
        mergeable_ranks=mergeable_ranks,
        special_tokens={
            "<|endoftext|>": 100257,
            "<|fim_prefix|>": 100258,
            "<|fim_middle|>": 100259,
            "<|fim_suffix|>": 100260,
            "<|endofprompt|>": 100276,
        },
    )


def get_token_counter() -> TokenCounter:
    encoding_name = os.getenv("CONTEXT_TOKEN_ENCODING", "cl100k_base").strip()
    if not encoding_name:
        raise RuntimeError("CONTEXT_TOKEN_ENCODING must name an installed tiktoken encoding")
    try:
        return TokenCounter(encoding_name=encoding_name)
    except Exception as exc:
        raise RuntimeError(
            f"Unable to initialize context tokenizer {encoding_name!r}; "
            "install requirements.txt and configure a valid tiktoken encoding"
        ) from exc

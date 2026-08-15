from __future__ import annotations

import bisect
import hashlib
import io
import re
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class TranscriptChunk:
    index: int
    start_char: int
    end_char: int
    text: str
    content_hash: str


def parse_transcript(path: str | Path) -> str:
    source_path = Path(path)
    suffix = source_path.suffix.lower()
    if suffix == ".txt":
        return _decode_text_bytes(source_path.read_bytes())
    if suffix == ".docx":
        return _parse_docx(source_path)
    raise ValueError(f"不支持的转写文件格式：{suffix}，请使用 .txt 或 .docx")


def parse_transcript_bytes(content: bytes, filename: str) -> str:
    suffix = Path(filename).suffix.lower()
    if suffix == ".txt":
        return _decode_text_bytes(content)
    if suffix == ".docx":
        return _parse_docx_bytes(content)
    raise ValueError(f"不支持的转写文件格式：{suffix}，请使用 .txt 或 .docx")


def estimate_tokens(text: str) -> int:
    chinese_chars = len(re.findall(r"[\u4e00-\u9fff]", text))
    other_chars = len(text) - chinese_chars
    return int(chinese_chars / 1.5 + other_chars / 4)


def split_transcript_by_speaker(text: str, max_chunk_tokens: int = 8000) -> list[str]:
    return [
        chunk.text
        for chunk in chunk_transcript(
            text,
            max_chunk_tokens=max_chunk_tokens,
            overlap_chars=0,
        )
    ]


def chunk_transcript(
    text: str,
    *,
    max_chunk_tokens: int = 8000,
    overlap_chars: int = 200,
) -> list[TranscriptChunk]:
    if max_chunk_tokens <= 0:
        raise ValueError("max_chunk_tokens must be positive")
    if overlap_chars < 0:
        raise ValueError("overlap_chars cannot be negative")
    if not text:
        return []

    boundaries = [index + 1 for index, char in enumerate(text) if char == "\n"]
    if not boundaries or boundaries[-1] != len(text):
        boundaries.append(len(text))

    chunks: list[TranscriptChunk] = []
    start = 0
    while start < len(text):
        hard_end = _furthest_token_bounded_end(text, start, max_chunk_tokens)
        boundary_index = bisect.bisect_right(boundaries, hard_end) - 1
        preferred_end = boundaries[boundary_index] if boundary_index >= 0 else start
        if hard_end == len(text) and start > 0 and text[start - 1] != "\n":
            next_boundary_index = bisect.bisect_right(boundaries, start)
            if next_boundary_index < len(boundaries) and boundaries[next_boundary_index] < len(text):
                preferred_end = boundaries[next_boundary_index]
        end = preferred_end if preferred_end > start else hard_end
        if end <= start:
            end = min(len(text), start + 1)

        chunk_text = text[start:end]
        chunks.append(
            TranscriptChunk(
                index=len(chunks),
                start_char=start,
                end_char=end,
                text=chunk_text,
                content_hash=hashlib.sha256(chunk_text.encode("utf-8")).hexdigest(),
            )
        )
        if end >= len(text):
            break
        bounded_overlap = min(overlap_chars, max(0, (end - start) // 3))
        start = end - bounded_overlap

    coverage = transcript_coverage(text, chunks)
    if coverage["uncovered_chars"]:
        raise RuntimeError(
            f"Transcript chunking left {coverage['uncovered_chars']} source characters uncovered"
        )
    return chunks


def transcript_coverage(text: str, chunks: list[TranscriptChunk]) -> dict[str, int]:
    ranges: list[tuple[int, int]] = []
    for chunk in chunks:
        if chunk.start_char < 0 or chunk.end_char > len(text) or chunk.end_char <= chunk.start_char:
            raise ValueError(f"Invalid transcript chunk range: {chunk.start_char}:{chunk.end_char}")
        expected = text[chunk.start_char:chunk.end_char]
        if chunk.text != expected:
            raise ValueError(f"Transcript chunk {chunk.index} text does not match its source range")
        expected_hash = hashlib.sha256(expected.encode("utf-8")).hexdigest()
        if chunk.content_hash != expected_hash:
            raise ValueError(f"Transcript chunk {chunk.index} content hash does not match")
        ranges.append((chunk.start_char, chunk.end_char))

    ranges.sort()
    covered = 0
    merged_end = 0
    total_span_chars = 0
    for start, end in ranges:
        total_span_chars += end - start
        if end <= merged_end:
            continue
        covered += end - max(start, merged_end)
        merged_end = end
    return {
        "source_chars": len(text),
        "covered_chars": covered,
        "uncovered_chars": max(0, len(text) - covered),
        "overlap_chars": max(0, total_span_chars - covered),
    }


def _furthest_token_bounded_end(text: str, start: int, max_chunk_tokens: int) -> int:
    if estimate_tokens(text[start:]) <= max_chunk_tokens:
        return len(text)
    low = start + 1
    high = len(text)
    best = start + 1
    while low <= high:
        middle = (low + high) // 2
        if estimate_tokens(text[start:middle]) <= max_chunk_tokens:
            best = middle
            low = middle + 1
        else:
            high = middle - 1
    return best


def needs_preprocessing(text: str, threshold_tokens: int = 20000) -> bool:
    return estimate_tokens(text) > threshold_tokens


def _parse_docx(path: Path) -> str:
    from docx import Document

    document = Document(str(path))
    return _join_docx_paragraphs(document)


def _parse_docx_bytes(content: bytes) -> str:
    from docx import Document

    document = Document(io.BytesIO(content))
    return _join_docx_paragraphs(document)


def _join_docx_paragraphs(document: object) -> str:
    return "\n".join(
        paragraph.text
        for paragraph in getattr(document, "paragraphs", [])
        if paragraph.text.strip()
    )


def _decode_text_bytes(content: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-8", "gb18030", "gbk"):
        try:
            return content.decode(encoding)
        except UnicodeDecodeError:
            continue
    return content.decode("utf-8", errors="replace")

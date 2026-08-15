# Bundled Tokenizer Vocabulary

`cl100k_base.tiktoken` is the OpenAI `cl100k_base` mergeable-ranks file used by
the `tiktoken` package.

- Upstream URL: `https://openaipublic.blob.core.windows.net/encodings/cl100k_base.tiktoken`
- SHA-256: `223921b76ee99bde995b7ff738513eef100fb51d18c93597a113bcffe865b2a7`
- Purpose: deterministic offline context-budget accounting

`agent.memory.token_budget` verifies this hash before constructing the encoding.
The project does not silently switch to character estimates when the vocabulary
is missing or invalid.

# Retrieval Evaluation Dataset Format

Use one JSON object per line (JSONL).

Required fields per row:
- id: unique question id (for example q001)
- question: user question
- evidence_anchor: human-readable location in source markdown (for example section heading)
- evidence_quote: exact quote that proves the answer
- gold_answer: short target answer
- source_document: relative path of the source markdown

Optional fields per row:
- notes: free text for assumptions, variants, constraints

Why this format:
- It is stable even if chunking changes.
- Relevant chunk ids can be derived per run by matching chunks to evidence_quote.

Practical guidance:
- 2 questions: smoke test only.
- 10-15 questions: good first benchmark.
- 30+ questions: better statistical stability.

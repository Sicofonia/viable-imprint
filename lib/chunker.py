def chunk_by_paragraphs(text: str, max_chars: int = 8000) -> list:
    """Split text at paragraph boundaries, keeping each chunk under max_chars.

    Historical texts exceed any LLM context window. We chunk at double-newline
    boundaries so the LLM never receives a truncated paragraph, which would produce
    garbled or incomplete markup at the seam.
    """
    paragraphs = text.split("\n\n")
    chunks = []
    current: list = []
    current_len = 0

    for para in paragraphs:
        # +2 accounts for the \n\n separator we'll put back
        para_len = len(para) + 2
        if current_len + para_len > max_chars and current:
            chunks.append("\n\n".join(current))
            current = []
            current_len = 0
        current.append(para)
        current_len += para_len

    if current:
        chunks.append("\n\n".join(current))

    return chunks

import re
from typing import List


def chunk_markdown(content: str, chunk_size: int = 512, overlap: int = 50) -> List[str]:
    """
    Markdown 切片策略：
    1. 按段落（\n\n）优先切分
    2. 单个段落超过 chunk_size，按句子切分
    3. 切片之间保留 overlap 字符，保证语义连续性
    """
    if not content or not content.strip():
        return []

    # 清洗：去除多余空白
    content = re.sub(r'\n{3,}', '\n\n', content).strip()

    # 按段落分割
    paragraphs = re.split(r'\n\n+', content)
    paragraphs = [p.strip() for p in paragraphs if p.strip()]

    chunks = []
    current_chunk = ""

    for para in paragraphs:
        para = para.strip()
        if not para:
            continue

        # 如果当前段落本身就超过 chunk_size，需要进一步切分
        if len(para) > chunk_size:
            # 先保存当前积累的 chunk
            if current_chunk:
                chunks.append(current_chunk.strip())
                # 保留 overlap
                if overlap > 0 and len(current_chunk) > overlap:
                    current_chunk = current_chunk[-overlap:]
                else:
                    current_chunk = ""

            # 按句子切分长段落
            sentences = re.split(r'(?<=[。！？.!?])\s+', para)
            for sentence in sentences:
                sentence = sentence.strip()
                if not sentence:
                    continue

                if len(current_chunk) + len(sentence) + 1 <= chunk_size:
                    current_chunk = (current_chunk + " " + sentence).strip() if current_chunk else sentence
                else:
                    if current_chunk:
                        chunks.append(current_chunk.strip())
                        # 保留 overlap
                        if overlap > 0 and len(current_chunk) > overlap:
                            current_chunk = current_chunk[-overlap:] + " " + sentence
                        else:
                            current_chunk = sentence
                    else:
                        # 单个句子就超过 chunk_size，强制切分
                        chunks.append(sentence[:chunk_size])
                        current_chunk = sentence[chunk_size - overlap:] if len(sentence) > chunk_size else ""
        else:
            # 普通段落，尝试合并到当前 chunk
            if len(current_chunk) + len(para) + 2 <= chunk_size:
                current_chunk = (current_chunk + "\n\n" + para).strip() if current_chunk else para
            else:
                if current_chunk:
                    chunks.append(current_chunk.strip())
                    # 保留 overlap
                    if overlap > 0 and len(current_chunk) > overlap:
                        current_chunk = current_chunk[-overlap:] + "\n\n" + para
                    else:
                        current_chunk = para
                else:
                    current_chunk = para

    # 保存最后一个 chunk
    if current_chunk:
        chunks.append(current_chunk.strip())

    # 过滤空 chunk
    chunks = [c for c in chunks if c.strip()]

    return chunks

"""
Markdown 切片策略（标题感知版）

核心思路：
  1) 以 ### 标题为分段边界，标题 + 后续内容组成一个"section"
  2) 每个 section 按 chunk_size 拆成若干 sub-chunk
  3) 第 1 片带标题和 header 内容；续片自动补充备注："2016年武将（1/3）"
"""
import re
from typing import List


def _get_heading_text(heading_line: str) -> str:
    """从 Markdown 标题行提取纯文本，如 '### 3.1 2016年武将（开服前）' -> '3.1 2016年武将'"""
    m = re.match(r'^#{1,6}\s+(.+)$', heading_line)
    if not m:
        return heading_line
    text = m.group(1).strip()
    # 去掉末尾括号说明，如（开服前）
    text = re.sub(r'\s*[（(][^）)]*[）)]\s*$', '', text).strip()
    return text


def _make_note(heading_line: str, part: int, total: int) -> str:
    """生成续片备注，如：> 3.1 2016年武将（1/3）"""
    name = _get_heading_text(heading_line)
    return f"> {name}（{part}/{total}）\n\n"


def chunk_markdown(content: str, chunk_size: int = 512, overlap: int = 50) -> List[str]:
    if not content or not content.strip():
        return []

    content = re.sub(r'\n{3,}', '\n\n', content).strip()
    # 统一换行符（Windows \r\n -> \n）
    content = content.replace('\r\n', '\n')
    paragraphs = [p.strip() for p in re.split(r'\n\n+', content) if p.strip()]

    # ---- 1. 按标题分组 ----
    groups: List[dict] = []   # {heading, content, paras:[]}
    cur_group = None

    for p in paragraphs:
        if re.match(r'^#{1,6}\s+', p):
            if cur_group is not None:
                groups.append(cur_group)
            cur_group = {"heading": p, "content": p, "paras": [p]}
        else:
            if cur_group is None:
                cur_group = {"heading": "", "content": "", "paras": []}
            cur_group["paras"].append(p)

    if cur_group is not None:
        groups.append(cur_group)

    # 拼接每个 group 的完整文本，并估算总片数
    for g in groups:
        g["content"] = "\n\n".join(g["paras"])
        # 粗估总片数（每片平均按 chunk_size 的 70% 有效利用）
        g["total_parts"] = max(1, (len(g["content"]) + int(chunk_size * 0.7) - 1) // int(chunk_size * 0.7))

    # ---- 2. 每个 group 按 chunk_size 拆片 ----
    chunks = []

    def flush_buf(buf: str, note: str = "") -> None:
        text = (note + buf).strip()
        if text:
            chunks.append(text)

    for g in groups:
        text = g["content"]
        heading = g["heading"]
        total_parts = g["total_parts"]

        if len(text) <= chunk_size:
            chunks.append(text)
            continue

        # 大 section 需要拆分
        part_no = 0
        buf = ""
        for p in g["paras"]:
            sep = "\n\n" if buf else ""
            cand = buf + sep + p
            if len(cand) <= chunk_size:
                buf = cand
                continue

            # 放不下了：保存当前 buf
            note = _make_note(heading, part_no, total_parts) if part_no > 0 and heading else ""
            flush_buf(buf, note)
            part_no += 1

            # 这个段落本身太长？
            if len(p) > chunk_size:
                items = [l for l in p.split('\n') if l.strip()] if ('|' in p and p.lstrip().startswith('|')) else re.split(r'(?<=[。！？.!?])\s+', p)
                sub_buf = ""
                for item in items:
                    item = item.strip()
                    if not item:
                        continue
                    s = "\n" if '|' in p else " "
                    sub_cand = (sub_buf + s + item).strip() if sub_buf else item
                    if len(sub_cand) <= chunk_size:
                        sub_buf = sub_cand
                    else:
                        if sub_buf:
                            note2 = _make_note(heading, part_no, total_parts) if part_no > 0 and heading else ""
                            flush_buf(sub_buf, note2)
                            part_no += 1
                        note3 = _make_note(heading, part_no, total_parts) if part_no > 0 and heading else ""
                        flush_buf(item[:chunk_size], note3)
                        part_no += 1
                        rest = item[chunk_size:]
                        sub_buf = rest[:overlap] if overlap > 0 else (rest[:60] if rest else "")
                if sub_buf:
                    note4 = _make_note(heading, part_no, total_parts) if part_no > 0 and heading else ""
                    flush_buf(sub_buf, note4)
                    part_no += 1
                    buf = ""
            else:
                buf = p

        # group 剩余
        if buf:
            note = _make_note(heading, part_no, total_parts) if part_no > 0 and heading else ""
            flush_buf(buf, note)

    return [c for c in chunks if c.strip()]

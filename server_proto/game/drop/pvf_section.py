# -*- coding: utf-8 -*-
"""PVF 文本 section 解析小工具（运行时只走 pvf_runtime.read_text）。"""
from __future__ import annotations

import re
from typing import List


def extract_section(text: str, name: str) -> str:
    """取 ``[name]`` 到下一个 ``[`` 之间的正文（不含闭合标签语义）。"""
    tag = "[%s]" % name
    start = text.find(tag)
    if start < 0:
        # 大小写不敏感再试
        low = text.lower()
        tlow = tag.lower()
        start = low.find(tlow)
        if start < 0:
            return ""
        start += len(tag)
        end = low.find("[", start)
        if end < 0:
            end = len(text)
        return text[start:end]
    start += len(tag)
    end = text.find("[", start)
    if end < 0:
        end = len(text)
    return text[start:end]


def extract_tagged_block(text: str, name: str) -> str:
    """取 ``[name] ... [/name]`` 块。"""
    open_tag = "[%s]" % name
    close_tag = "[/%s]" % name
    start = text.lower().find(open_tag.lower())
    if start < 0:
        return ""
    start += len(open_tag)
    end = text.lower().find(close_tag.lower(), start)
    if end < 0:
        return text[start:]
    return text[start:end]


def parse_ints(section: str) -> List[int]:
    if not section:
        return []
    cleaned = re.sub(r"//.*?$", "", section, flags=re.M)
    out: List[int] = []
    for tok in cleaned.replace("\r", " ").replace("\n", " ").replace("\t", " ").split():
        try:
            out.append(int(tok, 10))
        except ValueError:
            continue
    return out


def parse_floats(section: str) -> List[float]:
    if not section:
        return []
    cleaned = re.sub(r"//.*?$", "", section, flags=re.M)
    out: List[float] = []
    for tok in cleaned.replace("\r", " ").replace("\n", " ").replace("\t", " ").split():
        try:
            out.append(float(tok))
        except ValueError:
            continue
    return out


def parse_lst_entries(text: str) -> List[tuple[int, str]]:
    """``id `path``` 列表。"""
    if not text:
        return []
    out: List[tuple[int, str]] = []
    for m in re.finditer(r"(\d+)\s+`([^`]*)`", text):
        try:
            iid = int(m.group(1))
        except ValueError:
            continue
        path = (m.group(2) or "").replace("\\", "/").strip()
        if iid > 0 and path:
            out.append((iid, path))
    return out

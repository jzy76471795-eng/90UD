"""Explicit persistent payload for the fully warmed quest reward index."""
from __future__ import annotations

import json
from typing import Any, Mapping


CACHE_DOMAIN = "quest_rewards"
CACHE_SCHEMA = 1
CACHE_SEMANTIC_VERSION = 2
REWARD_SECTION_TAGS = frozenset(
    {
        "clear reward item",
        "depend give item",
        "difficulty",
        "gold multiple",
        "grade",
        "ignore quest level 4 exp",
        "level",
        "name",
        "reward int data",
        "reward selection int data",
        "reward type",
        "special reward status",
        "target character",
        "type",
    }
)


def _validate_path(quest_id: int, path: str) -> str:
    if quest_id <= 0 or quest_id > 0xFFFF:
        raise ValueError("quest reward cache quest ID is out of range")
    normalized = str(path).replace("\\", "/")
    if (
        not normalized
        or normalized != normalized.lower()
        or not normalized.startswith("n_quest/")
        or not normalized.endswith(".qst")
    ):
        raise ValueError("quest reward cache path is invalid for id=%d" % quest_id)
    return normalized


def _restore_parameter(table_type: Any, value: Any) -> Any:
    if not isinstance(value, dict):
        raise ValueError("quest reward parameter table is invalid")
    raw_weights = value.get("difficulty_weight")
    raw_exp = value.get("exp_table")
    raw_gold = value.get("gold_table")
    if (
        not isinstance(raw_weights, list)
        or not isinstance(raw_exp, list)
        or not isinstance(raw_gold, list)
    ):
        raise ValueError("quest reward parameter rows are invalid")

    weights: dict[str, int] = {}
    for pair in raw_weights:
        if not isinstance(pair, list) or len(pair) != 2:
            raise ValueError("quest reward difficulty row is invalid")
        key = str(pair[0])
        weight = int(pair[1])
        if not key or key in weights or weight < 0:
            raise ValueError("quest reward difficulty identity is invalid")
        weights[key] = weight

    exp_table = [int(item) for item in raw_exp]
    gold_table = [int(item) for item in raw_gold]
    green_penalty = int(value.get("green_penalty", -1))
    grey_penalty = int(value.get("grey_penalty", -1))
    if (
        not weights
        or not exp_table
        or not gold_table
        or any(item < 0 for item in exp_table)
        or any(item < 0 for item in gold_table)
        or green_penalty < 0
        or grey_penalty < 0
    ):
        raise ValueError("quest reward parameter values are invalid")
    return table_type(
        difficulty_weight=weights,
        exp_table=exp_table,
        gold_table=gold_table,
        green_penalty=green_penalty,
        grey_penalty=grey_penalty,
    )


def restore(
    payload: Any,
    table_type: Any,
) -> tuple[dict[int, str], dict[int, tuple[dict[str, list[str]], str]], Any]:
    if not isinstance(payload, dict) or int(payload.get("schema", -1)) != CACHE_SCHEMA:
        raise ValueError("quest reward cache schema mismatch")
    if not bool(payload.get("sections_fully_loaded", False)):
        raise ValueError("quest reward cache is not fully warmed")

    raw_paths = payload.get("quest_paths")
    raw_sections = payload.get("quest_sections")
    if not isinstance(raw_paths, list) or not isinstance(raw_sections, list):
        raise ValueError("quest reward cache collections are invalid")

    paths: dict[int, str] = {}
    for pair in raw_paths:
        if not isinstance(pair, list) or len(pair) != 2:
            raise ValueError("quest reward path row is invalid")
        quest_id = int(pair[0])
        path = _validate_path(quest_id, str(pair[1]))
        if quest_id in paths:
            raise ValueError("duplicate quest reward path id=%d" % quest_id)
        paths[quest_id] = path
    if not paths or len(paths) != int(payload.get("quest_count", -1)):
        raise ValueError("quest reward path count mismatch")

    sections: dict[int, tuple[dict[str, list[str]], str]] = {}
    for row in raw_sections:
        if not isinstance(row, list) or len(row) != 3:
            raise ValueError("quest reward section row is invalid")
        quest_id = int(row[0])
        path = _validate_path(quest_id, str(row[1]))
        if quest_id in sections or paths.get(quest_id) != path:
            raise ValueError("quest reward section identity mismatch for id=%d" % quest_id)
        raw_pairs = row[2]
        if not isinstance(raw_pairs, list):
            raise ValueError("quest reward section collection is invalid")
        restored_sections: dict[str, list[str]] = {}
        for pair in raw_pairs:
            if (
                not isinstance(pair, list)
                or len(pair) != 2
                or not isinstance(pair[1], list)
            ):
                raise ValueError("quest reward section data is invalid")
            tag = str(pair[0])
            values = pair[1]
            if (
                not tag
                or tag != tag.lower()
                or tag not in REWARD_SECTION_TAGS
                or tag in restored_sections
            ):
                raise ValueError("quest reward section tag is invalid")
            if any(not isinstance(value, str) for value in values):
                raise ValueError("quest reward section value is invalid")
            restored_sections[tag] = list(values)
        sections[quest_id] = (restored_sections, path)
    if set(sections) != set(paths) or len(sections) != int(
        payload.get("section_count", -1)
    ):
        raise ValueError("quest reward section/path count mismatch")

    parameter = _restore_parameter(table_type, payload.get("parameter"))
    return paths, sections, parameter


def to_payload(
    paths: Mapping[int, str],
    sections: Mapping[int, tuple[dict[str, list[str]], str]],
    parameter: Any,
    *,
    fully_loaded: bool,
) -> dict[str, Any]:
    if not fully_loaded or set(paths) != set(sections):
        raise ValueError("quest reward index is not fully warmed")

    quest_paths: list[list[Any]] = []
    quest_sections: list[list[Any]] = []
    for quest_id in sorted(paths):
        path = _validate_path(int(quest_id), str(paths[quest_id]))
        section_map, section_path = sections[int(quest_id)]
        if path != str(section_path) or not isinstance(section_map, dict):
            raise ValueError("quest reward section/path mismatch for id=%d" % quest_id)
        pairs: list[list[Any]] = []
        for tag, values in section_map.items():
            if (
                not isinstance(tag, str)
                or not tag
                or tag != tag.lower()
                or tag not in REWARD_SECTION_TAGS
                or not isinstance(values, list)
            ):
                raise ValueError("quest reward section shape is invalid")
            if any(not isinstance(value, str) for value in values):
                raise ValueError("quest reward section row is invalid")
            pairs.append([tag, list(values)])
        quest_paths.append([int(quest_id), path])
        quest_sections.append([int(quest_id), path, pairs])

    payload = {
        "schema": CACHE_SCHEMA,
        "quest_count": len(quest_paths),
        "section_count": len(quest_sections),
        "sections_fully_loaded": True,
        "quest_paths": quest_paths,
        "quest_sections": quest_sections,
        "parameter": {
            "difficulty_weight": [
                [str(key), int(value)]
                for key, value in sorted(parameter.difficulty_weight.items())
            ],
            "exp_table": [int(value) for value in parameter.exp_table],
            "gold_table": [int(value) for value in parameter.gold_table],
            "green_penalty": int(parameter.green_penalty),
            "grey_penalty": int(parameter.grey_penalty),
        },
    }
    json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return payload


__all__ = [
    "CACHE_DOMAIN",
    "CACHE_SCHEMA",
    "CACHE_SEMANTIC_VERSION",
    "REWARD_SECTION_TAGS",
    "restore",
    "to_payload",
]

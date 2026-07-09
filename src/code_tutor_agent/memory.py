"""记忆系统：基于本地 JSON 的知识图谱，用于持久化洞察。"""

import json
import os
from typing import Dict, List

MEMORY_FILE = os.path.join(
    os.path.dirname(__file__), "..", "..", "memory_graph.json"
)


def init_memory() -> None:
    """Create the memory file if it doesn't exist yet."""
    if not os.path.exists(MEMORY_FILE):
        with open(MEMORY_FILE, "w", encoding="utf-8") as f:
            json.dump([], f)


def save_knowledge(
    entity: str,
    entity_type: str,
    observations: List[str],
) -> None:
    """Persist or update a knowledge entity in the local graph.

    Args:
        entity: A short name for the knowledge item (e.g. "两数之和-哈希表解法").
        entity_type: Category such as "Algorithm", "Concept", "CommonMistake".
        observations: List of insights / pitfalls associated with this entity.
    """
    init_memory()

    with open(MEMORY_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)

    found = False
    for item in data:
        if item["entity"] == entity and item["type"] == entity_type:
            for obs in observations:
                if obs not in item["observations"]:
                    item["observations"].append(obs)
            found = True
            break

    if not found:
        data.append({
            "entity": entity,
            "type": entity_type,
            "observations": observations,
        })

    with open(MEMORY_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    print(f"💾 知识已保存: {entity}")


def get_all_knowledge() -> List[Dict]:
    """Return every knowledge entity stored in the graph."""
    init_memory()
    with open(MEMORY_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def search_knowledge(keyword: str) -> List[Dict]:
    """Search entities by keyword in entity name or observations."""
    all_knowledge = get_all_knowledge()
    results = []
    for item in all_knowledge:
        if (
            keyword.lower() in item["entity"].lower()
            or any(keyword.lower() in obs.lower() for obs in item.get("observations", []))
        ):
            results.append(item)
    return results

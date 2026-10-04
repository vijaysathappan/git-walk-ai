"""Bounded BFS traversal for lineage and impact queries."""

from collections import deque


def traverse(adjacency: dict[str, list[str]], start: str, max_depth: int, max_nodes: int) -> dict:
    queue = deque([(start, 0)])
    visited = {start}
    distances: dict[str, int] = {}
    truncated = False
    while queue:
        node, depth = queue.popleft()
        if depth >= max_depth:
            continue
        for neighbor in adjacency.get(node, []):
            if neighbor in visited:
                continue
            if len(distances) >= max_nodes:
                truncated = True
                queue.clear()
                break
            visited.add(neighbor)
            distances[neighbor] = depth + 1
            queue.append((neighbor, depth + 1))
    return {
        "distances": distances,
        "maximum_depth": max(distances.values(), default=0),
        "truncated": truncated,
    }

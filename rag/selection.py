def select_pair(ranked: list[dict]) -> tuple[dict | None, dict | None]:
    ordered = sorted(
        ranked,
        key=lambda item: float(item.get("confidence") or 0),
        reverse=True,
    )
    unique: list[dict] = []
    seen: set[str] = set()
    for item in ordered:
        org_id = str(item.get("org_id") or "")
        if not org_id or org_id in seen:
            continue
        seen.add(org_id)
        unique.append(item)
    primary = unique[0] if unique else None
    backup = unique[1] if len(unique) > 1 else None
    return primary, backup


def confidence_from_distance(distance: float) -> float:
    return 1.0 / (1.0 + float(distance))


def fallback_ranked(candidates: list[dict]) -> list[dict]:
    ranked = []
    for index, candidate in enumerate(candidates):
        distance = candidate.get("distance")
        confidence = confidence_from_distance(float(index if distance is None else distance))
        ranked.append({**candidate, "confidence": confidence})
    return sorted(ranked, key=lambda item: item["confidence"], reverse=True)


def align_probabilities(candidates: list[dict], probabilities: dict) -> list[dict]:
    scored: list[dict] = []
    for index, candidate in enumerate(candidates):
        key = f"poi_{index}"
        if key not in probabilities:
            raise KeyError(key)
        scored.append({**candidate, "confidence": float(probabilities[key])})
    return scored

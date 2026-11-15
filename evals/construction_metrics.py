"""构建评测的计数口径。"""


def precision_recall(correct: int, predicted: int, expected: int) -> dict[str, int | float | None]:
    """按标注项计分，重复预测占用预测数。"""
    if not 0 <= correct <= min(predicted, expected):
        raise ValueError("Correct count exceeds the predicted or expected count")
    return {
        "correct": correct,
        "predicted": predicted,
        "expected": expected,
        "false_positives": predicted - correct,
        "false_negatives": expected - correct,
        "precision": correct / predicted if predicted else None,
        "recall": correct / expected if expected else None,
    }


def coverage(covered: int, expected: int) -> dict[str, int | float | None]:
    """按已标注的证据片段、来源或章节计覆盖率。"""
    if not 0 <= covered <= expected:
        raise ValueError("Covered count exceeds the expected count")
    return {"covered": covered, "expected": expected, "rate": covered / expected if expected else None}

"""从公开发行件还原已冻结的评测样本。"""

from __future__ import annotations

import argparse
import json
import tarfile
from hashlib import sha256
from pathlib import Path

from .evidence import ROOT, read_json

CORPUS = ROOT / "evals" / "corpus" / "public"
MANIFEST = read_json(CORPUS / "manifest.json")


def checked_bytes(path: Path, expected: str) -> bytes:
    payload = path.read_bytes()
    if sha256(payload).hexdigest() != expected:
        raise ValueError(f"SHA-256 校验失败：{path}")
    return payload


def freeze_techqa(data: list, spec: dict) -> dict:
    rows = {row["id"]: row for row in data if row["id"].startswith("DEV_")}
    notes = {note["filename"]: note["text"] for row in rows.values() for note in row["contexts"]}
    documents = {
        name: {"title": notes[name].splitlines()[0].removeprefix("Title: "), "text": notes[name]}
        for name in spec["documents"]
    }
    questions = []
    for item in spec["questions"]:
        row = rows[item["id"]]
        supports = (
            [] if row["is_impossible"] else [{"doc": row["contexts"][0]["filename"], "text": row["answer"]}]
        )
        questions.append(
            {
                "id": row["id"],
                "question": row["question"],
                "type": "answerable" if supports else "unanswerable",
                "supports": supports,
            }
        )
    return {"documents": documents, "questions": questions}


def freeze_hotpot(path: Path, spec: dict) -> dict:
    try:
        from pyarrow.parquet import read_table
    except ImportError as exc:
        raise ValueError("准备 HotpotQA 语料需要 PyArrow，请见 evals/README.md") from exc

    rows = {row["id"]: row for row in read_table(path).to_pylist()}
    documents, questions = {}, []
    entries = list(spec["documents"].items())
    for index, item in enumerate(spec["questions"]):
        row = rows[item["id"]]
        context = dict(zip(row["context"]["title"], row["context"]["sentences"], strict=True))
        names = {}
        for name, title in entries[index * 3 : index * 3 + 3]:
            names[title] = name
            text = " ".join(sentence.strip() for sentence in context[title])
            documents[name] = {"title": title, "text": f"# {title}\n\n{text}\n"}
        gold = zip(row["supporting_facts"]["title"], row["supporting_facts"]["sent_id"], strict=True)
        questions.append(
            {
                "id": row["id"],
                "question": row["question"],
                "type": row["type"],
                "supports": [
                    {"doc": names[title], "text": context[title][offset].strip()}
                    for title, offset in dict.fromkeys(gold)
                ],
            }
        )
    return {"documents": documents, "questions": questions}


def freeze_qasper(paper: dict, spec: dict) -> tuple[str, str, list]:
    text = f"# {paper['title']}\n\n## Abstract\n\n{paper['abstract'] or ''}\n\n"
    paragraphs = {}
    for section in paper["full_text"]:
        if name := section["section_name"]:
            parts = name.split(" ::: ")
            text += f"{'#' * min(6, len(parts) + 1)} {parts[-1].strip()}\n\n"
        for paragraph in filter(None, section["paragraphs"]):
            paragraphs.setdefault(" ".join(paragraph.split()), []).append(
                [len(text), len(text) + len(paragraph)]
            )
            text += paragraph + "\n\n"
    questions = []
    rows = {row["question_id"]: row for row in paper["qas"]}
    for item in spec["questions"]:
        row = rows[item["id"]]
        annotator = item["annotator_index"]
        evidence = row["answers"][annotator]["answer"]["evidence"]
        spans = []
        for paragraph in dict.fromkeys(" ".join(entry.split()) for entry in evidence):
            [span] = paragraphs[paragraph]
            spans.append(span)
        questions.append(
            {"id": item["id"], "question": row["question"], "spans": spans, "annotator_index": annotator}
        )
    return paper["title"], text, questions


def freeze_cuad(document: dict, spec: dict) -> tuple[str, str, list]:
    [paragraph] = document["paragraphs"]
    text = paragraph["context"]
    rows = {row["id"].rsplit("__", 1)[-1]: row for row in paragraph["qas"]}
    questions = []
    for item in spec["questions"]:
        row = rows[item["id"]]
        spans = []
        for answer in row["answers"]:
            start = answer["answer_start"]
            end = start + len(answer["text"])
            if text[start:end] != answer["text"]:
                raise ValueError(f"CUAD 标注与原文不符：{row['id']}")
            spans.append([start, end])
        questions.append({"id": item["id"], "question": row["question"], "spans": spans})
    return document["title"], text, questions


def freeze_long(dataset: str, path: Path, spec: dict) -> dict:
    source_id = spec["source_id"]
    if dataset == "qasper":
        with tarfile.open(path) as archive:
            [member] = [
                entry for entry in archive.getmembers() if entry.name.endswith("qasper-test-v0.3.json")
            ]
            with archive.extractfile(member) as file:
                paper = json.load(file)[source_id]
        title, text, questions = freeze_qasper(paper, spec)
    else:
        [document] = [item for item in read_json(path)["data"] if item["title"] == source_id]
        title, text, questions = freeze_cuad(document, spec)
    fixture = {
        "source_id": source_id,
        "title": title,
        "format": "md" if dataset == "qasper" else "txt",
        "text": text,
        "questions": questions,
        "dataset": dataset,
        "selected_length_band": spec["selected_length_band"],
        "seed": spec["seed"],
        "source_sha256": sha256(text.encode()).hexdigest(),
    }
    if "supplement" in spec:
        fixture["supplement"] = spec["supplement"]
    return fixture


def fixture_bytes(fixture: dict) -> bytes:
    return (json.dumps(fixture, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def bundled_fixture(tag: str) -> bytes:
    if tag in {"techqa", "hotpot"}:
        folder = CORPUS / tag
        fixture = read_json(folder / "questions.json")
        for name, note in fixture["documents"].items():
            note["text"] = (folder / name).read_bytes().decode("utf-8")
    elif tag in {"cuad-1", "cuad-1-cross"}:
        folder = CORPUS / "cuad-1"
        fixture = read_json(folder / ("cross-questions.json" if tag.endswith("-cross") else "questions.json"))
        text = (folder / "strategic-alliance-agreement.txt").read_bytes().decode("utf-8")
        metadata = {key: fixture.pop(key) for key in ("source_id", "title", "format")}
        fixture = {**metadata, "text": text, **fixture}
    else:
        raise ValueError(f"{tag} 的原文需从公开发行件准备，请传入 --sources")
    return fixture_bytes(fixture)


def load_fixture(tag: str, fixtures: Path | None = None) -> dict:
    expected = MANIFEST["cases"][tag]["sha256"]
    if fixtures is not None:
        return json.loads(checked_bytes(fixtures / f"{tag}-fixture.json", expected))
    payload = bundled_fixture(tag)
    if sha256(payload).hexdigest() != expected:
        raise ValueError(f"仓库样本的 SHA-256 与历史样本不同：{tag}")
    return json.loads(payload)


def freeze(tag: str, sources: Path, fixtures: Path) -> Path:
    spec = MANIFEST["cases"][tag]
    dataset = spec["dataset"]
    source = MANIFEST["sources"][dataset]
    path = sources / source["filename"]
    payload = checked_bytes(path, source["sha256"])
    if dataset == "techqa":
        fixture = freeze_techqa(json.loads(payload), spec)
    elif dataset == "hotpot":
        fixture = freeze_hotpot(path, spec)
    else:
        fixture = freeze_long(dataset, path, spec)
    frozen = fixture_bytes(fixture)
    if sha256(frozen).hexdigest() != spec["sha256"]:
        raise ValueError(f"还原结果与历史样本的 SHA-256 不同：{tag}")
    fixtures.mkdir(parents=True, exist_ok=True)
    target = fixtures / f"{tag}-fixture.json"
    target.write_bytes(frozen)
    return target


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("cases", nargs="+", choices=MANIFEST["cases"])
    parser.add_argument("--sources", type=Path, required=True)
    parser.add_argument("--fixtures", type=Path, required=True)
    args = parser.parse_args()
    try:
        for tag in args.cases:
            print(freeze(tag, args.sources, args.fixtures))
    except (OSError, ValueError, KeyError) as exc:
        parser.exit(1, f"{exc}\n")


if __name__ == "__main__":
    main()

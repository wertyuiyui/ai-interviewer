import json
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BANK_PATH = ROOT / "questions" / "real_practice_bank.json"
MANIFEST_PATH = ROOT / "resources" / "practice_source_manifest.json"


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def test_real_practice_bank_has_reviewed_bilingual_questions() -> None:
    bank = load_json(BANK_PATH)
    questions = bank["questions"]

    assert bank["schema_version"] == "1.0"
    assert len(questions) >= 60
    assert len({question["id"] for question in questions}) == len(questions)
    assert len({question["prompt"]["zh"] for question in questions}) == len(questions)
    assert len({question["prompt"]["en"] for question in questions}) == len(questions)

    for question in questions:
        assert re.fullmatch(r"[a-z0-9-]+", question["id"])
        assert question["prompt"]["zh"].strip()
        assert question["prompt"]["en"].strip()
        assert question["authenticity"] == "licensed_bank"
        assert question["status"] == "approved"
        assert question["difficulty"] in {"easy", "medium", "hard"}
        assert 30 <= question["suggested_seconds"] <= 600
        assert question["company_tags"]
        assert question["direction_tags"]
        assert {"voice", "text"}.issubset(question["answer_modes"])
        assert len(question["scoring"]["key_points"]) >= 2
        assert len(question["scoring"]["red_flags"]) >= 2
        assert all(item.strip() for item in question["scoring"]["key_points"])
        assert all(item.strip() for item in question["scoring"]["red_flags"])


def test_every_question_matches_a_pinned_permissive_source() -> None:
    questions = load_json(BANK_PATH)["questions"]
    sources = {
        source["id"]: source
        for source in load_json(MANIFEST_PATH)["sources"]
    }

    assert set(sources) == {
        "javaguide",
        "interview-go",
        "tech-interview-handbook",
        "aris-in-ai-offer",
    }
    assert {source_id: source["revision"] for source_id, source in sources.items()} == {
        "javaguide": "82bb2b64bbd5dc3feed3051a22609786d0d1ae0b",
        "interview-go": "ac017e269c91983889f4dbd392c6318435f4239f",
        "tech-interview-handbook": "e1d28e8886c0b6ff3e50da991ce0e895134ddc59",
        "aris-in-ai-offer": "6f60d728ae290982f7bddd88d9816073dd64d045",
    }
    assert {source["license"] for source in sources.values()} <= {
        "MIT",
        "Apache-2.0",
    }

    for question in questions:
        source = sources[question["source_id"]]
        assert question["revision"] == source["revision"]
        assert question["license"] == source["license"]
        assert question["source_path"].endswith(".md")
        assert not question["source_path"].startswith(("/", "http://", "https://"))
        assert ".." not in Path(question["source_path"]).parts
        assert any(
            question["source_path"].startswith(prefix)
            for prefix in source["approved_path_prefixes"]
        )


def test_bank_covers_required_practice_domains() -> None:
    questions = load_json(BANK_PATH)["questions"]
    topics = {topic.lower() for question in questions for topic in question["topics"]}
    directions = {
        direction.lower()
        for question in questions
        for direction in question["direction_tags"]
    }

    required_topic_markers = {
        "java",
        "mysql",
        "redis",
        "并发",
        "操作系统",
        "计算机网络",
        "go",
        "系统设计",
        "ai 工程",
        "english behavioral",
    }
    assert required_topic_markers <= topics
    assert {"backend", "ai_engineering", "english_interview"} <= directions

    by_source: dict[str, int] = {}
    for question in questions:
        by_source[question["source_id"]] = by_source.get(question["source_id"], 0) + 1
    assert by_source["javaguide"] >= 30
    assert by_source["interview-go"] >= 8
    assert by_source["aris-in-ai-offer"] >= 8
    assert by_source["tech-interview-handbook"] >= 8


def test_bank_does_not_embed_restricted_or_synthetic_sources() -> None:
    serialized = json.dumps(
        {
            "bank": load_json(BANK_PATH),
            "manifest": load_json(MANIFEST_PATH),
        },
        ensure_ascii=False,
    ).lower()

    for forbidden in (
        "synthetic",
        "nowcoder",
        "xiaohongshu",
        "glassdoor",
        "teamblind",
        "mianshiya",
        "0voice",
    ):
        assert forbidden not in serialized


def test_manifest_license_copies_and_notices_are_present() -> None:
    manifest = load_json(MANIFEST_PATH)
    notices = (ROOT / "THIRD_PARTY_NOTICES.md").read_text(encoding="utf-8")

    for source in manifest["sources"]:
        license_path = ROOT / source["license_copy"]
        assert license_path.is_file()
        license_text = license_path.read_text(encoding="utf-8")
        if source["license"] == "MIT":
            assert "MIT License" in license_text
            assert "Permission is hereby granted" in license_text
        else:
            assert "Apache License" in license_text
            assert "Version 2.0" in license_text
        assert source["revision"] in notices

from __future__ import annotations

import json
from dataclasses import replace

import httpx
import pytest

import app.main as main_module
from app.config import get_settings
from app.db import Database
from app.interview_engine import InterviewEngine
from app.profile import ProfileService, ProjectUpload
from app.resume import ResumeParser


class _GitHubSnapshot:
    async def fetch(self, _url: str) -> list[ProjectUpload]:
        return [
            ProjectUpload("README.md", "# 网关\n一个 Go 网关项目。".encode()),
            ProjectUpload("cmd/server.go", b"package main\nfunc main() {}\n"),
        ]


@pytest.mark.asyncio
async def test_profile_routes_project_analysis_and_interview_snapshot(
    tmp_path, monkeypatch
) -> None:
    settings = replace(
        get_settings(),
        db_path=tmp_path / "profile-api.db",
        mock_llm=True,
        voice_mode="L3",
        daily_interview_limit=50,
        client_daily_interview_limit=20,
    )
    database = Database(settings)
    await database.initialize()
    parser = ResumeParser(settings)
    profile_service = ProfileService(
        database,
        settings,
        resume_parser=parser,
        github_fetcher=_GitHubSnapshot(),
    )
    await profile_service.initialize()
    monkeypatch.setattr(main_module, "settings", settings)
    monkeypatch.setattr(main_module, "db", database)
    monkeypatch.setattr(main_module, "resume_parser", parser)
    monkeypatch.setattr(main_module, "profile_service", profile_service)
    monkeypatch.setattr(
        main_module, "interview_engine", InterviewEngine(database, settings)
    )

    client_id = "profile-api-client-000001"
    transport = httpx.ASGITransport(app=main_module.app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://testserver",
        headers={"X-Profile-Key": client_id},
    ) as client:
        empty = await client.get("/api/profile")
        assert empty.status_code == 200
        assert empty.json()["projects"] == []

        resume_response = await client.post(
            "/api/profile/resumes",
            data={"client_id": client_id, "name": "后端实习简历"},
            files={
                "file": (
                    "resume.txt",
                    "某大学计算机本科。订单服务项目使用 Java、Redis 和 MySQL，负责库存链路。",
                    "text/plain",
                )
            },
        )
        assert resume_response.status_code == 201
        resume = resume_response.json()["resume"]
        assert resume["parsed_resume"]["项目"]

        renamed_resume = await client.patch(
            f"/api/profile/resumes/{resume['id']}",
            json={"client_id": client_id, "name": "后端实习简历 · 主投版"},
        )
        assert renamed_resume.status_code == 200
        assert renamed_resume.json()["resume"]["name"] == "后端实习简历 · 主投版"
        assert renamed_resume.json()["resume"]["parsed_resume"] == resume["parsed_resume"]

        reparsed_resume = await client.post(
            f"/api/profile/resumes/{resume['id']}/reparse",
            json={"client_id": client_id},
        )
        assert reparsed_resume.status_code == 200
        assert reparsed_resume.json()["resume"]["name"] == "后端实习简历 · 主投版"
        assert reparsed_resume.json()["resume"]["parsed_resume"]["项目"]

        text_resume_response = await client.post(
            "/api/profile/resumes/text",
            json={
                "client_id": client_id,
                "name": "粘贴版简历",
                "text": "某大学计算机本科生，使用 Go 完成网关项目，负责限流、链路追踪和可观测性，并完成压测与故障复盘。",
            },
        )
        assert text_resume_response.status_code == 201
        assert text_resume_response.json()["resume"]["source_type"] == "text"

        project_response = await client.post(
            "/api/profile/projects",
            data={
                "client_id": client_id,
                "name": "订单服务",
                "responsibility": "负责订单入口、幂等校验和失败补偿",
            },
            files=[
                ("files", ("README.md", "# 订单服务\n处理创建订单请求。", "text/markdown")),
                ("files", ("main.py", "def create_order():\n    return True\n", "text/x-python")),
                ("files", ("db.py", "def save_order():\n    return True\n", "text/x-python")),
            ],
        )
        assert project_response.status_code == 201
        project = project_response.json()["project"]
        assert {item["path"] for item in project["files"]} == {
            "README.md",
            "db.py",
            "main.py",
        }
        assert project["responsibility"] == "负责订单入口、幂等校验和失败补偿"

        associated = await client.put(
            f"/api/profile/resumes/{resume['id']}/projects/0/association",
            json={"client_id": client_id, "project_id": project["id"]},
        )
        assert associated.status_code == 200
        assert associated.json()["association"] == {
            "project_index": 0,
            "project_id": project["id"],
        }

        appended_file = await client.post(
            f"/api/profile/projects/{project['id']}/files",
            data={"client_id": client_id},
            files={"files": ("metrics.py", "def latency():\n    return 12\n", "text/x-python")},
        )
        assert appended_file.status_code == 200
        assert "metrics.py" in {
            item["path"] for item in appended_file.json()["project"]["files"]
        }

        appended_link = await client.post(
            f"/api/profile/projects/{project['id']}/links",
            json={
                "client_id": client_id,
                "urls": ["https://github.com/example/gateway"],
            },
        )
        assert appended_link.status_code == 200
        assert appended_link.json()["project"]["links"] == [
            "https://github.com/example/gateway"
        ]

        selected = await client.patch(
            f"/api/profile/projects/{project['id']}/selection",
            json={"client_id": client_id, "selected": True},
        )
        assert selected.status_code == 200
        assert selected.json()["selected_project_id"] == project["id"]

        async with client.stream(
            "POST",
            f"/api/profile/projects/{project['id']}/analysis/stream",
            json={"client_id": client_id, "refresh": True},
        ) as analyzed:
            assert analyzed.status_code == 200
            assert analyzed.headers["content-type"].startswith("application/x-ndjson")
            analysis_events = [
                json.loads(line) async for line in analyzed.aiter_lines() if line
            ]
        assert analysis_events[0] == {
            "type": "progress",
            "stage": "reading",
            "progress": 10,
            "message": "正在读取项目文件",
        }
        assert analysis_events[-1]["type"] == "complete"
        assert analysis_events[-1]["progress"] == 100
        analysis = analysis_events[-1]["result"]["analysis"]
        assert analysis["interview_questions"]
        assert analysis["interview_intro"]
        assert analysis["request_flow_review"]["status"] in {
            "verified",
            "partial",
            "needs_verification",
        }
        assert all(item["evidence"] for item in analysis["interview_questions"])
        assert [
            event["progress"] for event in analysis_events if event["type"] == "progress"
        ] == sorted(
            event["progress"]
            for event in analysis_events
            if event["type"] == "progress"
        )

        updated = await client.patch(
            f"/api/profile/projects/{project['id']}",
            json={
                "client_id": client_id,
                "name": "订单服务 2.0",
                "responsibility": "负责库存预占与订单失败补偿",
            },
        )
        assert updated.status_code == 200
        assert updated.json()["project"]["name"] == "订单服务 2.0"
        assert updated.json()["project"]["responsibility"] == "负责库存预占与订单失败补偿"

        analyzed_after_update = await client.post(
            f"/api/profile/projects/{project['id']}/analysis",
            json={"client_id": client_id, "refresh": False},
        )
        assert analyzed_after_update.status_code == 200
        analysis = analyzed_after_update.json()["analysis"]
        assert "库存预占与订单失败补偿" in analysis["interview_intro"]

        more_questions = await client.post(
            f"/api/profile/projects/{project['id']}/questions",
            json={
                "client_id": client_id,
                "mode": "more",
                "existing_questions": [
                    item["question"] for item in analysis["interview_questions"]
                ],
                "count": 3,
            },
        )
        assert more_questions.status_code == 200
        more_payload = more_questions.json()
        assert more_payload["mode"] == "more"
        assert more_payload["generated_count"] == 3
        assert all(item["evidence"] for item in more_payload["questions"])

        regenerated = await client.post(
            f"/api/profile/projects/{project['id']}/questions",
            json={
                "client_id": client_id,
                "mode": "regenerate",
                "existing_questions": [
                    item["question"] for item in more_payload["questions"]
                ],
                "count": 3,
            },
        )
        assert regenerated.status_code == 200
        assert regenerated.json()["mode"] == "regenerate"
        assert regenerated.json()["generated_count"] == 3

        unauthorized = await client.post(
            f"/api/profile/projects/{project['id']}/analysis",
                json={"client_id": "different-client-0000001", "refresh": False},
                headers={"X-Profile-Key": "different-client-0000001"},
        )
        assert unauthorized.status_code == 404

        unauthorized_resume_rename = await client.patch(
            f"/api/profile/resumes/{resume['id']}",
            json={"client_id": "different-client-0000001", "name": "越权改名"},
            headers={"X-Profile-Key": "different-client-0000001"},
        )
        assert unauthorized_resume_rename.status_code == 404

        missing_stream = await client.post(
            "/api/profile/projects/missing-project/analysis/stream",
            json={"client_id": client_id, "refresh": False},
        )
        missing_events = [
            json.loads(line) for line in missing_stream.text.splitlines() if line
        ]
        assert missing_stream.status_code == 200
        assert missing_events[-1] == {
            "type": "error",
            "error": {
                "code": "PROFILE_PROJECT_NOT_FOUND",
                "message": "项目不存在",
                "details": {},
            },
        }

        project_without_capability = await client.post(
            "/api/interviews",
            headers={"X-Profile-Key": ""},
            json={
                "client_id": client_id,
                "resume": resume["parsed_resume"],
                "profile_project_id": project["id"],
                "company": "bytedance",
                "role": "backend",
                "interview_type": "technical",
                "specialization": "Java 后端",
                "language_mode": "zh",
                "duration_minutes": 10,
            },
        )
        assert project_without_capability.status_code == 403

        created = await client.post(
            "/api/interviews",
            json={
                "client_id": client_id,
                "resume": resume["parsed_resume"],
                "profile_project_id": project["id"],
                "company": "bytedance",
                "role": "backend",
                "interview_type": "technical",
                "specialization": "Java 后端",
                "language_mode": "zh",
                "duration_minutes": 10,
            },
        )
        assert created.status_code == 201
        assert created.json()["profile_project_id"] == project["id"]
        stored = await database.get_interview(created.json()["id"])
        uploaded = stored["resume"]["项目"][0]
        assert uploaded["name"].startswith("[匿名 Profile 项目]")
        assert uploaded["role"] == "负责库存预占与订单失败补偿"
        assert any("架构模块" in item for item in uploaded["highlights"])
        assert any("请求链路" in item for item in uploaded["highlights"])
        assert all("建议回答" not in item for item in uploaded["highlights"])
        assert all("system prompt" not in item for item in uploaded["highlights"])
        assert all("服务端必须" not in item for item in uploaded["highlights"])

        github = await client.post(
            "/api/profile/projects/github",
            json={
                "client_id": client_id,
                "name": "Go 网关",
                "url": "https://github.com/example/gateway",
                "responsibility": "负责限流与链路追踪",
            },
        )
        assert github.status_code == 201
        assert {item["path"] for item in github.json()["project"]["files"]} == {
            "README.md",
            "cmd/server.go",
        }
        assert github.json()["project"]["responsibility"] == "负责限流与链路追踪"

        linked = await client.post(
            "/api/profile/projects/links",
            json={
                "client_id": client_id,
                "name": "多仓库技术项目",
                "project_type": "technical",
                "urls": [
                    "https://github.com/example/gateway",
                    "https://github.com/example/worker",
                ],
            },
        )
        assert linked.status_code == 201
        linked_project = linked.json()["project"]
        assert linked_project["project_type"] == "technical"
        assert linked_project["responsibility_scope"] == "all"
        assert linked_project["responsibility"] == ""
        assert linked_project["links"] == [
            "https://github.com/example/gateway",
            "https://github.com/example/worker",
        ]
        assert all(item["path"].startswith("sources/") for item in linked_project["files"])

        deleted = await client.delete(
            f"/api/profile/projects/{project['id']}",
        )
        assert deleted.status_code == 200
        assert deleted.json() == {"deleted": True}

    assert any(getattr(route, "path", None) == "/project" for route in main_module.app.routes)

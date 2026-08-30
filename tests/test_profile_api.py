from __future__ import annotations

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
            data={"client_id": client_id, "name": "订单服务"},
            files=[
                ("files", ("README.md", "# 订单服务\n处理创建订单请求。", "text/markdown")),
                ("files", ("main.py", "def create_order():\n    return True\n", "text/x-python")),
            ],
        )
        assert project_response.status_code == 201
        project = project_response.json()["project"]
        assert {item["path"] for item in project["files"]} == {
            "README.md",
            "main.py",
        }

        selected = await client.patch(
            f"/api/profile/projects/{project['id']}/selection",
            json={"client_id": client_id, "selected": True},
        )
        assert selected.status_code == 200
        assert selected.json()["selected_project_id"] == project["id"]

        analyzed = await client.post(
            f"/api/profile/projects/{project['id']}/analysis",
            json={"client_id": client_id, "refresh": False},
        )
        assert analyzed.status_code == 200
        assert analyzed.json()["analysis"]["interview_questions"]

        unauthorized = await client.post(
            f"/api/profile/projects/{project['id']}/analysis",
                json={"client_id": "different-client-0000001", "refresh": False},
                headers={"X-Profile-Key": "different-client-0000001"},
        )
        assert unauthorized.status_code == 404

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
        uploaded = stored["resume"]["项目"][-1]
        assert uploaded["name"].startswith("[匿名 Profile 项目]")
        assert any("项目材料建议追问" in item for item in uploaded["highlights"])
        assert all("建议回答" not in item for item in uploaded["highlights"])

        github = await client.post(
            "/api/profile/projects/github",
            json={
                "client_id": client_id,
                "name": "Go 网关",
                "url": "https://github.com/example/gateway",
            },
        )
        assert github.status_code == 201
        assert {item["path"] for item in github.json()["project"]["files"]} == {
            "README.md",
            "cmd/server.go",
        }

        deleted = await client.delete(
            f"/api/profile/projects/{project['id']}",
        )
        assert deleted.status_code == 200
        assert deleted.json() == {"deleted": True}

    assert any(getattr(route, "path", None) == "/project" for route in main_module.app.routes)

from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PUBLIC = ROOT / "public"


def test_home_exposes_three_parallel_practice_modes() -> None:
    page = (PUBLIC / "index.html").read_text(encoding="utf-8")

    assert 'class="feature-launch-grid"' in page
    assert 'href="#setupTitle"' in page and "完整模拟" in page
    assert 'class="feature-launch-card is-quick" href="/practice"' in page
    assert 'class="feature-launch-card is-project" href="/project"' in page
    assert "快速刷题" in page and "项目解读" in page


def test_anonymous_profile_supports_multiple_resume_and_project_assets() -> None:
    page = (PUBLIC / "index.html").read_text(encoding="utf-8")
    script = (PUBLIC / "js" / "home.js").read_text(encoding="utf-8")
    common = (PUBLIC / "js" / "common.js").read_text(encoding="utf-8")

    for element_id in (
        "profilePanel",
        "profileStatus",
        "profileResumeFiles",
        "profileResumeList",
        "profileProjectFiles",
        "profileProjectResponsibility",
        "profileProjectProgress",
        "profileGithubUrl",
        "profileGithubAdd",
        "profileProjectList",
        "savedPanel",
        "savedResumeChoice",
    ):
        assert f'id="{element_id}"' in page

    assert 'id="profileResumeFiles" type="file"' in page and "multiple" in page
    assert 'id="profileProjectFiles" type="file"' in page and "multiple" in page
    assert "匿名个人 Profile" in page and "无需登录" in page
    assert "apiFetch('/api/profile'" in script
    assert "apiFetch('/api/profile/resumes'" in script
    assert "apiFetch('/api/profile/resumes/text'" in script
    assert "apiFetch('/api/resumes/parse'" not in script
    assert "apiFetch('/api/profile/projects'" in script
    assert "apiFetch('/api/profile/projects/github'" in script
    assert "data.append('responsibility_scope'" in script
    assert "profileProjectPartialScope.checked" in script
    assert "apiFetch('/api/profile/projects/links'" in script
    assert 'id="profileProjectType"' in page and "arXiv" in page
    assert "readProfileProjectFileList(files)" in script
    assert "updateProfileProjectProgress('error'" in script
    assert "/selection`" in script
    assert "method: 'DELETE'" in script
    assert "getStructuredResume(selected)" in script
    assert "resumeMode === 'saved'" in script
    assert "profile_project_id: profile.selected_project_id || null" in script
    assert "不使用项目" in script
    assert "selected: false" in script
    assert "SQLite" in page and "阿里云百炼" in page
    assert "String(url).startsWith('/api/')" in common


def test_standalone_profile_integrates_assets_mistakes_and_history() -> None:
    page = (PUBLIC / "profile.html").read_text(encoding="utf-8")
    script = (PUBLIC / "js" / "profile.js").read_text(encoding="utf-8")
    style = (PUBLIC / "assets" / "profile.css").read_text(encoding="utf-8")

    for element_id in (
        "profileAvatar",
        "profileResumeFiles",
        "profileResumeText",
        "profileResumeList",
        "profileProjectType",
        "profileProjectFiles",
        "profileProjectLinks",
        "profileProjectList",
        "profileMistakeList",
        "profileInterviewHistory",
        "profilePracticeHistory",
        "projectEditDialog",
        "editProjectName",
        "editProjectFiles",
        "editProjectLinks",
        "saveProjectEdit",
        "appendProjectFiles",
        "appendProjectLinks",
    ):
        assert f'id="{element_id}"' in page

    assert "简历中的论文/项目行会与同名档案链接自动对应" in page
    assert "function resumeSurname" in script
    assert "parsed_resume?.['姓名']" in script
    assert "|| '?'" in script
    assert "icon.textContent = '历'" not in (PUBLIC / "js" / "home.js").read_text(encoding="utf-8")
    assert "function linkedProjectFor" in script
    assert "project_associations" in script
    assert "method: 'PUT'" in script
    assert "/association`" in script
    assert "/files`" in script and "/links`" in script
    assert "编辑并添加资料" in script and "编辑关联资料" in script
    assert "matches.length === 1" in script
    assert "normalizeMatchName(project?.name) === key" in script
    assert "noopener noreferrer" in script
    assert "apiFetch('/api/profile'" in script
    assert "apiFetch('/api/profile/resumes'" in script
    assert "apiFetch('/api/profile/resumes/text'" in script
    assert "apiFetch('/api/profile/projects'" in script
    assert "'/api/profile/projects/github'" in script
    assert "'/api/profile/projects/links'" in script
    assert "/api/practice/mistakes?client_id=" in script
    assert "/api/history?client_id=" in script
    assert "/api/practice/history?client_id=" in script
    assert "Promise.allSettled" in script
    assert "@media (max-width: 720px)" in style
    assert ".profile-edit-dialog" in style
    assert "font-family: var(--font-serif)" in style


def test_global_typography_uses_local_font_tokens_and_centered_buttons() -> None:
    style = (PUBLIC / "assets" / "app.css").read_text(encoding="utf-8")
    profile_style = (PUBLIC / "assets" / "profile.css").read_text(encoding="utf-8")
    project_style = (PUBLIC / "assets" / "project.css").read_text(encoding="utf-8")
    coding_style = (PUBLIC / "assets" / "coding.css").read_text(encoding="utf-8")

    assert "--font-sans:" in style and "--font-serif:" in style
    assert ".button-label { font-size: inherit; line-height: inherit; }" in style
    assert ".secondary-button, .danger-button" in style
    assert "display: inline-flex" in style
    assert "Georgia" not in profile_style
    assert not any(
        legacy in css
        for css in (style, profile_style, project_style, coding_style)
        for legacy in ("font-family: ui-serif", "font-family: ui-monospace")
    )


def test_three_interview_types_are_available_in_full_and_quick_practice() -> None:
    home = (PUBLIC / "index.html").read_text(encoding="utf-8")
    home_script = (PUBLIC / "js" / "home.js").read_text(encoding="utf-8")
    practice = (PUBLIC / "practice.html").read_text(encoding="utf-8")
    practice_script = (PUBLIC / "js" / "practice.js").read_text(encoding="utf-8")

    for value in ("technical", "hr", "technical_hr"):
        assert f'name="interview_type" value="{value}"' in home
        assert f'name="practice_interview_type" value="{value}"' in practice

    assert "技术面" in home and "综合面（HR面）" in home and "技术+综合面" in home
    assert "['technical', 'hr', 'technical_hr']" in home_script
    assert "interview_type: interviewType" in practice_script


def test_project_interpretation_page_uses_profile_analysis_contract() -> None:
    page = (PUBLIC / "project.html").read_text(encoding="utf-8")
    script = (PUBLIC / "js" / "project.js").read_text(encoding="utf-8")
    style = (PUBLIC / "assets" / "project.css").read_text(encoding="utf-8")

    for element_id in (
        "projectFiles",
        "projectResponsibility",
        "projectResponsibilityPanel",
        "projectResponsibilityEdit",
        "projectResponsibilitySave",
        "projectGithubUrl",
        "projectAssetList",
        "projectAnalyzeButton",
        "projectArchitecture",
        "projectRequestFlow",
        "projectInterviewIntro",
        "projectFlowReviewState",
        "projectFlowReviewSummary",
        "projectFlowIssues",
        "projectFlowAssumptions",
        "projectFlowToVerify",
        "projectTechnologyChoices",
        "projectRisks",
        "projectImprovements",
        "projectQuestionList",
        "projectQuestionStatus",
        "projectMoreQuestions",
        "projectRegenerateQuestions",
        "projectProgressSteps",
    ):
        assert f'id="{element_id}"' in page

    assert 'id="projectFiles" type="file"' in page and "multiple" in page
    assert "apiFetch('/api/profile'" in script
    assert "apiFetch('/api/profile/projects'" in script
    assert "apiFetch('/api/profile/projects/github'" in script
    assert "/selection`" in script
    assert "/analysis`" in script
    assert "refresh: Boolean(refresh)" in script
    assert "data.append('responsibility_scope'" in script
    assert "elements.partialScope.checked" in script
    assert "apiFetch('/api/profile/projects/links'" in script
    assert 'id="projectType"' in page and "arXiv" in page
    assert "默认视为负责整个项目" in page
    assert "method: 'PATCH'" in script and "responsibility" in script
    assert "/analysis/stream`" in script
    assert "response.body.getReader()" in script
    assert "event?.type === 'progress'" in script
    assert "ANALYSIS_PROGRESS_STAGES" in script
    assert "%" not in page
    assert "analysis.interview_intro" in script
    assert "analysis.request_flow_review" in script
    assert "flowLabels" in script
    assert "架构与核心业务流程" in script
    assert "方法与实验链" in script
    assert "它不一定是 HTTP 请求" in page
    assert "input[data-responsibility-text]:checked" in script
    assert "/questions`" in script
    assert "generateProjectQuestions('more')" in script
    assert "generateProjectQuestions('regenerate')" in script
    assert "questionsBusy" in script
    assert "questionFingerprint" in script
    assert "evidence.length === 0" in script
    assert "project-question-evidence" in script
    assert "isSkillRuleText" in script
    assert "source.suggested_answer" in script
    assert "展开参考思路" in script
    assert "不使用项目" in script
    assert "|| profile.projects[0]" not in script
    assert "selected: false" in script
    assert "SQLite" in page and "阿里云百炼" in page
    assert "project_practice.v1" in script
    assert "完成回答" in script
    assert "formatSeconds(elapsedPracticeSeconds" in script
    assert "@media (max-width: 720px)" in style
    assert ".project-progress-steps" in style
    assert ".project-flow-review" in style
    assert ".project-question-toolbar" in style


def test_quick_practice_does_not_display_question_bank_source_copy() -> None:
    page = (PUBLIC / "practice.html").read_text(encoding="utf-8")
    script = (PUBLIC / "js" / "practice.js").read_text(encoding="utf-8")

    assert "真实公开题库" not in page
    assert "正在读取题库" not in page
    assert "真实题库已就绪" not in script
    assert "题库连接待恢复" not in script


def test_quick_practice_honors_text_only_mode_and_valid_filter_states() -> None:
    page = (PUBLIC / "practice.html").read_text(encoding="utf-8")
    script = (PUBLIC / "js" / "practice.js").read_text(encoding="utf-8")

    assert 'id="practiceVoiceProof"' in page
    assert "event.transcription_available === false" in script
    assert "normalizeMode(config?.voice_mode) !== 'L3'" in script
    assert "setupVoice.disabled = !voiceTranscriptionAvailable" in script
    assert "elements.answer.value = ''" in script
    assert "function syncPracticeFilters()" in script
    assert "elements.topic.disabled = true" in script
    assert "behavioralTopic" in script

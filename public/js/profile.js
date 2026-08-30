import {
  $, apiFetch, companyLabel, formatDate, getClientId, normalizeHistoryPayload,
  score10, setButtonBusy, showToast,
} from './common.js?v=20260830-profile-bank-v2';

const RESUME_SELECTION_KEY = 'mock_interview.profile_resume.v1';
const state = { profile: { resumes: [], projects: [], selected_project_id: '' }, mistakes: [], interviews: [], practice: [] };
let editingProjectId = '';
let editingResumeId = '';

function element(tag, className = '', text = '') {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== '') node.textContent = String(text);
  return node;
}

function itemId(item) {
  return String(item?.id || item?.resume_id || item?.project_id || '');
}

function itemName(item, fallback = '未命名资料') {
  return String(item?.name || item?.file_name || item?.title || fallback).trim();
}

function readResumeSelection() {
  try { return localStorage.getItem(RESUME_SELECTION_KEY) || ''; } catch { return ''; }
}

function saveResumeSelection(id) {
  try {
    if (id) localStorage.setItem(RESUME_SELECTION_KEY, id);
    else localStorage.removeItem(RESUME_SELECTION_KEY);
  } catch { /* 页面内仍可正常选择。 */ }
}

function resumeSurname(resume) {
  const name = String(resume?.parsed_resume?.['姓名'] || '').trim();
  return name.match(/[\u3400-\u9fff]/u)?.[0] || name.match(/[A-Za-z]/)?.[0]?.toLocaleUpperCase() || '?';
}

function normalizeMatchName(value) {
  return String(value || '').normalize('NFKC').toLocaleLowerCase('zh-CN')
    .replace(/[^\p{L}\p{N}]/gu, '');
}

function linkedProjectFor(resume, resumeProject, projectIndex) {
  const association = (resume?.project_associations || []).find((item) => Number(item?.project_index) === projectIndex);
  if (association?.project_id) {
    return state.profile.projects.find((project) => itemId(project) === String(association.project_id)) || null;
  }
  const key = normalizeMatchName(resumeProject?.name);
  if (!key) return null;
  const matches = state.profile.projects.filter((project) => normalizeMatchName(project?.name) === key);
  return matches.length === 1 ? matches[0] : null;
}

function safeLinks(project) {
  const values = Array.isArray(project?.links) ? project.links : [project?.github_url];
  return [...new Set(values.map((value) => String(value || '').trim()).filter((value) => {
    try {
      const url = new URL(value);
      return url.protocol === 'https:' && ['github.com', 'arxiv.org', 'www.arxiv.org'].includes(url.hostname.toLowerCase());
    } catch { return false; }
  }))];
}

function renderCounts() {
  $('#resumeCount').textContent = `${state.profile.resumes.length} 份简历`;
  $('#projectCount').textContent = `${state.profile.projects.length} 个论文/项目`;
  $('#mistakeCount').textContent = `${state.mistakes.length} 道错题`;
  const selectedId = readResumeSelection();
  const selected = state.profile.resumes.find((resume) => itemId(resume) === selectedId) || state.profile.resumes[0];
  $('#profileAvatar').textContent = resumeSurname(selected);
  $('#profileIdentity').textContent = selected
    ? `${itemName(selected, '当前简历')} · 当前设备匿名档案`
    : '当前设备匿名档案 · 上传简历后生成经历视图';
}

function detailList(title, values, projector) {
  const items = Array.isArray(values) ? values : [];
  if (!items.length) return null;
  const section = element('section', 'resume-detail-group');
  section.append(element('h4', '', title));
  const list = element('div', 'resume-detail-list');
  items.forEach((value, index) => list.append(projector(value, index)));
  section.append(list);
  return section;
}

function renderTextLines(target, values) {
  const lines = (Array.isArray(values) ? values : []).map(String).filter(Boolean);
  if (!lines.length) return;
  const list = element('ul');
  lines.forEach((line) => list.append(element('li', '', line)));
  target.append(list);
}

function renderResumeDetails(resume) {
  const parsed = resume?.parsed_resume || {};
  const wrap = element('div', 'resume-details');
  const education = detailList('教育经历', parsed['教育'], (entry) => {
    const row = element('article', 'resume-detail-row');
    row.append(element('strong', '', [entry?.school, entry?.degree, entry?.major].filter(Boolean).join(' · ') || '教育经历'));
    if (entry?.period) row.append(element('small', '', entry.period));
    renderTextLines(row, entry?.details);
    return row;
  });
  const internships = detailList('实习经历', parsed['实习经历'], (entry) => {
    const row = element('article', 'resume-detail-row');
    row.append(element('strong', '', [entry?.company, entry?.role].filter(Boolean).join(' · ') || '实习经历'));
    if (entry?.period) row.append(element('small', '', entry.period));
    renderTextLines(row, [...(entry?.highlights || []), ...(entry?.metrics || [])]);
    return row;
  });
  const projects = detailList('论文 / 项目经历', parsed['项目'], (entry, projectIndex) => {
    const row = element('article', 'resume-detail-row resume-project-row');
    const heading = element('div', 'resume-project-heading');
    heading.append(element('strong', '', entry?.name || '未命名项目'));
    const linked = linkedProjectFor(resume, entry, projectIndex);
    if (linked) {
      const badge = element('span', 'resume-link-match', '已关联档案资料');
      badge.title = `对应：${itemName(linked)}`;
      heading.append(badge);
    } else {
      heading.append(element('span', 'resume-link-missing', '待关联档案资料'));
    }
    row.append(heading);
    if (entry?.role) row.append(element('small', '', entry.role));
    const technologies = Array.isArray(entry?.technologies) ? entry.technologies : [];
    if (technologies.length) {
      const tags = element('div', 'resume-tech-tags');
      technologies.forEach((value) => tags.append(element('span', '', value)));
      row.append(tags);
    }
    renderTextLines(row, [...(entry?.highlights || []), ...(entry?.metrics || [])]);
    const extractedLinks = safeLinks(entry);
    if (linked || extractedLinks.length) {
      const linkRow = element('div', 'resume-project-links');
      safeLinks(linked || entry).forEach((href, index) => {
        const anchor = element('a', '', href.includes('arxiv.org') ? '查看 arXiv' : `查看项目链接${index ? ` ${index + 1}` : ''}`);
        anchor.href = href;
        anchor.target = '_blank';
        anchor.rel = 'noopener noreferrer';
        linkRow.append(anchor);
      });
      const analysis = element('a', '', '查看解读');
      analysis.href = `/project?project=${encodeURIComponent(itemId(linked))}`;
      linkRow.append(analysis);
      row.append(linkRow);
    }
    const edit = element('button', 'resume-project-edit', linked ? '编辑关联资料' : '编辑并添加资料');
    edit.type = 'button';
    edit.addEventListener('click', () => editResumeProject(resume, projectIndex, linked));
    row.append(edit);
    return row;
  });
  [education, internships, projects].filter(Boolean).forEach((section) => wrap.append(section));
  const skills = Array.isArray(parsed['技能']) ? parsed['技能'] : [];
  if (skills.length) {
    const skillSection = element('section', 'resume-detail-group');
    skillSection.append(element('h4', '', '技能'));
    const tags = element('div', 'resume-tech-tags');
    skills.forEach((skill) => tags.append(element('span', '', skill)));
    skillSection.append(tags);
    wrap.append(skillSection);
  }
  if (!wrap.childElementCount) wrap.append(element('p', 'profile-empty-copy', '这份简历尚无可展示的结构化内容。'));
  return wrap;
}

function renderResumes() {
  const list = $('#profileResumeList');
  list.replaceChildren();
  if (!state.profile.resumes.length) {
    list.append(element('div', 'profile-empty-state', '还没有简历。上传 PDF 或粘贴文字后，这里会展示结构化经历。'));
    return;
  }
  const selectedId = readResumeSelection();
  state.profile.resumes.forEach((resume, index) => {
    const id = itemId(resume);
    const chosen = id === selectedId || (!selectedId && index === 0);
    const card = element('details', `profile-resume-card${chosen ? ' is-selected' : ''}`);
    if (chosen) card.open = true;
    const summary = element('summary');
    const avatar = element('span', 'resume-avatar', resumeSurname(resume));
    const copy = element('span', 'resume-card-copy');
    copy.append(element('strong', '', itemName(resume, '未命名简历')), element('small', '', `${formatDate(resume.created_at, false)} · ${resume.source_type === 'text' ? '文字简历' : 'PDF 简历'}`));
    const internships = Array.isArray(resume?.parsed_resume?.['实习经历']) ? resume.parsed_resume['实习经历'] : [];
    if (internships.length) {
      const first = internships[0] || {};
      const internshipLabel = [first.company, first.role].filter(Boolean).join(' · ');
      copy.append(element('span', 'resume-internship-strip', `【实习经历】${internships.length} 段${internshipLabel ? ` · ${internshipLabel}` : ''}`));
    }
    const actions = element('span', 'resume-card-actions');
    const choose = element('button', chosen ? 'is-selected' : '', chosen ? '模拟面试已选' : '设为开面简历');
    choose.type = 'button';
    choose.addEventListener('click', (event) => {
      event.preventDefault();
      saveResumeSelection(id);
      renderResumes();
      renderCounts();
      showToast('已设为下一场模拟面试使用的简历。', 'success');
    });
    const rename = element('button', '', '重命名');
    rename.type = 'button';
    rename.addEventListener('click', (event) => {
      event.preventDefault();
      openResumeRename(resume);
    });
    const reparse = element('button', '', '重新识别');
    reparse.type = 'button';
    reparse.addEventListener('click', async (event) => {
      event.preventDefault();
      setButtonBusy(reparse, true, '识别中…');
      try {
        await apiFetch(`/api/profile/resumes/${encodeURIComponent(id)}/reparse`, {
          method: 'POST', timeout: 65_000, json: { client_id: getClientId() },
        });
        await loadProfile();
        showToast('简历已按最新规则重新识别；旧项目关联已清除，请核对后重新关联。', 'success', 5200);
      } catch (error) {
        showToast(error?.message || '简历重新识别失败。', 'error', 5200);
        setButtonBusy(reparse, false);
      }
    });
    const remove = element('button', 'is-danger', '删除');
    remove.type = 'button';
    remove.addEventListener('click', (event) => {
      event.preventDefault();
      deleteProfileItem('resumes', resume);
    });
    actions.append(choose, rename, reparse, remove);
    summary.append(avatar, copy, actions);
    card.append(summary, renderResumeDetails(resume));
    list.append(card);
  });
}

function openResumeRename(resume) {
  editingResumeId = itemId(resume);
  $('#editResumeName').value = itemName(resume, '未命名简历');
  $('#resumeRenameStatus').textContent = '';
  const dialog = $('#resumeRenameDialog');
  if (!dialog.open) dialog.showModal();
  window.requestAnimationFrame(() => $('#editResumeName').select());
}

async function saveResumeRename() {
  const name = $('#editResumeName').value.trim();
  if (!editingResumeId || !name) return showToast('请填写简历名称。', 'error');
  const button = $('#saveResumeRename');
  setButtonBusy(button, true, '正在保存…');
  try {
    await apiFetch(`/api/profile/resumes/${encodeURIComponent(editingResumeId)}`, {
      method: 'PATCH', timeout: 15_000, json: { client_id: getClientId(), name },
    });
    await loadProfile();
    $('#resumeRenameStatus').textContent = '名称已保存。';
    $('#resumeRenameDialog').close();
    showToast('简历已重命名。', 'success');
  } catch (error) {
    $('#resumeRenameStatus').textContent = '保存失败';
    showToast(error?.message || '简历重命名失败。', 'error');
  } finally { setButtonBusy(button, false); }
}

function projectTypeLabel(project) {
  return { paper: '论文', technical: '技术类项目', application: '应用类项目' }[project?.project_type] || '项目';
}

function renderProjects() {
  const list = $('#profileProjectList');
  list.replaceChildren();
  if (!state.profile.projects.length) {
    list.append(element('div', 'profile-empty-state', '还没有论文或项目资料。'));
    return;
  }
  state.profile.projects.forEach((project) => {
    const id = itemId(project);
    const selected = id === String(state.profile.selected_project_id || '') || project?.selected === true;
    const card = element('article', `profile-project-card${selected ? ' is-selected' : ''}`);
    const top = element('div', 'profile-project-card-top');
    const icon = element('span', 'project-type-mark', project?.project_type === 'paper' ? '论' : '项');
    const copy = element('div');
    copy.append(element('span', 'project-kind', projectTypeLabel(project)), element('h3', '', itemName(project)));
    top.append(icon, copy);
    card.append(top);
    const responsibility = project?.responsibility_scope === 'partial'
      ? String(project?.responsibility || '仅负责部分内容')
      : '默认责任范围：整个项目';
    card.append(element('p', 'project-responsibility-copy', responsibility));
    const links = safeLinks(project);
    const meta = element('div', 'profile-project-meta');
    meta.append(element('span', '', `${Array.isArray(project?.files) ? project.files.length : 0} 个已保存文件`), element('span', '', `${links.length} 个公开链接`));
    card.append(meta);
    if (links.length) {
      const linkList = element('div', 'profile-project-link-list');
      links.forEach((href) => {
        const link = element('a', '', href.replace(/^https:\/\//, ''));
        link.href = href;
        link.target = '_blank';
        link.rel = 'noopener noreferrer';
        linkList.append(link);
      });
      card.append(linkList);
    }
    const actions = element('div', 'profile-project-actions');
    const select = element('button', selected ? 'is-selected' : '', selected ? '模拟面试已选' : '设为重点项目');
    select.type = 'button';
    select.addEventListener('click', () => selectProject(project, !selected));
    const analyze = element('a', '', project?.project_type === 'paper' ? '阅读论文' : '解读项目');
    analyze.href = `/project?project=${encodeURIComponent(id)}`;
    const remove = element('button', 'is-danger', '删除');
    remove.type = 'button';
    remove.addEventListener('click', () => deleteProfileItem('projects', project));
    const edit = element('button', '', '编辑');
    edit.type = 'button';
    edit.addEventListener('click', () => openProjectEditor(project));
    actions.append(select, edit, analyze, remove);
    card.append(actions);
    list.append(card);
  });
}

async function editResumeProject(resume, projectIndex, linkedProject) {
  const resumeId = itemId(resume);
  if (!resumeId) return;
  try {
    const response = await apiFetch(`/api/profile/resumes/${encodeURIComponent(resumeId)}/projects/${projectIndex}/association`, {
      method: 'PUT',
      timeout: 15_000,
      json: { client_id: getClientId(), ...(linkedProject ? { project_id: itemId(linkedProject) } : {}) },
    });
    await loadProfile();
    const projectId = String(response?.association?.project_id || itemId(response?.project));
    const project = state.profile.projects.find((item) => itemId(item) === projectId) || response?.project;
    if (project) {
      openProjectEditor(project);
      const extracted = resume?.parsed_resume?.['项目']?.[projectIndex];
      const suggestedLinks = safeLinks(extracted).filter((url) => !safeLinks(project).includes(url));
      if (suggestedLinks.length) $('#editProjectLinks').value = suggestedLinks.join('\n');
    }
  } catch (error) {
    showToast(error?.message || '无法建立简历项目关联，请稍后重试。', 'error');
  }
}

function renderEditorAssets(project) {
  const files = Array.isArray(project?.files) ? project.files : [];
  const links = safeLinks(project);
  $('#editProjectFileCount').textContent = `${files.length} 个文件`;
  $('#editProjectLinkCount').textContent = `${links.length} 个链接`;
  const current = $('#editProjectCurrentAssets');
  current.replaceChildren();
  if (files.length) {
    const group = element('div');
    group.append(element('strong', '', '已关联文件'));
    const list = element('p', '', files.slice(0, 8).map((item) => item.path).join(' · '));
    if (files.length > 8) list.textContent += ` · 另 ${files.length - 8} 个`;
    group.append(list); current.append(group);
  }
  if (links.length) {
    const group = element('div'); group.append(element('strong', '', '已关联链接'));
    const list = element('div', 'profile-project-link-list');
    links.forEach((href) => {
      const link = element('a', '', href.replace(/^https:\/\//, ''));
      link.href = href; link.target = '_blank'; link.rel = 'noopener noreferrer'; list.append(link);
    });
    group.append(list); current.append(group);
  }
  if (!current.childElementCount) current.append(element('p', 'profile-empty-copy', '尚未关联文件或公开链接，可以在上方继续添加。'));
}

function openProjectEditor(project) {
  editingProjectId = itemId(project);
  $('#editProjectName').value = itemName(project);
  $('#editProjectType').value = project?.project_type || 'application';
  $('#editProjectPartialScope').checked = project?.responsibility_scope === 'partial';
  $('#editProjectResponsibility').value = String(project?.responsibility || '');
  $('#editProjectResponsibility').classList.toggle('is-hidden', !$('#editProjectPartialScope').checked);
  $('#editProjectFiles').value = '';
  $('#editProjectLinks').value = '';
  $('#projectEditStatus').textContent = '';
  renderEditorAssets(project);
  const dialog = $('#projectEditDialog');
  if (!dialog.open) dialog.showModal();
}

function currentEditingProject() {
  return state.profile.projects.find((project) => itemId(project) === editingProjectId) || null;
}

async function saveProjectEdit() {
  const project = currentEditingProject();
  if (!project) return;
  const partial = $('#editProjectPartialScope').checked;
  const responsibility = $('#editProjectResponsibility').value.trim();
  if (partial && !responsibility) return showToast('部分负责时请填写具体负责内容。', 'error');
  const button = $('#saveProjectEdit');
  setButtonBusy(button, true, '正在保存…');
  try {
    const response = await apiFetch(`/api/profile/projects/${encodeURIComponent(editingProjectId)}`, {
      method: 'PATCH', timeout: 15_000, json: {
        client_id: getClientId(),
        name: $('#editProjectName').value.trim(),
        project_type: $('#editProjectType').value,
        responsibility_scope: partial ? 'partial' : 'all',
        responsibility: partial ? responsibility : '',
      },
    });
    await loadProfile();
    const updated = response?.project || currentEditingProject();
    if (updated) { editingProjectId = itemId(updated); renderEditorAssets(updated); }
    $('#projectEditStatus').textContent = '基础信息已保存；已有分析将在下次查看时按新资料更新。';
    showToast('论文/项目基础信息已保存。', 'success');
  } catch (error) { showToast(error?.message || '项目保存失败。', 'error'); }
  finally { setButtonBusy(button, false); }
}

async function appendEditingProjectFiles() {
  const files = [...$('#editProjectFiles').files];
  if (!editingProjectId || !files.length) return showToast('请先选择要追加的文件。', 'error');
  if (currentEditingProject()?.project_type !== $('#editProjectType').value) return showToast('类型有改动，请先保存基础信息再追加文件。', 'error');
  const button = $('#appendProjectFiles'); setButtonBusy(button, true, '正在追加…');
  try {
    const data = new FormData(); data.append('client_id', getClientId());
    files.forEach((file) => data.append('files', file, file.name));
    const response = await apiFetch(`/api/profile/projects/${encodeURIComponent(editingProjectId)}/files`, { method: 'POST', body: data, timeout: 65_000 });
    await loadProfile();
    const updated = response?.project || currentEditingProject();
    if (updated) renderEditorAssets(updated);
    $('#editProjectFiles').value = '';
    $('#projectEditStatus').textContent = `已追加 ${files.length} 个所选文件。`;
    showToast('关联文件已追加。', 'success');
  } catch (error) { showToast(error?.message || '文件追加失败。', 'error', 5200); }
  finally { setButtonBusy(button, false); }
}

async function appendEditingProjectLinks() {
  const project = currentEditingProject();
  const urls = $('#editProjectLinks').value.split(/\n+/).map((value) => value.trim()).filter(Boolean);
  if (!project || !urls.length) return showToast('请先填写要追加的公开链接。', 'error');
  if (project.project_type !== $('#editProjectType').value) return showToast('类型有改动，请先保存基础信息再追加链接。', 'error');
  if (!validateProjectLinks([...safeLinks(project), ...urls], project.project_type)) return showToast('链接格式或类型不正确；论文需且只能包含一个 arXiv 主链接。', 'error');
  const button = $('#appendProjectLinks'); setButtonBusy(button, true, '正在读取…');
  try {
    const response = await apiFetch(`/api/profile/projects/${encodeURIComponent(editingProjectId)}/links`, { method: 'POST', timeout: 65_000, json: { client_id: getClientId(), urls } });
    await loadProfile();
    const updated = response?.project || currentEditingProject();
    if (updated) renderEditorAssets(updated);
    $('#editProjectLinks').value = '';
    $('#projectEditStatus').textContent = `已追加 ${urls.length} 个公开链接及其可读取快照。`;
    showToast('关联链接已追加。', 'success');
  } catch (error) { showToast(error?.message || '链接追加失败。', 'error', 5200); }
  finally { setButtonBusy(button, false); }
}

async function loadProfile() {
  $('#resumeStatus').textContent = '正在同步档案…';
  try {
    const payload = await apiFetch('/api/profile', { timeout: 15_000 });
    const source = payload?.profile && typeof payload.profile === 'object' ? payload.profile : payload || {};
    state.profile = {
      resumes: Array.isArray(source.resumes) ? source.resumes : [],
      projects: Array.isArray(source.projects) ? source.projects : [],
      selected_project_id: String(source.selected_project_id || ''),
    };
    if (!readResumeSelection() && state.profile.resumes[0]) saveResumeSelection(itemId(state.profile.resumes[0]));
    renderResumes();
    renderProjects();
    renderCounts();
    $('#resumeStatus').textContent = `${state.profile.resumes.length} 份简历已同步`;
    $('#projectStatus').textContent = `${state.profile.projects.length} 个论文/项目已同步`;
  } catch (error) {
    $('#resumeStatus').textContent = '档案暂时无法同步';
    $('#projectStatus').textContent = '论文/项目暂时无法同步';
    showToast(error?.message || '个人档案读取失败，请稍后重试。', 'error');
  }
}

async function uploadResumes(files) {
  const selected = [...files];
  const valid = selected.filter((file) => (file.type === 'application/pdf' || file.name.toLowerCase().endsWith('.pdf')) && file.size <= 8 * 1024 * 1024);
  if (valid.length !== selected.length) showToast('已跳过非 PDF 或超过 8 MB 的简历。', 'error', 5200);
  if (!valid.length) {
    $('#profileResumeFiles').value = '';
    return;
  }
  $('#profileResumeFiles').disabled = true;
  $('#resumeStatus').textContent = `正在解析并保存 ${valid.length} 份简历…`;
  let latestId = '';
  let saved = 0;
  const failed = [];
  try {
    for (const file of valid) {
      const data = new FormData();
      data.append('client_id', getClientId());
      data.append('name', file.name.replace(/\.pdf$/i, ''));
      data.append('file', file, file.name);
      try {
        const response = await apiFetch('/api/profile/resumes', { method: 'POST', body: data, timeout: 65_000 });
        latestId = itemId(response?.resume || response);
        saved += 1;
      } catch (error) {
        failed.push(file.name);
        showToast(`${file.name}：${error?.message || '识别失败'}`, 'error', 5200);
      }
    }
    if (latestId) saveResumeSelection(latestId);
    await loadProfile();
    if (saved) showToast(`已识别并保存 ${saved} 份简历${failed.length ? `，${failed.length} 份失败` : ''}。`, 'success', 5200);
    if (!saved) $('#resumeStatus').textContent = `${failed.length} 份简历识别失败，请按文件名查看提示`;
  } finally {
    $('#profileResumeFiles').disabled = false;
    $('#profileResumeFiles').value = '';
  }
}

async function saveTextResume(event) {
  event.preventDefault();
  const text = $('#profileResumeText').value.trim();
  if (text.length < 30) return showToast('简历文字至少需要 30 个字。', 'error');
  const button = $('#saveResumeText');
  setButtonBusy(button, true, '正在解析…');
  try {
    const response = await apiFetch('/api/profile/resumes/text', { method: 'POST', timeout: 65_000, json: { client_id: getClientId(), name: $('#profileResumeName').value.trim() || '文字简历', text } });
    saveResumeSelection(itemId(response?.resume || response));
    $('#profileResumeText').value = '';
    $('#profileResumeName').value = '';
    await loadProfile();
    showToast('文字简历已解析并保存。', 'success');
  } catch (error) {
    showToast(error?.message || '文字简历保存失败。', 'error', 5200);
  } finally { setButtonBusy(button, false); }
}

function projectPayload() {
  return {
    client_id: getClientId(),
    name: $('#profileProjectName').value.trim(),
    project_type: $('#profileProjectType').value,
    responsibility_scope: $('#profileProjectPartialScope').checked ? 'partial' : 'all',
    responsibility: $('#profileProjectPartialScope').checked ? $('#profileProjectResponsibility').value.trim() : '',
  };
}

function resetProjectForm() {
  $('#profileProjectName').value = '';
  $('#profileProjectLinks').value = '';
  $('#profileProjectFiles').value = '';
  $('#profileProjectPartialScope').checked = false;
  $('#profileProjectResponsibility').value = '';
  $('#profileProjectResponsibility').classList.add('is-hidden');
}

async function uploadProjectFiles() {
  const files = [...$('#profileProjectFiles').files];
  if (!files.length) return showToast('请先选择项目源码、文本或论文 PDF。', 'error');
  const payload = projectPayload();
  const button = $('#saveProjectFiles');
  setButtonBusy(button, true, '正在上传…');
  $('#projectStatus').textContent = `正在读取并保存 ${files.length} 个文件…`;
  try {
    const data = new FormData();
    Object.entries({ ...payload, name: payload.name || files[0].name.replace(/\.[^.]+$/, '') }).forEach(([key, value]) => data.append(key, value));
    files.forEach((file) => data.append('files', file, file.name));
    const response = await apiFetch('/api/profile/projects', { method: 'POST', body: data, timeout: 65_000 });
    await loadProfile();
    await selectProject(response?.project || response, true, true);
    resetProjectForm();
    showToast('论文/项目文件已保存。', 'success');
  } catch (error) {
    $('#projectStatus').textContent = error?.message || '上传失败';
    showToast(error?.message || '论文/项目上传失败。', 'error', 5200);
  } finally { setButtonBusy(button, false); }
}

function validateProjectLinks(values, type) {
  if (!values.length || values.length > 5) return false;
  let arxiv = 0;
  for (const value of values) {
    try {
      const url = new URL(value);
      const host = url.hostname.toLowerCase();
      const parts = url.pathname.split('/').filter(Boolean);
      const github = host === 'github.com' && parts.length === 2;
      const paper = ['arxiv.org', 'www.arxiv.org'].includes(host) && ['abs', 'pdf'].includes(parts[0]) && parts.length === 2;
      if (url.protocol !== 'https:' || url.search || url.hash || (!github && !(type === 'paper' && paper))) return false;
      if (paper) arxiv += 1;
    } catch { return false; }
  }
  return type !== 'paper' || arxiv === 1;
}

async function saveProjectLinks() {
  const values = $('#profileProjectLinks').value.split(/\n+/).map((value) => value.trim()).filter(Boolean);
  const payload = projectPayload();
  if (!validateProjectLinks(values, payload.project_type)) return showToast('请填写 1–5 个有效链接；论文需且只能包含一个 arXiv 链接。', 'error');
  const button = $('#saveProjectLinks');
  setButtonBusy(button, true, '正在读取…');
  try {
    const repositoryName = new URL(values[0]).pathname.split('/').filter(Boolean).pop().replace(/\.git$|\.pdf$/gi, '');
    const singleGithub = values.length === 1 && new URL(values[0]).hostname.toLowerCase() === 'github.com';
    const request = { ...payload, name: payload.name || repositoryName, urls: values };
    if (singleGithub) request.url = values[0];
    const response = await apiFetch(singleGithub ? '/api/profile/projects/github' : '/api/profile/projects/links', { method: 'POST', timeout: 35_000, json: request });
    await loadProfile();
    await selectProject(response?.project || response, true, true);
    resetProjectForm();
    showToast('公开链接已读取并保存。', 'success');
  } catch (error) {
    $('#projectStatus').textContent = error?.message || '链接读取失败';
    showToast(error?.message || '公开链接保存失败。', 'error', 5200);
  } finally { setButtonBusy(button, false); }
}

async function selectProject(project, selected, quiet = false) {
  const id = itemId(project);
  if (!id) return;
  try {
    const response = await apiFetch(`/api/profile/projects/${encodeURIComponent(id)}/selection`, { method: 'PATCH', timeout: 15_000, json: { client_id: getClientId(), selected } });
    state.profile.selected_project_id = String(response?.selected_project_id || (selected ? id : ''));
    state.profile.projects = state.profile.projects.map((item) => ({ ...item, selected: itemId(item) === state.profile.selected_project_id }));
    renderProjects();
    if (!quiet) showToast(selected ? '已设为下一场模拟面试重点项目。' : '已取消重点项目。', 'success');
  } catch (error) { showToast(error?.message || '项目选择失败。', 'error'); }
}

async function deleteProfileItem(kind, item) {
  const label = kind === 'resumes' ? '简历' : '论文/项目';
  if (!window.confirm(`确定删除“${itemName(item, label)}”吗？此操作不可恢复。`)) return;
  try {
    await apiFetch(`/api/profile/${kind}/${encodeURIComponent(itemId(item))}`, { method: 'DELETE', timeout: 15_000 });
    if (kind === 'resumes' && readResumeSelection() === itemId(item)) saveResumeSelection('');
    await loadProfile();
    showToast(`${label}已删除。`, 'success');
  } catch (error) { showToast(error?.message || `${label}删除失败。`, 'error'); }
}

function renderMistakes() {
  const list = $('#profileMistakeList');
  list.replaceChildren();
  if (!state.mistakes.length) {
    list.append(element('div', 'profile-empty-state', '还没有错题。快速刷题中已评分且不高于 6 分的题会自动收录。'));
    return;
  }
  state.mistakes.forEach((mistake) => {
    const row = element('article', 'profile-mistake-item');
    const score = element('span', 'mistake-score', Number.isFinite(Number(mistake.latest_score)) ? Number(mistake.latest_score).toFixed(1) : '—');
    const copy = element('div');
    const question = mistake?.question || {};
    copy.append(element('span', 'mistake-topic', [question.company && companyLabel(question.company), question.topic || question.category].filter(Boolean).join(' · ') || '快速刷题'), element('h3', '', question.question || '题目内容暂不可用'));
    const deductions = Array.isArray(mistake.latest_deductions) ? mistake.latest_deductions.filter(Boolean) : [];
    copy.append(element('p', '', deductions[0] || `已作答 ${mistake.attempt_count || 1} 次，建议重答并补齐机制、链路与验证方式。`));
    const actions = element('div', 'profile-mistake-actions');
    const retry = element('a', '', '去重答'); retry.href = '/practice';
    const remove = element('button', '', '移出错题本'); remove.type = 'button';
    remove.addEventListener('click', () => deleteMistake(mistake));
    actions.append(retry, remove);
    row.append(score, copy, actions);
    list.append(row);
  });
}

async function deleteMistake(mistake) {
  if (!window.confirm('确定将这道题移出错题本吗？')) return;
  try {
    await apiFetch(`/api/practice/mistakes/${encodeURIComponent(mistake.id)}?client_id=${encodeURIComponent(getClientId())}`, { method: 'DELETE', timeout: 12_000 });
    state.mistakes = state.mistakes.filter((item) => item.id !== mistake.id);
    renderMistakes(); renderCounts();
    showToast('已移出错题本。', 'success');
  } catch (error) { showToast(error?.message || '错题删除失败。', 'error'); }
}

function renderInterviewHistory() {
  const list = $('#profileInterviewHistory'); list.replaceChildren();
  if (!state.interviews.length) return list.append(element('div', 'profile-empty-state', '还没有已完成的模拟面试。'));
  state.interviews.slice(0, 8).forEach((report) => {
    const id = String(report.id || report.session_id || report.interview_id || '');
    const row = element('a', 'profile-history-item'); row.href = `/report?session=${encodeURIComponent(id)}`;
    const copy = element('span');
    copy.append(element('strong', '', `${companyLabel(report.company)} · ${report.specialization || '通用后端'}`), element('small', '', `${formatDate(report.ended_at || report.generated_at)} · ${report.interview_type === 'hr' ? '综合面' : report.interview_type === 'technical_hr' ? '技术+综合面' : '技术面'}`));
    const rawScore = report.overall_score ?? report.score;
    const numeric = rawScore === null || rawScore === undefined ? null : score10(rawScore, null);
    row.append(copy, element('b', numeric === null ? 'is-empty' : '', numeric === null ? '—' : numeric.toFixed(1)));
    list.append(row);
  });
}

function renderPracticeHistory() {
  const list = $('#profilePracticeHistory'); list.replaceChildren();
  if (!state.practice.length) return list.append(element('div', 'profile-empty-state', '还没有快速刷题记录。'));
  state.practice.slice(0, 8).forEach((session) => {
    const row = element('a', 'profile-history-item'); row.href = '/practice';
    const copy = element('span');
    const status = session.status === 'completed' || session.status === 'finished' ? '已完成' : '练习中';
    copy.append(element('strong', '', `${companyLabel(session.company)} · ${session.topic || '综合练习'}`), element('small', '', `${formatDate(session.ended_at || session.created_at)} · ${session.attempt_count || 0} 次作答 · ${status}`));
    const numeric = session.best_score === null || session.best_score === undefined ? null : Number(session.best_score);
    row.append(copy, element('b', numeric === null ? 'is-empty' : '', numeric === null ? '—' : numeric.toFixed(1)));
    list.append(row);
  });
}

async function loadActivity() {
  const client = encodeURIComponent(getClientId());
  const [mistakes, interviews, practice] = await Promise.allSettled([
    apiFetch(`/api/practice/mistakes?client_id=${client}&limit=100`, { timeout: 15_000 }),
    apiFetch(`/api/history?client_id=${client}`, { timeout: 15_000 }),
    apiFetch(`/api/practice/history?client_id=${client}&limit=20`, { timeout: 15_000 }),
  ]);
  state.mistakes = mistakes.status === 'fulfilled' && Array.isArray(mistakes.value?.items) ? mistakes.value.items : [];
  state.interviews = interviews.status === 'fulfilled' ? normalizeHistoryPayload(interviews.value) : [];
  state.practice = practice.status === 'fulfilled' ? normalizeHistoryPayload(practice.value) : [];
  renderMistakes(); renderInterviewHistory(); renderPracticeHistory(); renderCounts();
}

$('#profileResumeFiles').addEventListener('change', (event) => uploadResumes(event.target.files));
$('#resumeTextForm').addEventListener('submit', saveTextResume);
$('#toggleResumeText').addEventListener('click', () => {
  const form = $('#resumeTextForm');
  const shown = form.classList.toggle('is-hidden') === false;
  $('#toggleResumeText').setAttribute('aria-expanded', String(shown));
  if (shown) $('#profileResumeName').focus();
});
$('#profileProjectPartialScope').addEventListener('change', () => $('#profileProjectResponsibility').classList.toggle('is-hidden', !$('#profileProjectPartialScope').checked));
$('#profileProjectType').addEventListener('change', () => {
  $('#profileProjectFiles').accept = $('#profileProjectType').value === 'paper'
    ? '.pdf,.zip,.md,.txt,.json,.yaml,.yml,.toml,.ini,.conf,.py,.java,.go,.js,.ts,.tsx,.jsx,.sql,.xml,.proto,.c,.cc,.cpp,.h,.hpp,.rs,.rb,.php,.kt,.swift,.sh'
    : '.zip,.md,.txt,.json,.yaml,.yml,.toml,.ini,.conf,.py,.java,.go,.js,.ts,.tsx,.jsx,.sql,.xml,.proto,.c,.cc,.cpp,.h,.hpp,.rs,.rb,.php,.kt,.swift,.sh';
});
$('#saveProjectFiles').addEventListener('click', uploadProjectFiles);
$('#saveProjectLinks').addEventListener('click', saveProjectLinks);
$('#saveResumeRename').addEventListener('click', saveResumeRename);
$('#editProjectPartialScope').addEventListener('change', () => $('#editProjectResponsibility').classList.toggle('is-hidden', !$('#editProjectPartialScope').checked));
$('#saveProjectEdit').addEventListener('click', saveProjectEdit);
$('#appendProjectFiles').addEventListener('click', appendEditingProjectFiles);
$('#appendProjectLinks').addEventListener('click', appendEditingProjectLinks);

await Promise.all([loadProfile(), loadActivity()]);

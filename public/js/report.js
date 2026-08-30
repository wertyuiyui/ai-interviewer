import {
  $, $$, apiFetch, cacheReport, clamp, clearCachedReports, companyLabel,
  firstValue, formatDate, getCachedReports, getClientId, getCurrentSession,
  normalizeHistoryPayload, removeCachedReport, score10, setButtonBusy,
  setCurrentSession, showToast, toArray,
} from './common.js';

const query = new URLSearchParams(location.search);
const sessionId = query.get('session') || '';
let activeView = query.get('view') === 'history' || !sessionId ? 'history' : 'current';
let currentReport = null;
let sessionMeta = null;
let historyReports = [];
let reportPollStopped = false;
let reportLoadingActive = Boolean(sessionId);

const rubricDefinitions = [
  { key: 'project', label: '项目深度', weight: 40, aliases: ['project_depth', 'project', '项目深度', '项目'] },
  { key: 'fundamentals', label: '基础八股', weight: 30, aliases: ['fundamentals', 'basic_knowledge', 'basics', '基础八股', '基础知识'] },
  { key: 'coding', label: '手撕思路', weight: 20, aliases: ['coding_thought', 'coding', 'algorithm', 'problem_solving', '手撕思路', '算法思路'] },
  { key: 'communication', label: '表达逻辑', weight: 10, aliases: ['communication', 'expression', 'clarity', '表达逻辑', '表达'] },
];

function parsePossibleJson(value) {
  if (typeof value !== 'string') return value;
  const cleaned = value.trim().replace(/^```(?:json)?\s*/i, '').replace(/\s*```$/, '');
  if (!cleaned.startsWith('{') && !cleaned.startsWith('[')) return value;
  try { return JSON.parse(cleaned); } catch { return value; }
}

function unwrapReport(value) {
  let source = parsePossibleJson(value);
  if (!source || typeof source !== 'object') return null;
  if (source.report !== undefined) source = parsePossibleJson(source.report);
  else if (source.data?.report !== undefined) source = parsePossibleJson(source.data.report);
  else if (source.data && typeof source.data === 'object' && !Array.isArray(source.data)) source = source.data;
  return source && typeof source === 'object' && !Array.isArray(source) ? source : null;
}

function reportId(report) {
  return String(firstValue(report, ['id', 'session_id', 'interview_id'], '') || '');
}

function reportDate(report) {
  return firstValue(report, ['ended_at', 'completed_at', 'generated_at', 'created_at', 'date', '_cached_at'], '');
}

function findDimension(root, definition) {
  if (!root) return undefined;
  if (Array.isArray(root)) {
    return root.find((item) => definition.aliases.includes(String(firstValue(item, ['key', 'name', 'label', 'dimension'], '')).toLowerCase()));
  }
  for (const alias of definition.aliases) {
    if (root[alias] !== undefined) return root[alias];
  }
  return undefined;
}

function normalizeTextList(value) {
  return toArray(value).map((item) => {
    if (typeof item === 'string' || typeof item === 'number') return String(item);
    return String(firstValue(item, ['text', 'point', 'reason', 'description', 'name', '扣分点'], '') || '');
  }).filter(Boolean);
}

function normalizeQuestion(item, index) {
  const deductions = normalizeTextList(firstValue(item, ['deductions', 'deduction_points', 'issues', 'weaknesses', '扣分点'], []));
  const rawScore = firstValue(item, ['score', 'points', '得分'], null);
  return {
    index: index + 1,
    question: String(firstValue(item, ['question', 'prompt', 'q', '题目'], `第 ${index + 1} 题`) || `第 ${index + 1} 题`),
    answer: String(firstValue(item, ['candidate_answer', 'answer', 'response', 'user_answer', '候选人回答'], '未记录到完整回答') || '未记录到完整回答'),
    improved: String(firstValue(item, ['improved_answer', 'rewrite', 'example_answer', 'better_answer', '改写示范'], '暂无改写示范') || '暂无改写示范'),
    category: String(firstValue(item, ['category', 'dimension', 'type', '考察点'], '综合追问') || '综合追问'),
    score: rawScore === null ? null : score10(rawScore),
    deductions,
  };
}

function normalizeLinks(item) {
  let links = firstValue(item, ['links', 'resources', 'urls', '参考链接'], []);
  if (!links?.length && (item?.url || item?.resource_url)) {
    links = [{ label: item.resource_title || item.label || item.title || '参考资料', url: item.resource_url || item.url }];
  }
  return toArray(links).map((link) => {
    if (typeof link === 'string') return { label: link.includes('codetop') ? 'CodeTop' : '参考资料', url: link };
    return {
      label: String(firstValue(link, ['label', 'name', 'title'], '参考资料')),
      url: String(firstValue(link, ['url', 'href', 'link'], '')),
    };
  }).filter((link) => link.url);
}

function normalizePractice(item, index) {
  if (typeof item === 'string') return { topic: item, reason: '这是当前报告识别出的薄弱项，建议在下一场前集中复习。', links: [] };
  return {
    topic: String(firstValue(item, ['topic', 'name', 'title', 'knowledge_point', '知识点'], `必练项 ${index + 1}`)),
    reason: String(firstValue(item, ['reason', 'description', 'why', 'suggestion', '练习建议'], '建议结合高频题复习，并尝试用自己的项目案例解释。')),
    links: normalizeLinks(item),
  };
}

function normalizeTopicScores(value) {
  if (!value || typeof value !== 'object') return {};
  if (Array.isArray(value)) {
    return Object.fromEntries(value.map((item) => [
      String(firstValue(item, ['topic', 'name', 'label'], '')),
      score10(firstValue(item, ['score', 'value'], 0)),
    ]).filter(([topic]) => topic));
  }
  return Object.fromEntries(Object.entries(value).map(([topic, score]) => [topic, score10(score)]));
}

function normalizeHintEvents(value) {
  return toArray(value).map((event, index) => ({
    ordinal: Math.max(1, Number(firstValue(event, ['ordinal', 'question_ordinal'], index + 1)) || index + 1),
    question: String(firstValue(event, ['question', 'prompt'], `第 ${index + 1} 题`) || `第 ${index + 1} 题`),
    hint: String(firstValue(event, ['hint', 'text'], '') || ''),
  }));
}

function normalizeReport(raw, metadata = {}) {
  const report = unwrapReport(raw) || {};
  const rubricRoot = firstValue(report, ['rubric', 'dimension_scores', 'dimensions', '评分细则'], {});
  const scoresRoot = firstValue(report, ['scores', 'score_breakdown', '评分'], {});
  const rubric = rubricDefinitions.map((definition) => {
    const entry = findDimension(rubricRoot, definition) ?? findDimension(scoresRoot, definition);
    const score = score10(entry, 0);
    const feedback = typeof entry === 'object' && entry !== null
      ? String(firstValue(entry, ['feedback', 'summary', 'comment', 'description', '评价'], '') || '')
      : '';
    const deductions = typeof entry === 'object' && entry !== null
      ? normalizeTextList(firstValue(entry, ['deductions', 'issues', '扣分点'], []))
      : [];
    return { ...definition, score, feedback: feedback || deductions.slice(0, 2).join('；') || '详见下方逐题扣分依据。' };
  });

  const questionsRaw = firstValue(report, ['question_reviews', 'question_feedback', 'questions', 'per_question', '逐题反馈', '逐题扣分'], []);
  const practiceRaw = firstValue(report, ['must_practice', 'practice_list', 'next_practice', 'practice_plan', '下次必练清单'], []);
  const overallRaw = firstValue(report, ['overall_score', 'total_score', 'score', '综合得分'], null);
  const weighted = rubric.reduce((sum, item) => sum + item.score * item.weight / 100, 0);
  const id = reportId(report) || reportId(metadata) || sessionId;
  const durationRaw = Object.hasOwn(report, 'duration_minutes')
    ? report.duration_minutes
    : (Object.hasOwn(report, 'duration')
      ? report.duration
      : (Object.hasOwn(metadata, 'duration_minutes') ? metadata.duration_minutes : 0));
  const stressLevelRaw = firstValue(report, ['stress_level'], firstValue(metadata, ['stress_level'], null));
  const legacyStress = Boolean(firstValue(report, ['stress'], firstValue(metadata, ['stress'], false)));
  const hintEvents = normalizeHintEvents(firstValue(report, ['hint_events'], firstValue(metadata, ['hint_events'], [])));
  const memoryEnabled = Boolean(firstValue(report, ['memory_enabled'], firstValue(metadata, ['memory_enabled'], true)));
  const stressLevel = stressLevelRaw === null
    ? (legacyStress ? 2 : 0)
    : clamp(Math.round(Number(stressLevelRaw) || 0), 0, 3);
  return {
    raw: report,
    id,
    company: firstValue(report, ['company'], firstValue(metadata, ['company'], '')),
    role: firstValue(report, ['role'], firstValue(metadata, ['role'], 'backend')),
    specialization: String(firstValue(report, ['specialization'], firstValue(metadata, ['specialization'], '通用后端')) || '通用后端'),
    languageMode: firstValue(report, ['language_mode'], firstValue(metadata, ['language_mode'], 'bilingual')) === 'zh' ? 'zh' : 'bilingual',
    stress: stressLevel > 0,
    stressLevel,
    unlimited: durationRaw === null,
    duration: durationRaw === null ? 0 : Number(durationRaw) || 0,
    endedAt: reportDate(report) || reportDate(metadata) || new Date().toISOString(),
    endReason: String(firstValue(report, ['end_reason', 'reason'], firstValue(metadata, ['end_reason'], '')) || ''),
    summary: String(firstValue(report, ['summary', 'overall_feedback', 'conclusion', '总结', '总体评价'], '本场报告已生成，请结合逐题扣分点安排下一次练习。')),
    overall: overallRaw === null ? Math.round(weighted * 10) / 10 : score10(overallRaw),
    rubric,
    questions: toArray(questionsRaw).map(normalizeQuestion),
    practice: toArray(practiceRaw).map(normalizePractice),
    topicScores: normalizeTopicScores(firstValue(report, ['topic_scores', 'knowledge_scores', '知识点得分'], {})),
    hintEvents,
    memoryEnabled,
  };
}

function isReportPending(payload) {
  const status = String(firstValue(payload, ['status', 'state'], '') || '').toLowerCase();
  if (['pending', 'generating', 'processing', 'reporting', 'running', 'queued'].includes(status)) return true;
  const candidate = unwrapReport(payload);
  if (!candidate) return true;
  const reportKeys = ['overall_score', 'total_score', 'score', 'rubric', 'question_reviews', 'question_feedback', '逐题反馈', '综合得分'];
  return !reportKeys.some((key) => candidate[key] !== undefined);
}

function createElement(tag, className = '', text = '') {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (text !== '') element.textContent = String(text);
  return element;
}

function renderRubric(report) {
  const grid = $('#rubricGrid');
  grid.replaceChildren();
  report.rubric.forEach((dimension) => {
    const card = createElement('article', 'rubric-card');
    const top = createElement('div', 'rubric-top');
    top.append(createElement('h3', '', dimension.label), createElement('span', 'rubric-weight', `权重 ${dimension.weight}%`));
    const score = createElement('div', 'rubric-score');
    score.append(createElement('strong', '', dimension.score.toFixed(1)), createElement('small', '', '/ 10'));
    const track = createElement('progress', 'score-track');
    track.max = 10;
    track.value = clamp(dimension.score, 0, 10);
    track.setAttribute('aria-label', `${dimension.label} ${dimension.score.toFixed(1)} 分`);
    card.append(top, score, track, createElement('p', '', dimension.feedback));
    grid.append(card);
  });
}

function renderQuestions(report) {
  const list = $('#questionList');
  list.replaceChildren();
  $('#questionCount').textContent = report.questions.length ? `共 ${report.questions.length} 题` : '暂无题目记录';
  if (!report.questions.length) {
    const empty = createElement('div', 'topic-empty', '报告没有返回逐题记录，请稍后刷新或重新生成报告。');
    list.append(empty);
    return;
  }
  report.questions.forEach((question, index) => {
    const card = createElement('details', 'question-card');
    if (index === 0) card.open = true;
    const summary = createElement('summary');
    const main = createElement('div', 'question-main');
    main.append(createElement('strong', '', question.question), createElement('small', '', question.category));
    const score = createElement('span', 'question-score');
    if (question.score === null) score.textContent = '—';
    else score.append(document.createTextNode(question.score.toFixed(1)), createElement('small', '', '/10'));
    summary.append(createElement('span', 'question-index', String(question.index).padStart(2, '0')), main, score, createElement('i', 'chevron'));

    const detail = createElement('div', 'question-detail');
    const answer = createElement('section', 'answer-block full');
    answer.append(createElement('div', 'answer-label', '你的回答'), createElement('p', '', question.answer));
    const deductions = createElement('section', 'answer-block');
    deductions.append(createElement('div', 'answer-label', '具体扣分点'));
    const deductionList = createElement('ul', 'deduction-list');
    const points = question.deductions.length ? question.deductions : ['本题未返回明确扣分点。'];
    points.forEach((point) => deductionList.append(createElement('li', '', point)));
    deductions.append(deductionList);
    const improved = createElement('section', 'answer-block improved');
    improved.append(createElement('div', 'answer-label', '改写示范'), createElement('p', '', question.improved));
    detail.append(answer, deductions, improved);
    card.append(summary, detail);
    list.append(card);
  });
}

function safeResourceLink(link) {
  try {
    const url = new URL(link.url, location.origin);
    if (url.protocol !== 'https:' && url.protocol !== 'http:') return null;
    return url.href;
  } catch {
    return null;
  }
}

function renderPractice(report) {
  const grid = $('#practiceGrid');
  grid.replaceChildren();
  const practices = report.practice.length ? report.practice : [{
    topic: '按最低评分维度复盘',
    reason: `优先练习“${[...report.rubric].sort((a, b) => a.score - b.score)[0]?.label || '基础知识'}”，并用 STAR 和请求链路组织回答。`,
    links: [],
  }];
  practices.slice(0, 6).forEach((practice, index) => {
    const card = createElement('article', 'practice-card');
    card.append(createElement('span', 'practice-number', String(index + 1).padStart(2, '0')), createElement('h3', '', practice.topic), createElement('p', '', practice.reason));
    const resources = createElement('div', 'resource-links');
    const links = practice.links.length ? practice.links : [
      { label: 'JavaGuide', url: 'https://javaguide.cn/' },
      { label: 'CodeTop', url: 'https://codetop.cc/home' },
    ];
    links.forEach((link) => {
      const href = safeResourceLink(link);
      if (!href) return;
      const anchor = createElement('a', '', `${link.label} ↗`);
      anchor.href = href;
      anchor.target = '_blank';
      anchor.rel = 'noopener noreferrer';
      resources.append(anchor);
    });
    card.append(resources);
    grid.append(card);
  });
}

function renderHintUsage(report) {
  const events = report.hintEvents || [];
  $('#hintUsageCount').textContent = `${events.length} 次`;
  $('#hintUsageEmpty').classList.toggle('is-hidden', events.length > 0);
  const list = $('#hintUsageList');
  list.replaceChildren();
  events.forEach((event) => {
    const item = createElement('article', 'hint-usage-item');
    item.append(
      createElement('span', '', `第 ${event.ordinal} 题`),
      createElement('strong', '', event.question),
      createElement('p', '', event.hint || '本题使用了一次思路拆解提示。'),
    );
    list.append(item);
  });
}

function renderCurrent(report) {
  currentReport = report;
  $('#reportTitle').textContent = `${companyLabel(report.company)} · ${report.specialization}一面报告`;
  const pressureLabels = ['无压力', '温和压力', '标准压力', '高压'];
  const durationLabel = report.unlimited ? '不限时 · 手动结束' : (report.duration ? `${report.duration} 分钟` : '');
  const memoryLabel = report.memoryEnabled ? '参与弱项记忆' : '未参与弱项记忆';
  const languageLabel = report.languageMode === 'zh' ? '全程中文' : '中英双语';
  const tags = [formatDate(report.endedAt), durationLabel, pressureLabels[report.stressLevel], languageLabel, memoryLabel].filter(Boolean);
  $('#reportMeta').textContent = tags.join(' · ');
  $('#reportSummary').textContent = report.summary;
  $('#overallScore').textContent = report.overall.toFixed(1);
  renderRubric(report);
  renderQuestions(report);
  renderPractice(report);
  renderHintUsage(report);
}

function showView(view) {
  activeView = view === 'history' ? 'history' : 'current';
  $$('.report-tab').forEach((tab) => tab.classList.toggle('is-active', tab.dataset.view === activeView));
  const waitingForCurrent = activeView === 'current' && !currentReport && reportLoadingActive;
  $('#reportLoading').classList.toggle('is-hidden', !waitingForCurrent);
  $('#currentView').classList.toggle('is-hidden', activeView !== 'current' || !currentReport);
  $('#historyView').classList.toggle('is-hidden', activeView !== 'history' || !historyReports.length);
  const empty = !waitingForCurrent && ((activeView === 'current' && !currentReport) || (activeView === 'history' && !historyReports.length));
  $('#reportEmpty').classList.toggle('is-hidden', !empty);
  const url = new URL(location.href);
  if (activeView === 'history') url.searchParams.set('view', 'history');
  else url.searchParams.delete('view');
  history.replaceState({}, '', url);
  if (activeView === 'history' && historyReports.length) requestAnimationFrame(updateComparison);
}

function historyLabel(report) {
  return `${formatDate(report.endedAt)} · ${companyLabel(report.company)} · ${report.overall.toFixed(1)} 分`;
}

function populateComparisonSelects() {
  const currentSelect = $('#currentCompare');
  const previousSelect = $('#previousCompare');
  const selectedCurrent = currentSelect.value;
  const selectedPrevious = previousSelect.value;
  currentSelect.replaceChildren();
  previousSelect.replaceChildren();
  historyReports.forEach((report) => {
    const optionA = createElement('option', '', historyLabel(report));
    optionA.value = report.id;
    currentSelect.append(optionA);
    const optionB = optionA.cloneNode(true);
    previousSelect.append(optionB);
  });
  if (historyReports.length === 1) {
    const emptyOption = createElement('option', '', '暂无其他场次');
    emptyOption.value = '';
    previousSelect.prepend(emptyOption);
  }
  currentSelect.value = historyReports.some((item) => item.id === selectedCurrent) ? selectedCurrent : historyReports[0]?.id || '';
  const defaultPrevious = historyReports.find((item) => item.id !== currentSelect.value)?.id || '';
  previousSelect.value = historyReports.some((item) => item.id === selectedPrevious && item.id !== currentSelect.value) ? selectedPrevious : defaultPrevious;
}

function drawRadar(current, previous) {
  const canvas = $('#radarCanvas');
  const context = canvas.getContext('2d');
  const width = canvas.width;
  const height = canvas.height;
  const centerX = width / 2;
  const centerY = height / 2 + 8;
  const radius = Math.min(width, height) * .34;
  const count = rubricDefinitions.length;
  context.clearRect(0, 0, width, height);

  const point = (index, value = 1) => {
    const angle = -Math.PI / 2 + index * Math.PI * 2 / count;
    return [centerX + Math.cos(angle) * radius * value, centerY + Math.sin(angle) * radius * value];
  };

  context.strokeStyle = '#dde2e8';
  context.lineWidth = 1.3;
  for (let ring = 1; ring <= 5; ring += 1) {
    context.beginPath();
    for (let index = 0; index < count; index += 1) {
      const [x, y] = point(index, ring / 5);
      if (index === 0) context.moveTo(x, y); else context.lineTo(x, y);
    }
    context.closePath();
    context.stroke();
  }
  for (let index = 0; index < count; index += 1) {
    const [x, y] = point(index);
    context.beginPath(); context.moveTo(centerX, centerY); context.lineTo(x, y); context.stroke();
    const [labelX, labelY] = point(index, 1.18);
    context.fillStyle = '#667284';
    context.font = '600 22px ui-sans-serif, system-ui, sans-serif';
    context.textAlign = Math.abs(labelX - centerX) < 10 ? 'center' : labelX > centerX ? 'left' : 'right';
    context.textBaseline = 'middle';
    context.fillText(rubricDefinitions[index].label, labelX, labelY);
  }

  const polygon = (report, stroke, fill, dashed = false) => {
    if (!report) return;
    context.save();
    context.strokeStyle = stroke;
    context.fillStyle = fill;
    context.lineWidth = 4;
    context.setLineDash(dashed ? [9, 7] : []);
    context.beginPath();
    report.rubric.forEach((dimension, index) => {
      const [x, y] = point(index, clamp(dimension.score / 10, 0, 1));
      if (index === 0) context.moveTo(x, y); else context.lineTo(x, y);
    });
    context.closePath(); context.fill(); context.stroke();
    context.restore();
  };
  polygon(previous, '#aab4c2', 'rgba(170,180,194,.10)', true);
  polygon(current, '#356eea', 'rgba(53,110,234,.16)');
}

function renderScoreComparison(current, previous) {
  const container = $('#scoreComparison');
  container.replaceChildren();
  current.rubric.forEach((dimension, index) => {
    const item = createElement('div', 'comparison-item');
    item.append(createElement('span', '', dimension.label));
    const value = createElement('strong', '', dimension.score.toFixed(1));
    if (previous) {
      const delta = Math.round((dimension.score - previous.rubric[index].score) * 10) / 10;
      const deltaNode = createElement('em', delta < 0 ? 'down' : '', `${delta > 0 ? '+' : ''}${delta.toFixed(1)}`);
      value.append(deltaNode);
    }
    item.append(value);
    container.append(item);
  });
}

function renderTopicDeltas(current, previous) {
  const container = $('#topicDeltas');
  container.replaceChildren();
  if (!previous) {
    container.append(createElement('div', 'topic-empty', '再完成一场面试后，这里会展示 Redis、MySQL、并发与计网等知识点的分数变化。'));
    return;
  }
  const topics = [...new Set([...Object.keys(current.topicScores), ...Object.keys(previous.topicScores)])]
    .filter((topic) => current.topicScores[topic] !== undefined && previous.topicScores[topic] !== undefined)
    .map((topic) => ({ topic, current: current.topicScores[topic], previous: previous.topicScores[topic] }))
    .sort((a, b) => Math.abs(b.current - b.previous) - Math.abs(a.current - a.previous));
  if (!topics.length) {
    container.append(createElement('div', 'topic-empty', '两份报告暂无可直接比较的共同知识点；上方仍可比较四项核心能力。'));
    return;
  }
  topics.slice(0, 6).forEach((item) => {
    const delta = Math.round((item.current - item.previous) * 10) / 10;
    const card = createElement('article', 'topic-delta');
    const header = createElement('header');
    header.append(createElement('strong', '', item.topic), createElement('em', delta < 0 ? 'down' : '', `${delta > 0 ? '↑ +' : delta < 0 ? '↓ ' : '→ '}${delta.toFixed(1)}`));
    card.append(header, createElement('p', '', `上次 ${item.previous.toFixed(1)} 分 · 这次 ${item.current.toFixed(1)} 分`));
    container.append(card);
  });
}

function updateComparison() {
  if (!historyReports.length) return;
  const current = historyReports.find((item) => item.id === $('#currentCompare').value) || historyReports[0];
  const previous = historyReports.find((item) => item.id === $('#previousCompare').value && item.id !== current.id) || null;
  drawRadar(current, previous);
  renderScoreComparison(current, previous);
  renderTopicDeltas(current, previous);
}

function renderHistoryList() {
  const list = $('#historyList');
  list.replaceChildren();
  $('#historyCount').textContent = `${historyReports.length} 场`;
  historyReports.forEach((report) => {
    const item = createElement('article', 'history-item');
    const score = createElement('span', 'history-score', report.overall.toFixed(1));
    const copy = createElement('div', 'history-copy');
    const durationLabel = report.unlimited ? '不限时' : (report.duration ? `${report.duration} 分钟` : '');
    copy.append(createElement('strong', '', `${companyLabel(report.company)} · ${report.specialization}一面`), createElement('small', '', `${formatDate(report.endedAt)}${durationLabel ? ` · ${durationLabel}` : ''}`));
    const tags = createElement('div', 'history-tags');
    tags.append(createElement('span', '', ['无压力', '温和压力', '标准压力', '高压'][report.stressLevel]));
    if (report.endReason) tags.append(createElement('span', '', /poor|early|提前/i.test(report.endReason) ? '提前结束' : '已完成'));
    const actions = createElement('div', 'history-actions');
    const view = createElement('a', '', '查看');
    view.href = `/report?session=${encodeURIComponent(report.id)}`;
    const remove = createElement('button', '', '删除');
    remove.type = 'button';
    remove.dataset.deleteId = report.id;
    actions.append(view, remove);
    item.append(score, copy, tags, actions);
    list.append(item);
  });
}

function mergeHistory(remoteRows) {
  const records = [...remoteRows, ...getCachedReports()];
  if (currentReport) {
    records.unshift({
      ...currentReport.raw,
      id: currentReport.id,
      session_id: currentReport.id,
      company: currentReport.company,
      role: currentReport.role,
      specialization: currentReport.specialization,
      language_mode: currentReport.languageMode,
      stress: currentReport.stress,
      stress_level: currentReport.stressLevel,
      duration_minutes: currentReport.unlimited ? null : currentReport.duration,
      ended_at: currentReport.endedAt,
      end_reason: currentReport.endReason,
      memory_enabled: currentReport.memoryEnabled,
      hint_events: currentReport.hintEvents,
    });
  }
  const byId = new Map();
  records.forEach((row, index) => {
    const source = row?.report && typeof row.report === 'object' ? { ...row, ...row.report } : unwrapReport(row) || row;
    const id = reportId(source) || `local-${index}-${reportDate(source)}`;
    if (!id || byId.has(id)) return;
    byId.set(id, normalizeReport({ ...source, id }, row));
  });
  historyReports = [...byId.values()].sort((a, b) => new Date(b.endedAt).getTime() - new Date(a.endedAt).getTime());
  populateComparisonSelects();
  renderHistoryList();
  if (historyReports.length) updateComparison();
}

async function loadHistory() {
  let remote = [];
  try {
    const payload = await apiFetch(`/api/history?client_id=${encodeURIComponent(getClientId())}`, { timeout: 15_000 });
    remote = normalizeHistoryPayload(payload);
  } catch (error) {
    if (!getCachedReports().length) showToast(`历史记录读取失败：${error.message}`, 'error');
  }
  mergeHistory(remote);
  if (activeView === 'history') showView('history');
}

async function loadCurrentReport() {
  if (!sessionId) return;
  try {
    sessionMeta = await apiFetch(`/api/interviews/${encodeURIComponent(sessionId)}`, { timeout: 12_000 });
    sessionMeta = sessionMeta?.interview || sessionMeta?.session || sessionMeta;
  } catch {
    const stored = getCurrentSession();
    sessionMeta = stored && String(stored.id || stored.session_id) === String(sessionId)
      ? stored
      : {};
  }

  for (let attempt = 0; attempt < 75 && !reportPollStopped; attempt += 1) {
    try {
      const payload = await apiFetch(`/api/interviews/${encodeURIComponent(sessionId)}/report`, { timeout: 30_000 });
      if (!isReportPending(payload)) {
        const raw = unwrapReport(payload);
        const enriched = { ...raw, id: reportId(raw) || sessionId, session_id: reportId(raw) || sessionId };
        currentReport = normalizeReport(enriched, sessionMeta || {});
        reportLoadingActive = false;
        cacheReport({
          ...enriched,
          company: currentReport.company,
          specialization: currentReport.specialization,
          language_mode: currentReport.languageMode,
          stress: currentReport.stress,
          stress_level: currentReport.stressLevel,
          duration_minutes: currentReport.unlimited ? null : currentReport.duration,
          ended_at: currentReport.endedAt,
          memory_enabled: currentReport.memoryEnabled,
          hint_events: currentReport.hintEvents,
        }, sessionId);
        renderCurrent(currentReport);
        await loadHistory();
        if (activeView === 'current') showView('current');
        return;
      }
    } catch (error) {
      if (![404, 409, 425].includes(error.status) && attempt > 2) {
        showToast(`报告暂时无法读取：${error.message}`, 'error');
      }
    }
    $('#loadingHint').textContent = attempt < 12 ? '面试官正在复核转写与评分依据，通常需要几十秒。' : '报告仍在生成，请保持页面打开；你也可以先查看历史记录。';
    await new Promise((resolve) => setTimeout(resolve, attempt < 15 ? 2000 : 3500));
  }
  if (!reportPollStopped) {
    reportLoadingActive = false;
    $('#reportLoading').classList.add('is-hidden');
    $('#reportEmpty').classList.remove('is-hidden');
    showToast('报告生成时间超出预期，请稍后刷新页面。', 'error', 7000);
  }
}

async function deleteHistoryItem(id) {
  if (!id || !window.confirm('确定删除这场报告吗？删除后无法在本设备恢复。')) return;
  try {
    await apiFetch(`/api/history/${encodeURIComponent(id)}?client_id=${encodeURIComponent(getClientId())}`, { method: 'DELETE', timeout: 15_000 });
  } catch (error) {
    showToast(`服务端记录删除失败：${error.message}`, 'error');
    return;
  }
  removeCachedReport(id);
  historyReports = historyReports.filter((report) => report.id !== id);
  if (currentReport?.id === id) currentReport = null;
  populateComparisonSelects();
  renderHistoryList();
  if (historyReports.length) updateComparison();
  else showView('history');
  showToast('报告已删除。', 'success');
}

async function clearHistory() {
  if (!historyReports.length || !window.confirm(`确定清空 ${historyReports.length} 场历史报告吗？此操作不可恢复。`)) return;
  const ids = historyReports.map((report) => report.id).filter(Boolean);
  $('#clearHistory').disabled = true;
  const results = await Promise.allSettled(ids.map((id) => apiFetch(`/api/history/${encodeURIComponent(id)}?client_id=${encodeURIComponent(getClientId())}`, { method: 'DELETE', timeout: 15_000 })));
  const failed = results.filter((result) => result.status === 'rejected').length;
  clearCachedReports();
  if (failed) {
    showToast(`${failed} 条服务端记录未能删除，请稍后重试。`, 'error');
    await loadHistory();
  } else {
    historyReports = [];
    currentReport = null;
    renderHistoryList();
    showView('history');
    showToast('历史报告已清空。', 'success');
  }
  $('#clearHistory').disabled = false;
}

async function retryWeaknesses() {
  if (!currentReport?.id) return;
  const button = $('#retryWeakButton');
  try {
    setButtonBusy(button, true, '正在创建弱项复练…');
    const session = await apiFetch(`/api/interviews/${encodeURIComponent(currentReport.id)}/retry`, {
      method: 'POST',
      timeout: 65_000,
      json: { client_id: getClientId() },
    });
    const id = String(session?.id || session?.session_id || '');
    if (!id) throw new Error('服务端没有返回新面试编号。');
    setCurrentSession({
      ...session,
      id,
      client_id: getClientId(),
      company: session?.company || currentReport.company,
      role: session?.role || currentReport.role || 'backend',
      specialization: session?.specialization || currentReport.specialization,
      memory_enabled: true,
      created_at: session?.created_at || new Date().toISOString(),
    });
    showToast('已复用原简历，并把本场弱项加入新剧本。', 'success');
    window.location.assign(`/interview?session=${encodeURIComponent(id)}`);
  } catch (error) {
    setButtonBusy(button, false);
    showToast(error?.message || '创建弱项复练失败。', 'error', 5200);
  }
}

$$('.report-tab').forEach((tab) => tab.addEventListener('click', () => showView(tab.dataset.view)));
$('#currentCompare').addEventListener('change', () => {
  if ($('#previousCompare').value === $('#currentCompare').value) {
    $('#previousCompare').value = historyReports.find((item) => item.id !== $('#currentCompare').value)?.id || '';
  }
  updateComparison();
});
$('#previousCompare').addEventListener('change', updateComparison);
$('#historyList').addEventListener('click', (event) => {
  const button = event.target.closest('[data-delete-id]');
  if (button) deleteHistoryItem(button.dataset.deleteId);
});
$('#clearHistory').addEventListener('click', clearHistory);
$('#retryWeakButton').addEventListener('click', retryWeaknesses);
$('#copySummary').addEventListener('click', async () => {
  if (!currentReport) return;
  const text = `${companyLabel(currentReport.company)}后端一面：${currentReport.overall.toFixed(1)} 分\n${currentReport.summary}`;
  try {
    await navigator.clipboard.writeText(text);
    showToast('报告总结已复制。', 'success');
  } catch {
    showToast('浏览器未允许复制，请手动选择总结文字。', 'error');
  }
});
window.addEventListener('resize', () => {
  if (activeView === 'history' && historyReports.length) updateComparison();
});
window.addEventListener('pagehide', () => { reportPollStopped = true; });

async function initialize() {
  await loadHistory();
  if (sessionId) {
    const cached = historyReports.find((report) => report.id === sessionId);
    if (cached) {
      currentReport = cached;
      reportLoadingActive = false;
      renderCurrent(cached);
      if (activeView === 'current') showView('current');
    }
    loadCurrentReport();
  }
  else showView('history');
}

initialize();

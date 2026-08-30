import {
  $, $$, apiFetch, cacheReport, clamp, clearCachedReports, companyLabel,
  firstValue, formatDate, getCachedReports, getClientId, getCurrentSession,
  normalizeHistoryPayload, removeCachedReport, score10, setButtonBusy,
  setCurrentSession, showToast, toArray,
} from './common.js?v=20260830-hide-internals';

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

const holisticDimensionDefinitions = [
  { key: 'resume', label: '简历质量', aliases: ['resume', 'resume_quality', 'resume_strength', '简历质量', '简历'] },
  { key: 'project', label: '项目深度', aliases: ['project', 'project_depth', '项目深度', '项目'] },
  { key: 'fundamentals', label: '基础知识', aliases: ['fundamentals', 'basic_knowledge', 'basics', '基础知识', '基础八股'] },
  { key: 'problem_solving', label: '解题思路', aliases: ['problem_solving', 'coding', 'coding_thought', 'algorithm', '解题思路', '手撕思路'] },
  { key: 'delivery', label: '表达流畅度', aliases: ['delivery', 'speech_delivery', 'fluency', 'communication', '表达流畅度', '语速措辞及流畅度'] },
  { key: 'timing', label: '时间把握', aliases: ['timing', 'time_management', 'time_control', '时间把握', '时间管理'] },
  { key: 'role_fit', label: '岗位契合度', aliases: ['role_fit', 'job_fit', 'position_fit', '岗位契合度', '岗位匹配度'] },
  { key: 'pressure', label: '抗压与应变', aliases: ['pressure', 'stress_resilience', 'adaptability', '抗压与应变', '抗压表现'] },
];

const processAreaDefinitions = [
  { key: 'timing', label: '时间把握', aliases: ['time_management', 'timing', 'time_control', '时间把握', '时间管理'] },
  { key: 'pace', label: '语速节奏', aliases: ['pace', 'speech_rate', 'speaking_rate', '语速', '语速节奏'] },
  { key: 'wording', label: '措辞准确性', aliases: ['wording', 'word_choice', 'terminology', '措辞', '措辞准确性'] },
  { key: 'fluency', label: '流畅与结构', aliases: ['fluency', 'delivery', 'structure', '流畅度', '表达结构'] },
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
    return root.find((item) => {
      const key = String(firstValue(item, ['key', 'name', 'label', 'dimension'], '')).toLowerCase();
      return definition.aliases.some((alias) => String(alias).toLowerCase() === key);
    });
  }
  for (const alias of definition.aliases) {
    if (root[alias] !== undefined) return root[alias];
    const matchedKey = Object.keys(root).find((key) => key.toLowerCase() === String(alias).toLowerCase());
    if (matchedKey) return root[matchedKey];
  }
  return undefined;
}

function scoreOrNull(value) {
  const raw = typeof value === 'object' && value !== null
    ? firstValue(value, ['score', 'value', 'rating', '得分'], null)
    : value;
  if (raw === null || raw === undefined || raw === '') return null;
  const numeric = Number(raw);
  return Number.isFinite(numeric) ? score10(numeric, null) : null;
}

function normalizeTextList(value) {
  return toArray(value).map((item) => {
    if (typeof item === 'string' || typeof item === 'number') return String(item);
    return String(firstValue(item, ['text', 'point', 'reason', 'description', 'name', '扣分点'], '') || '');
  }).filter(Boolean);
}

function flagEnabled(value, fallback = true) {
  if (value === undefined || value === null || value === '') return fallback;
  if (typeof value === 'string') return !['false', '0', 'no', 'off'].includes(value.trim().toLowerCase());
  return Boolean(value);
}

function normalizeScoredEntry(entry, fallbackFeedback = '本场未形成足够证据，暂不评分。') {
  const explicitScorable = typeof entry === 'object' && entry !== null
    ? firstValue(entry, ['scorable', 'scoreable', 'has_evidence'], undefined)
    : undefined;
  const status = typeof entry === 'object' && entry !== null
    ? String(firstValue(entry, ['status', 'score_status'], '') || '').toLowerCase()
    : '';
  const rawScore = scoreOrNull(entry);
  const explicitlyBlocked = explicitScorable !== undefined && !flagEnabled(explicitScorable, false);
  const scorable = entry !== undefined
    && !explicitlyBlocked
    && !['insufficient_data', 'not_scorable', 'unscored', 'missing'].includes(status)
    && rawScore !== null;
  const evidence = typeof entry === 'object' && entry !== null
    ? normalizeTextList(firstValue(entry, ['evidence', 'evidences', '依据', '证据'], []))
    : [];
  const feedback = typeof entry === 'object' && entry !== null
    ? String(firstValue(entry, ['feedback', 'summary', 'comment', 'description', 'analysis', '评价'], '') || '')
    : '';
  return {
    score: scorable ? rawScore : null,
    scorable,
    evidence,
    feedback: feedback || (scorable ? '该分数基于本场有效回答证据。' : fallbackFeedback),
  };
}

function normalizeAnalysis(value) {
  if (typeof value === 'string') {
    return { score: null, summary: value, strengths: [], weaknesses: [], suggestions: [], content: [], layout: [], rewritten: [], evidence: [] };
  }
  const source = value && typeof value === 'object' ? value : {};
  const scoreEntry = normalizeScoredEntry(source.overall ?? source);
  const strengths = normalizeTextList(firstValue(source, ['strengths', 'strong_points', 'highlights', 'matched_requirements', '优势', '亮点'], []));
  const weaknesses = normalizeTextList(firstValue(source, ['weaknesses', 'weak_points', 'gaps', 'risks', '不足', '弱项'], []));
  const suggestions = normalizeTextList(firstValue(source, ['suggestions', 'recommendations', 'advice', 'rewrite_suggestions', 'improvement_plan', '建议', '改进建议', '改写建议'], []));
  const rewrittenRaw = firstValue(source, ['rewritten_examples', 'rewrite_examples', '改写示例'], []);
  const rewritten = toArray(rewrittenRaw).map((item) => {
    if (typeof item === 'string') return item;
    const before = String(firstValue(item, ['before', 'original', '原文'], '') || '');
    const after = String(firstValue(item, ['after', 'rewritten', 'improved', '改写后'], '') || '');
    return before && after ? `原文：${before}\n改写：${after}` : after || before;
  }).filter(Boolean);
  const explicitSummary = String(firstValue(source, ['summary', 'overview', 'assessment', 'analysis', '总结', '分析'], '') || '');
  return {
    score: scoreEntry.score,
    summary: explicitSummary || [strengths[0] && `优势：${strengths[0]}`, weaknesses[0] && `待提升：${weaknesses[0]}`].filter(Boolean).join('；'),
    strengths,
    weaknesses,
    suggestions,
    content: normalizeTextList(firstValue(source, ['content_suggestions', 'content_advice', 'content', '内容建议'], [])),
    layout: normalizeTextList(firstValue(source, ['layout_suggestions', 'formatting_suggestions', 'layout', 'formatting', '排版建议'], [])),
    rewritten,
    evidence: scoreEntry.evidence,
  };
}

function normalizeProcessAreas(value) {
  const source = value && typeof value === 'object' ? value : {};
  return processAreaDefinitions.map((definition) => {
    const entry = findDimension(source, definition);
    const normalized = normalizeScoredEntry(entry);
    const strengths = normalizeTextList(firstValue(entry, ['strengths', '优势'], []));
    const weaknesses = normalizeTextList(firstValue(entry, ['weaknesses', '不足'], []));
    const suggestions = normalizeTextList(firstValue(entry, ['suggestions', '建议'], []));
    const explicitDetail = typeof entry === 'string'
      ? entry
      : String(firstValue(entry, ['summary', 'feedback', 'analysis', 'comment', '评价'], '') || '');
    const detail = explicitDetail || [
      strengths[0] && `表现：${strengths[0]}`,
      weaknesses[0] && `不足：${weaknesses[0]}`,
      suggestions[0] && `建议：${suggestions[0]}`,
      !strengths.length && !weaknesses.length && !suggestions.length ? normalized.feedback : '',
    ].filter(Boolean).join('；');
    const metrics = [];
    if (definition.key === 'timing' && Number.isFinite(Number(source.average_answer_seconds))) {
      metrics.push(`平均回答 ${Math.round(Number(source.average_answer_seconds))} 秒`);
    }
    if (definition.key === 'pace' && Number.isFinite(Number(source.average_speech_rate_cpm))) {
      metrics.push(`平均语速 ${Math.round(Number(source.average_speech_rate_cpm))} 字/分钟`);
    }
    return { ...definition, ...normalized, detail, metrics };
  });
}

function normalizeCitation(value, index) {
  if (typeof value === 'string') return { title: `公开面经 ${index + 1}`, url: value, excerpt: '', platform: '' };
  const item = value && typeof value === 'object' ? value : {};
  return {
    title: String(firstValue(item, ['title', 'name', 'post_title', '标题'], `公开面经 ${index + 1}`) || `公开面经 ${index + 1}`),
    url: String(firstValue(item, ['url', 'href', 'link', 'source_url', '链接'], '') || ''),
    excerpt: (() => {
      const raw = firstValue(item, ['excerpt', 'summary', 'report_takeaway', 'key_point', 'note', 'takeaways', '摘要'], '');
      const takeawayText = Array.isArray(item.takeaways) ? normalizeTextList(item.takeaways).join('；') : '';
      const primary = Array.isArray(raw) ? normalizeTextList(raw).join('；') : String(raw || '');
      return [primary, takeawayText].filter(Boolean).join('；');
    })(),
    platform: String(firstValue(item, ['platform', 'site', 'source', '平台'], '') || ''),
    date: String(firstValue(item, ['date', 'published_at', 'published_date', '日期'], '') || ''),
    round: String(firstValue(item, ['round', 'interview_round', '轮次'], '') || ''),
  };
}

function normalizeCompanyInsights(value) {
  if (typeof value === 'string') return { summary: value, patterns: [], advice: [], citations: [] };
  const source = value && typeof value === 'object' ? value : {};
  const citations = firstValue(source, ['citations', 'sources', 'references', 'experience_posts', '面经引用', '引用'], []);
  const patterns = normalizeTextList(firstValue(source, ['recurring_patterns', 'patterns', 'common_patterns', '高频共性'], []));
  const caveat = String(firstValue(source, ['sample_caveat', 'caveat', '样本说明'], '') || '');
  const synthesis = String(firstValue(source, ['summary', 'overview', 'synthesis', 'company_summary', '综合总结'], '') || '')
    || (patterns.length ? `公开面经反复出现的关注点包括：${patterns.slice(0, 3).join('；')}` : '');
  return {
    summary: [caveat, synthesis].filter(Boolean).join(' '),
    patterns,
    advice: normalizeTextList(firstValue(source, ['interview_advice', 'advice', 'suggestions', 'recommendations', 'company_advice', '面试建议', '建议'], [])),
    citations: toArray(citations).map(normalizeCitation).filter((citation) => citation.url),
  };
}

function normalizeQuestion(item, index, reportScored = true) {
  const deductions = normalizeTextList(firstValue(item, ['deductions', 'deduction_points', 'issues', 'weaknesses', '扣分点'], []));
  const rawScore = firstValue(item, ['score', 'points', '得分'], null);
  const status = String(firstValue(item, ['status', 'score_status'], '') || '').toLowerCase();
  const score = scoreOrNull(rawScore);
  const explicitlyScorable = firstValue(item, ['scorable', 'scoreable'], undefined);
  const scored = reportScored
    && flagEnabled(firstValue(item, ['scored'], true))
    && !(explicitlyScorable !== undefined && !flagEnabled(explicitlyScorable, false))
    && !['insufficient_data', 'not_scorable', 'unscored', 'missing'].includes(status)
    && score !== null;
  return {
    index: index + 1,
    question: String(firstValue(item, ['question', 'prompt', 'q', '题目'], `第 ${index + 1} 题`) || `第 ${index + 1} 题`),
    answer: String(firstValue(item, ['candidate_answer', 'answer', 'response', 'user_answer', '候选人回答'], '未记录到完整回答') || '未记录到完整回答'),
    improved: String(firstValue(item, ['improved_answer', 'rewrite', 'example_answer', 'better_answer', '改写示范'], '暂无改写示范') || '暂无改写示范'),
    category: String(firstValue(item, ['category', 'dimension', 'type', '考察点'], '综合追问') || '综合追问'),
    score: scored ? score : null,
    scored,
    deductions,
    evidence: normalizeTextList(firstValue(item, ['evidence', 'evidences', '证据'], [])),
    recommendedSeconds: Number(firstValue(item, ['recommended_answer_seconds', 'suggested_answer_seconds'], 0)) || 0,
    durationSeconds: Number(firstValue(item, ['answer_duration_seconds', 'duration_seconds'], 0)) || 0,
    inputMode: String(firstValue(item, ['input_mode'], '') || ''),
    transcriptEdited: flagEnabled(firstValue(item, ['transcript_edited'], false), false),
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
      scoreOrNull(firstValue(item, ['score', 'value'], null)),
    ]).filter(([topic, score]) => topic && score !== null));
  }
  return Object.fromEntries(Object.entries(value)
    .map(([topic, score]) => [topic, scoreOrNull(score)])
    .filter(([, score]) => score !== null));
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
  const scoreStatus = String(firstValue(
    report,
    ['score_status'],
    firstValue(metadata, ['score_status'], ''),
  ) || '');
  const explicitScored = firstValue(report, ['scored'], firstValue(metadata, ['scored'], undefined));
  const normalizedStatus = scoreStatus.toLowerCase();
  const declaredScored = normalizedStatus === 'scored'
    || (explicitScored !== undefined && flagEnabled(explicitScored, false));
  const scored = declaredScored
    && !['insufficient_data', 'unscorable', 'not_scorable', 'unscored', 'missing'].includes(normalizedStatus);
  const rubricRoot = firstValue(report, ['rubric', 'dimension_scores', 'dimensions', '评分细则'], {});
  const scoresRoot = firstValue(report, ['scores', 'score_breakdown', '评分'], {});
  const rubric = rubricDefinitions.map((definition) => {
    const entry = findDimension(rubricRoot, definition) ?? findDimension(scoresRoot, definition);
    const normalized = normalizeScoredEntry(entry);
    const deductions = typeof entry === 'object' && entry !== null
      ? normalizeTextList(firstValue(entry, ['deductions', 'issues', '扣分点'], []))
      : [];
    return {
      ...definition,
      score: scored ? normalized.score : null,
      scorable: scored && normalized.scorable,
      evidence: normalized.evidence,
      feedback: scored
        ? (normalized.feedback || deductions.slice(0, 2).join('；'))
        : '有效回答不足，暂不评分。',
    };
  });

  const questionsRaw = firstValue(report, ['question_reviews', 'question_feedback', 'questions', 'per_question', '逐题反馈', '逐题扣分'], []);
  const practiceRaw = firstValue(report, ['must_practice', 'practice_list', 'next_practice', 'practice_plan', '下次必练清单'], []);
  const overallRaw = firstValue(report, ['overall_score', 'total_score', 'score', '综合得分'], null);
  const explicitCoverage = firstValue(report, ['score_coverage', 'scoring_coverage', 'coverage', '评分覆盖率'], null);
  let coverage = null;
  if (typeof explicitCoverage === 'object' && explicitCoverage !== null) {
    const percent = firstValue(explicitCoverage, ['percent', 'percentage', 'value'], null);
    const covered = Number(firstValue(explicitCoverage, ['scored_dimensions', 'covered', 'count'], Number.NaN));
    const total = Number(firstValue(explicitCoverage, ['total_dimensions', 'total'], Number.NaN));
    if (percent !== null && Number.isFinite(Number(percent))) coverage = Number(percent);
    else if (Number.isFinite(covered) && Number.isFinite(total) && total > 0) coverage = covered / total * 100;
  } else if (explicitCoverage !== null && Number.isFinite(Number(explicitCoverage))) {
    coverage = Number(explicitCoverage);
  }
  if (coverage !== null && coverage >= 0 && coverage <= 1) coverage *= 100;
  if (coverage === null) coverage = rubric.filter((item) => item.score !== null).length / rubric.length * 100;
  coverage = Math.round(clamp(coverage, 0, 100));
  const overallScore = scoreOrNull(overallRaw);
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
  const radarRoot = firstValue(report, ['radar', 'radar_dimensions', 'holistic_radar', '能力雷达'], {});
  const radar = Array.isArray(radarRoot) && radarRoot.length
    ? radarRoot.slice(0, 12).map((entry, index) => ({
      key: String(firstValue(entry, ['key', 'name'], `dimension_${index + 1}`)),
      label: String(firstValue(entry, ['label', 'name'], `维度 ${index + 1}`)),
      ...normalizeScoredEntry(entry),
    }))
    : holisticDimensionDefinitions.map((definition) => {
      const entry = findDimension(radarRoot, definition)
        ?? findDimension(rubricRoot, definition)
        ?? findDimension(scoresRoot, definition);
      return { ...definition, ...normalizeScoredEntry(entry) };
    });
  const resumeAnalysis = normalizeAnalysis(firstValue(report, ['resume_analysis', 'resume_review', '简历分析'], {}));
  const processRoot = firstValue(report, ['process_analysis', 'interview_process_analysis', 'delivery_analysis', '过程分析'], {});
  const roleFit = normalizeAnalysis(firstValue(report, ['role_fit', 'role_fit_analysis', 'job_fit', '岗位契合度'], {}));
  const companyInsights = normalizeCompanyInsights(firstValue(report, ['company_insights', 'company_interview_insights', 'company_advice', '大厂面经分析'], {}));
  return {
    raw: report,
    id,
    company: firstValue(report, ['company'], firstValue(metadata, ['company'], '')),
    role: firstValue(report, ['role'], firstValue(metadata, ['role'], 'backend')),
    interviewType: firstValue(report, ['interview_type'], firstValue(metadata, ['interview_type'], 'technical')) === 'technical_hr'
      ? 'technical_hr'
      : 'technical',
    specialization: String(firstValue(report, ['specialization'], firstValue(metadata, ['specialization'], '通用后端')) || '通用后端'),
    languageMode: ['zh', 'bilingual', 'en'].includes(
      firstValue(report, ['language_mode'], firstValue(metadata, ['language_mode'], 'bilingual')),
    ) ? firstValue(report, ['language_mode'], firstValue(metadata, ['language_mode'], 'bilingual')) : 'bilingual',
    stress: stressLevel > 0,
    stressLevel,
    unlimited: durationRaw === null,
    duration: durationRaw === null ? 0 : Number(durationRaw) || 0,
    endedAt: reportDate(report) || reportDate(metadata) || new Date().toISOString(),
    endReason: String(firstValue(report, ['end_reason', 'reason'], firstValue(metadata, ['end_reason'], '')) || ''),
    summary: String(firstValue(report, ['summary', 'overall_feedback', 'conclusion', '总结', '总体评价'], '本场报告已生成，请结合逐题扣分点安排下一次练习。')),
    scored,
    scoreStatus: scored ? 'scored' : 'insufficient_data',
    overall: scored && coverage > 0 ? overallScore : null,
    scoreCoverage: scored ? coverage : 0,
    rubric,
    questions: toArray(questionsRaw).map((item, index) => normalizeQuestion(item, index, scored)),
    practice: toArray(practiceRaw).map(normalizePractice),
    topicScores: scored ? normalizeTopicScores(firstValue(report, ['topic_scores', 'knowledge_scores', '知识点得分'], {})) : {},
    hintEvents,
    memoryEnabled,
    radar,
    resumeAnalysis,
    processAreas: normalizeProcessAreas(processRoot),
    roleFit,
    companyInsights,
  };
}

function isReportPending(payload) {
  const status = String(firstValue(payload, ['status', 'state'], '') || '').toLowerCase();
  if (['pending', 'generating', 'processing', 'reporting', 'running', 'queued'].includes(status)) return true;
  const candidate = unwrapReport(payload);
  if (!candidate) return true;
  const reportKeys = ['scored', 'overall_score', 'total_score', 'score', 'rubric', 'question_reviews', 'question_feedback', '逐题反馈', '综合得分'];
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
    const hasScore = report.scored && Number.isFinite(dimension.score);
    const card = createElement('article', `rubric-card${hasScore ? '' : ' is-unscored'}`);
    const top = createElement('div', 'rubric-top');
    top.append(createElement('h3', '', dimension.label), createElement('span', 'rubric-weight', `权重 ${dimension.weight}%`));
    const score = createElement('div', 'rubric-score');
    score.append(
      createElement('strong', '', hasScore ? dimension.score.toFixed(1) : '—'),
      createElement('small', '', hasScore ? '/ 10' : '数据不足'),
    );
    const track = createElement('progress', 'score-track');
    track.max = 10;
    track.value = hasScore ? clamp(dimension.score, 0, 10) : 0;
    track.setAttribute('aria-label', hasScore ? `${dimension.label} ${dimension.score.toFixed(1)} 分` : `${dimension.label} 数据不足`);
    card.append(top, score, track, createElement('p', '', dimension.feedback));
    if (dimension.evidence?.length) card.append(createElement('small', 'rubric-evidence', `依据：${dimension.evidence.slice(0, 2).join('；')}`));
    grid.append(card);
  });
}

function renderQuestions(report) {
  const list = $('#questionList');
  list.replaceChildren();
  $('#questionCount').textContent = report.questions.length ? `共 ${report.questions.length} 题` : '暂无题目记录';
  if (!report.questions.length) {
    const empty = createElement(
      'div',
      'topic-empty',
      report.scored
        ? '报告没有返回逐题记录，请稍后刷新或重新生成报告。'
        : '本场在形成可评分的完整回答前结束，因此不生成虚构分数或逐题扣分；你仍可参考下方练习建议。',
    );
    list.append(empty);
    return;
  }
  report.questions.forEach((question, index) => {
    const card = createElement('details', 'question-card');
    if (index === 0) card.open = true;
    const summary = createElement('summary');
    const main = createElement('div', 'question-main');
    const timing = [
      question.recommendedSeconds ? `建议 ${Math.round(question.recommendedSeconds)} 秒` : '',
      question.durationSeconds ? `实际 ${Math.round(question.durationSeconds)} 秒` : '',
      question.transcriptEdited ? '转写已修正' : '',
    ].filter(Boolean);
    main.append(createElement('strong', '', question.question), createElement('small', '', [question.category, ...timing].join(' · ')));
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
    if (question.evidence.length) deductions.append(createElement('small', 'question-evidence', `评分依据：${question.evidence.join('；')}`));
    const improved = createElement('section', 'answer-block improved');
    improved.append(createElement('div', 'answer-label', '改写示范'), createElement('p', '', question.improved));
    detail.append(answer, deductions, improved);
    const needsReview = question.score === null || question.score <= 6;
    if (needsReview && report.id) {
      const actions = createElement('div', 'question-review-actions');
      const retry = createElement('a', 'secondary-button', '单独重答这题');
      retry.href = `/practice?review=${encodeURIComponent(report.id)}&ordinal=${encodeURIComponent(question.index)}`;
      actions.append(retry);
      detail.append(actions);
    }
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
    topic: report.scored ? '按最低评分维度复盘' : '先完成一轮有效回答',
    reason: report.scored
      ? `优先练习“${report.rubric.filter((item) => Number.isFinite(item.score)).sort((a, b) => a.score - b.score)[0]?.label || '有明确回答证据的知识点'}”，并用 STAR 和请求链路组织回答。`
      : '本场有效回答不足，建议先完整回答至少一道项目题和一道基础题，再依据真实扣分安排复练。',
    links: [],
  }];
  practices.slice(0, 6).forEach((practice, index) => {
    const card = createElement('article', 'practice-card');
    card.append(createElement('span', 'practice-number', String(index + 1).padStart(2, '0')), createElement('h3', '', practice.topic), createElement('p', '', practice.reason));
    const resources = createElement('div', 'resource-links');
    resources.append(createElement('span', 'learning-resource-label', '学习资料'));
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

function appendAnalysisGroup(container, title, items, tone = '') {
  if (!items?.length) return;
  const section = createElement('section', `analysis-group${tone ? ` is-${tone}` : ''}`);
  section.append(createElement('h4', '', title));
  const list = createElement('ul');
  items.forEach((item) => list.append(createElement('li', '', item)));
  section.append(list);
  container.append(section);
}

function renderResumeAnalysis(report) {
  const analysis = report.resumeAnalysis;
  $('#resumeAnalysisScore').textContent = Number.isFinite(analysis.score) ? analysis.score.toFixed(1) : '—';
  $('#resumeAnalysisSummary').textContent = analysis.summary || '本场报告没有形成足够的简历分析证据。';
  const detail = $('#resumeAnalysisDetail');
  detail.replaceChildren();
  appendAnalysisGroup(detail, '亮点', analysis.strengths, 'positive');
  appendAnalysisGroup(detail, '薄弱点', analysis.weaknesses, 'warning');
  appendAnalysisGroup(detail, '内容与改写建议', [...analysis.content, ...analysis.suggestions]);
  appendAnalysisGroup(detail, '改写示例', analysis.rewritten, 'example');
  appendAnalysisGroup(detail, '排版建议', analysis.layout);
  appendAnalysisGroup(detail, '分析依据', analysis.evidence);
  if (!detail.children.length) detail.append(createElement('p', 'analysis-empty', '暂无可落到具体简历内容的建议。'));
}

function renderProcessAnalysis(report) {
  const detail = $('#processAnalysisDetail');
  detail.replaceChildren();
  report.processAreas.forEach((area) => {
    const item = createElement('section', `process-area${area.scorable ? '' : ' is-unscored'}`);
    const header = createElement('div');
    header.append(
      createElement('strong', '', area.label),
      createElement('span', '', Number.isFinite(area.score) ? `${area.score.toFixed(1)} / 10` : '— · 证据不足'),
    );
    item.append(header, createElement('p', '', area.detail));
    const evidence = [
      ...area.metrics,
      ...area.evidence.slice(0, 2).map((value) => `依据：${value}`),
    ];
    if (evidence.length) item.append(createElement('small', '', evidence.join(' · ')));
    detail.append(item);
  });
}

function renderRoleFit(report) {
  const analysis = report.roleFit;
  $('#roleFitScore').textContent = Number.isFinite(analysis.score) ? analysis.score.toFixed(1) : '—';
  $('#roleFitSummary').textContent = analysis.summary || '本场报告没有形成足够的岗位契合证据。';
  const detail = $('#roleFitDetail');
  detail.replaceChildren();
  appendAnalysisGroup(detail, '匹配优势', analysis.strengths, 'positive');
  appendAnalysisGroup(detail, '岗位差距', analysis.weaknesses, 'warning');
  appendAnalysisGroup(detail, '提升建议', analysis.suggestions);
  appendAnalysisGroup(detail, '分析依据', analysis.evidence);
  if (!detail.children.length) detail.append(createElement('p', 'analysis-empty', '暂无可验证的岗位匹配细项。'));
}

function drawHolisticRadar(report) {
  const canvas = $('#holisticRadarCanvas');
  const context = canvas.getContext('2d');
  const width = canvas.width;
  const height = canvas.height;
  const centerX = width / 2;
  const centerY = height / 2 + 8;
  const radius = Math.min(width, height) * .34;
  const dimensions = report.radar;
  const count = dimensions.length;
  const point = (index, scale = 1) => {
    const angle = -Math.PI / 2 + index * Math.PI * 2 / count;
    return [centerX + Math.cos(angle) * radius * scale, centerY + Math.sin(angle) * radius * scale];
  };
  context.clearRect(0, 0, width, height);
  context.strokeStyle = '#dfe3e8';
  context.lineWidth = 1.2;
  for (let ring = 1; ring <= 5; ring += 1) {
    context.beginPath();
    dimensions.forEach((_dimension, index) => {
      const [x, y] = point(index, ring / 5);
      if (index === 0) context.moveTo(x, y); else context.lineTo(x, y);
    });
    context.closePath();
    context.stroke();
  }
  dimensions.forEach((dimension, index) => {
    const [x, y] = point(index);
    context.beginPath(); context.moveTo(centerX, centerY); context.lineTo(x, y); context.stroke();
    const [labelX, labelY] = point(index, 1.2);
    context.fillStyle = Number.isFinite(dimension.score) ? '#536173' : '#a6adb6';
    context.font = '600 19px ui-sans-serif, system-ui, sans-serif';
    context.textAlign = Math.abs(labelX - centerX) < 12 ? 'center' : labelX > centerX ? 'left' : 'right';
    context.textBaseline = 'middle';
    context.fillText(`${dimension.label}${Number.isFinite(dimension.score) ? '' : ' —'}`, labelX, labelY);
  });

  const available = dimensions.filter((dimension) => Number.isFinite(dimension.score));
  if (available.length === count) {
    context.beginPath();
    dimensions.forEach((dimension, index) => {
      const [x, y] = point(index, clamp(dimension.score / 10, 0, 1));
      if (index === 0) context.moveTo(x, y); else context.lineTo(x, y);
    });
    context.closePath();
    context.fillStyle = 'rgba(53,110,234,.14)';
    context.strokeStyle = '#356eea';
    context.lineWidth = 4;
    context.fill(); context.stroke();
  } else {
    context.strokeStyle = '#356eea';
    context.lineWidth = 4;
    for (let index = 0; index < count; index += 1) {
      const next = (index + 1) % count;
      if (!Number.isFinite(dimensions[index].score) || !Number.isFinite(dimensions[next].score)) continue;
      const [x1, y1] = point(index, clamp(dimensions[index].score / 10, 0, 1));
      const [x2, y2] = point(next, clamp(dimensions[next].score / 10, 0, 1));
      context.beginPath(); context.moveTo(x1, y1); context.lineTo(x2, y2); context.stroke();
    }
  }
  dimensions.forEach((dimension, index) => {
    if (!Number.isFinite(dimension.score)) return;
    const [x, y] = point(index, clamp(dimension.score / 10, 0, 1));
    context.beginPath(); context.arc(x, y, 6, 0, Math.PI * 2);
    context.fillStyle = '#356eea'; context.fill();
  });
  $('#holisticRadarHint').textContent = available.length
    ? `已覆盖 ${available.length} / ${count} 个维度；灰色“—”为本场证据不足。`
    : '本场没有足够证据绘制能力曲线，未使用默认分。';
}

function renderHolisticDimensions(report) {
  const container = $('#holisticDimensionList');
  container.replaceChildren();
  report.radar.forEach((dimension) => {
    const item = createElement('article', `holistic-dimension${dimension.scorable ? '' : ' is-unscored'}`);
    const header = createElement('header');
    header.append(
      createElement('strong', '', dimension.label),
      createElement('span', '', Number.isFinite(dimension.score) ? dimension.score.toFixed(1) : '—'),
    );
    item.append(header, createElement('p', '', dimension.feedback));
    if (dimension.evidence.length) item.append(createElement('small', '', `依据：${dimension.evidence.slice(0, 2).join('；')}`));
    container.append(item);
  });
}

function renderCompanyInsights(report) {
  const insights = report.companyInsights;
  $('#companyInsightsTitle').textContent = `${companyLabel(report.company)}面经综合建议`;
  $('#companyInsightsSummary').textContent = insights.summary || '本次报告未返回足够的公开面经综合信息。';
  const patternList = $('#companyPatternList');
  patternList.replaceChildren();
  (insights.patterns.length ? insights.patterns : ['暂无可从公开面经交叉验证的共性。'])
    .forEach((pattern) => patternList.append(createElement('li', '', pattern)));
  const adviceList = $('#companyAdviceList');
  adviceList.replaceChildren();
  (insights.advice.length ? insights.advice : ['暂无基于可核验面经归纳出的针对性建议。'])
    .forEach((advice) => adviceList.append(createElement('li', '', advice)));
  const citationList = $('#companyCitationList');
  citationList.replaceChildren();
  insights.citations.forEach((citation) => {
    const href = safeResourceLink(citation);
    if (!href) return;
    const link = createElement('a', 'company-citation');
    link.href = href;
    link.target = '_blank';
    link.rel = 'noopener noreferrer';
    const copy = createElement('span');
    copy.append(
      createElement('strong', '', citation.title),
      createElement('small', '', [citation.platform, citation.date, citation.round, citation.excerpt].filter(Boolean).join(' · ') || '打开原帖核验'),
    );
    link.append(copy, createElement('i', '', '↗'));
    citationList.append(link);
  });
  $('#companyCitationEmpty').classList.toggle('is-hidden', citationList.children.length > 0);
}

function renderCurrent(report) {
  currentReport = report;
  const interviewTypeLabel = report.interviewType === 'technical_hr' ? '技术 / 综合面' : '技术面';
  $('#reportTitle').textContent = `${companyLabel(report.company)} · ${report.specialization} · ${interviewTypeLabel}报告`;
  const pressureLabels = ['无压力', '温和压力', '标准压力', '高压'];
  const durationLabel = report.unlimited ? '不限时 · 手动结束' : (report.duration ? `${report.duration} 分钟` : '');
  const memoryLabel = report.scored
    ? (report.memoryEnabled ? '参与弱项记忆' : '未参与弱项记忆')
    : '数据不足 · 不写入弱项记忆';
  const languageLabel = report.languageMode === 'zh'
    ? '全程中文'
    : report.languageMode === 'en' ? 'Pure English' : '中英双语';
  const tags = [formatDate(report.endedAt), interviewTypeLabel, durationLabel, pressureLabels[report.stressLevel], languageLabel, memoryLabel].filter(Boolean);
  $('#reportMeta').textContent = tags.join(' · ');
  $('#reportSummary').textContent = report.summary;
  const hasOverallScore = report.scored && report.scoreCoverage > 0 && Number.isFinite(report.overall);
  $('#overallScore').textContent = hasOverallScore ? report.overall.toFixed(1) : '—';
  $('#overallScoreUnit').textContent = hasOverallScore ? '/ 10' : '数据不足';
  $('#scoreCoverage').textContent = report.scored ? `评分覆盖率 ${report.scoreCoverage}%` : '评分覆盖率 0%';
  $('#scoreDisc').classList.toggle('is-unscored', !hasOverallScore);
  $('.button-label', $('#retryWeakButton')).textContent = report.scored ? '用原简历复练弱项' : '用原简历重新开始一场';
  const retryQuestions = $('#retryQuestionsButton');
  const hasReviewQuestions = report.questions.some((question) => Number.isFinite(question.score) && question.score <= 6);
  retryQuestions.classList.toggle('is-hidden', !hasReviewQuestions || !report.id);
  if (hasReviewQuestions && report.id) {
    retryQuestions.href = `/practice?review=${encodeURIComponent(report.id)}`;
  }
  drawHolisticRadar(report);
  renderHolisticDimensions(report);
  renderResumeAnalysis(report);
  renderProcessAnalysis(report);
  renderRoleFit(report);
  renderRubric(report);
  renderQuestions(report);
  renderPractice(report);
  renderHintUsage(report);
  renderCompanyInsights(report);
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
  const scoreLabel = report.scored && Number.isFinite(report.overall) ? `${report.overall.toFixed(1)} 分` : '数据不足';
  return `${formatDate(report.endedAt)} · ${companyLabel(report.company)} · ${scoreLabel}`;
}

function populateComparisonSelects() {
  const currentSelect = $('#currentCompare');
  const previousSelect = $('#previousCompare');
  const selectedCurrent = currentSelect.value;
  const selectedPrevious = previousSelect.value;
  const comparableReports = historyReports.filter((report) => report.scored);
  currentSelect.replaceChildren();
  previousSelect.replaceChildren();
  comparableReports.forEach((report) => {
    const optionA = createElement('option', '', historyLabel(report));
    optionA.value = report.id;
    currentSelect.append(optionA);
    const optionB = optionA.cloneNode(true);
    previousSelect.append(optionB);
  });
  if (!comparableReports.length) {
    const emptyCurrent = createElement('option', '', '暂无可评分场次');
    emptyCurrent.value = '';
    currentSelect.append(emptyCurrent);
  }
  if (comparableReports.length <= 1) {
    const emptyOption = createElement('option', '', '暂无其他可评分场次');
    emptyOption.value = '';
    previousSelect.prepend(emptyOption);
  }
  currentSelect.value = comparableReports.some((item) => item.id === selectedCurrent) ? selectedCurrent : comparableReports[0]?.id || '';
  const defaultPrevious = comparableReports.find((item) => item.id !== currentSelect.value)?.id || '';
  previousSelect.value = comparableReports.some((item) => item.id === selectedPrevious && item.id !== currentSelect.value) ? selectedPrevious : defaultPrevious;
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
    if (!report?.scored || !report.rubric.every((dimension) => Number.isFinite(dimension.score))) return;
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
  if (!current?.scored && !previous?.scored) {
    context.fillStyle = '#8b949f';
    context.font = '600 20px ui-sans-serif, system-ui, sans-serif';
    context.textAlign = 'center';
    context.fillText('有效回答不足，暂不绘制分数', centerX, centerY);
  }
}

function renderScoreComparison(current, previous) {
  const container = $('#scoreComparison');
  container.replaceChildren();
  current.rubric.forEach((dimension, index) => {
    const item = createElement('div', 'comparison-item');
    item.append(createElement('span', '', dimension.label));
    const hasCurrent = current.scored && Number.isFinite(dimension.score);
    const hasPrevious = previous?.scored && Number.isFinite(previous.rubric[index]?.score);
    const value = createElement('strong', '', hasCurrent ? dimension.score.toFixed(1) : '—');
    if (!hasCurrent) value.append(createElement('em', '', '数据不足'));
    else if (hasPrevious) {
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
  if (!current.scored) {
    container.append(createElement('div', 'topic-empty', '本场有效回答不足，不生成知识点分数或变化曲线。'));
    return;
  }
  if (!previous?.scored) {
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
  const comparableReports = historyReports.filter((report) => report.scored);
  if (!comparableReports.length) {
    drawRadar(null, null);
    const scoreContainer = $('#scoreComparison');
    scoreContainer.replaceChildren(createElement('div', 'topic-empty', '完成至少一场有效面试后，这里会显示能力对比。'));
    const topicContainer = $('#topicDeltas');
    topicContainer.replaceChildren(createElement('div', 'topic-empty', '未评分场次不会进入成长曲线或知识点变化。'));
    return;
  }
  const current = comparableReports.find((item) => item.id === $('#currentCompare').value) || comparableReports[0];
  const previous = comparableReports.find((item) => item.id === $('#previousCompare').value && item.id !== current.id) || null;
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
    const hasScore = report.scored && Number.isFinite(report.overall);
    const score = createElement('span', `history-score${hasScore ? '' : ' is-unscored'}`, hasScore ? report.overall.toFixed(1) : '—');
    const copy = createElement('div', 'history-copy');
    const durationLabel = report.unlimited ? '不限时' : (report.duration ? `${report.duration} 分钟` : '');
    copy.append(
      createElement('strong', '', `${companyLabel(report.company)} · ${report.specialization} · ${report.interviewType === 'technical_hr' ? '技术 / 综合面' : '技术面'}`),
      createElement('small', '', `${formatDate(report.endedAt)}${durationLabel ? ` · ${durationLabel}` : ''}${hasScore ? '' : ' · 数据不足'}`),
    );
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
      scored: currentReport.scored,
      score_status: currentReport.scoreStatus,
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
          interview_type: currentReport.interviewType,
          specialization: currentReport.specialization,
          language_mode: currentReport.languageMode,
          stress: currentReport.stress,
          stress_level: currentReport.stressLevel,
          duration_minutes: currentReport.unlimited ? null : currentReport.duration,
          ended_at: currentReport.endedAt,
          memory_enabled: currentReport.memoryEnabled,
          hint_events: currentReport.hintEvents,
          scored: currentReport.scored,
          score_status: currentReport.scoreStatus,
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
    setButtonBusy(button, true, currentReport.scored ? '正在创建弱项复练…' : '正在重新创建面试…');
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
      interview_type: session?.interview_type || currentReport.interviewType || 'technical',
      specialization: session?.specialization || currentReport.specialization,
      memory_enabled: true,
      created_at: session?.created_at || new Date().toISOString(),
    });
    showToast(
      currentReport.scored ? '已复用原简历，并把本场弱项加入新剧本。' : '已复用原简历，开始一场新的完整练习。',
      'success',
    );
    window.location.assign(`/interview?session=${encodeURIComponent(id)}`);
  } catch (error) {
    setButtonBusy(button, false);
    showToast(error?.message || '创建弱项复练失败。', 'error', 5200);
  }
}

$$('.report-tab').forEach((tab) => tab.addEventListener('click', () => showView(tab.dataset.view)));
$('#currentCompare').addEventListener('change', () => {
  if ($('#previousCompare').value === $('#currentCompare').value) {
    $('#previousCompare').value = historyReports.find((item) => item.scored && item.id !== $('#currentCompare').value)?.id || '';
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
  const scoreLabel = currentReport.scored && Number.isFinite(currentReport.overall)
    ? `${currentReport.overall.toFixed(1)} 分`
    : '数据不足';
  const text = `${companyLabel(currentReport.company)}后端一面：${scoreLabel}\n${currentReport.summary}`;
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

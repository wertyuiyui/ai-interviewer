const STORAGE = {
  clientId: 'mock_interview.client_id.v1',
  currentSession: 'mock_interview.current_session.v1',
  setup: 'mock_interview.setup.v1',
  reports: 'mock_interview.report_cache.v2',
  legacyReports: 'mock_interview.report_cache.v1',
};

export const COMPANY_LABELS = Object.freeze({
  bytedance: '字节跳动',
  meituan: '美团',
  tencent: '腾讯',
});

export const MODE_LABELS = Object.freeze({
  L0: 'L0 · 端到端语音',
  L1: 'L1 · 百炼语音管道',
  L2: 'L2 · 免费语音兜底',
  L3: 'L3 · 纯文字模式',
});

let fallbackClientId = '';

export class ApiError extends Error {
  constructor(message, status = 0, payload = null) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
    this.payload = payload;
  }
}

export function $(selector, root = document) {
  return root.querySelector(selector);
}

export function $$(selector, root = document) {
  return [...root.querySelectorAll(selector)];
}

export function clamp(value, minimum, maximum) {
  return Math.min(maximum, Math.max(minimum, Number(value) || 0));
}

export function normalizeMode(value, fallback = 'L3') {
  const mode = String(value || '').trim().toUpperCase();
  return Object.hasOwn(MODE_LABELS, mode) ? mode : fallback;
}

export function modeLabel(mode) {
  return MODE_LABELS[normalizeMode(mode)] || MODE_LABELS.L3;
}

export function companyLabel(company) {
  return COMPANY_LABELS[String(company || '').toLowerCase()] || String(company || '未知公司');
}

function uuid() {
  if (globalThis.crypto?.randomUUID) return globalThis.crypto.randomUUID();
  const random = Math.random().toString(16).slice(2);
  return `client-${Date.now().toString(36)}-${random}`;
}

function readStorage(key, fallback = null) {
  try {
    const value = localStorage.getItem(key);
    return value === null ? fallback : JSON.parse(value);
  } catch {
    return fallback;
  }
}

function writeStorage(key, value) {
  try {
    localStorage.setItem(key, JSON.stringify(value));
    return true;
  } catch {
    return false;
  }
}

export function getClientId() {
  if (fallbackClientId) return fallbackClientId;
  try {
    const existing = localStorage.getItem(STORAGE.clientId);
    if (existing) {
      fallbackClientId = existing;
      return existing;
    }
    fallbackClientId = uuid();
    localStorage.setItem(STORAGE.clientId, fallbackClientId);
    return fallbackClientId;
  } catch {
    fallbackClientId = uuid();
    return fallbackClientId;
  }
}

export function getCurrentSession() {
  return readStorage(STORAGE.currentSession, null);
}

export function setCurrentSession(session) {
  return writeStorage(STORAGE.currentSession, session);
}

export function getSavedSetup() {
  return readStorage(STORAGE.setup, null);
}

export function saveSetup(setup) {
  return writeStorage(STORAGE.setup, setup);
}

export function getCachedReports() {
  // v1 could contain pre-fix demo reports whose uncovered dimensions were
  // serialized as neutral-looking 5.0 values. They are intentionally not
  // migrated: retaining them would make an offline fallback look scored.
  try { localStorage.removeItem(STORAGE.legacyReports); } catch { /* no storage */ }
  const reports = readStorage(STORAGE.reports, []);
  return Array.isArray(reports) ? reports : [];
}

export function cacheReport(report, sessionId = '') {
  if (!report || typeof report !== 'object') return false;
  const id = String(report.id || report.session_id || sessionId || '');
  if (!id) return false;
  const record = {
    ...report,
    id,
    session_id: report.session_id || id,
    _cache_schema: 2,
    _cached_at: new Date().toISOString(),
  };
  const reports = getCachedReports().filter((item) => String(item.id || item.session_id) !== id);
  reports.unshift(record);
  return writeStorage(STORAGE.reports, reports.slice(0, 20));
}

export function removeCachedReport(id) {
  const target = String(id || '');
  return writeStorage(STORAGE.reports, getCachedReports().filter((item) => String(item.id || item.session_id) !== target));
}

export function clearCachedReports() {
  return writeStorage(STORAGE.reports, []);
}

export async function apiFetch(url, options = {}) {
  const { json, timeout = 30_000, headers: customHeaders = {}, ...fetchOptions } = options;
  const controller = new AbortController();
  const timer = timeout > 0 ? setTimeout(() => controller.abort(), timeout) : null;
  const headers = new Headers(customHeaders);
  let body = fetchOptions.body;

  if (json !== undefined) {
    headers.set('Content-Type', 'application/json');
    body = JSON.stringify(json);
  }

  try {
    const response = await fetch(url, {
      credentials: 'same-origin',
      ...fetchOptions,
      headers,
      body,
      signal: fetchOptions.signal || controller.signal,
    });
    const contentType = response.headers.get('content-type') || '';
    let payload = null;
    if (response.status !== 204) {
      payload = contentType.includes('json')
        ? await response.json().catch(() => null)
        : await response.text().catch(() => '');
    }
    if (!response.ok) {
      const message = payload?.detail?.message || payload?.error?.message || payload?.detail || payload?.error || payload?.message || `请求失败（${response.status}）`;
      throw new ApiError(typeof message === 'string' ? message : JSON.stringify(message), response.status, payload);
    }
    return payload;
  } catch (error) {
    if (error?.name === 'AbortError') throw new ApiError('请求超时，请检查网络后重试。', 0, null);
    if (error instanceof ApiError) throw error;
    throw new ApiError(error?.message || '网络连接失败，请稍后重试。', 0, null);
  } finally {
    if (timer) clearTimeout(timer);
  }
}

export function setButtonBusy(button, busy, label = '') {
  if (!button) return;
  if (!button.dataset.originalLabel) {
    button.dataset.originalLabel = $('.button-label', button)?.textContent || button.textContent.trim();
  }
  button.disabled = Boolean(busy);
  button.classList.toggle('is-busy', Boolean(busy));
  const labelNode = $('.button-label', button);
  const text = busy && label ? label : button.dataset.originalLabel;
  if (labelNode) labelNode.textContent = text;
  else button.textContent = text;
}

export function showToast(message, type = 'info', duration = 3600) {
  const region = $('#toastRegion');
  if (!region || !message) return;
  const toast = document.createElement('div');
  toast.className = `toast${type === 'error' ? ' is-error' : type === 'success' ? ' is-success' : ''}`;
  toast.setAttribute('role', type === 'error' ? 'alert' : 'status');
  toast.textContent = String(message);
  region.append(toast);
  const remove = () => {
    toast.classList.add('is-leaving');
    setTimeout(() => toast.remove(), 220);
  };
  setTimeout(remove, duration);
}

export function formatSeconds(totalSeconds) {
  const value = Math.max(0, Math.ceil(Number(totalSeconds) || 0));
  const minutes = Math.floor(value / 60);
  const seconds = value % 60;
  return `${String(minutes).padStart(2, '0')}:${String(seconds).padStart(2, '0')}`;
}

export function formatDate(value, withTime = true) {
  if (!value) return '时间未知';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value);
  return new Intl.DateTimeFormat('zh-CN', {
    month: '2-digit',
    day: '2-digit',
    ...(withTime ? { hour: '2-digit', minute: '2-digit', hour12: false } : {}),
  }).format(date).replaceAll('/', '.');
}

export function score10(value, fallback = 0) {
  let score = typeof value === 'object' && value !== null ? value.score : value;
  score = Number(score);
  if (!Number.isFinite(score)) return fallback;
  if (score > 10 && score <= 100) score /= 10;
  return Math.round(clamp(score, 0, 10) * 10) / 10;
}

export function toArray(value) {
  if (Array.isArray(value)) return value;
  if (value === null || value === undefined || value === '') return [];
  return [value];
}

export function firstValue(object, keys, fallback = undefined) {
  if (!object || typeof object !== 'object') return fallback;
  for (const key of keys) {
    if (object[key] !== undefined && object[key] !== null) return object[key];
  }
  return fallback;
}

export function normalizeHistoryPayload(payload) {
  if (Array.isArray(payload)) return payload;
  return firstValue(payload, ['items', 'history', 'reports', 'sessions', 'data'], []) || [];
}

export function base64ToArrayBuffer(value) {
  const input = String(value || '').replace(/^data:[^,]+,/, '').replace(/\s/g, '');
  if (!input) return new ArrayBuffer(0);
  const binary = atob(input);
  const bytes = new Uint8Array(binary.length);
  for (let index = 0; index < binary.length; index += 1) bytes[index] = binary.charCodeAt(index);
  return bytes.buffer;
}

export function debounce(callback, wait = 160) {
  let timer = 0;
  return (...args) => {
    clearTimeout(timer);
    timer = setTimeout(() => callback(...args), wait);
  };
}

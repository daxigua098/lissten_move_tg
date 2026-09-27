import axios from "axios";

import { auth } from "./stores/auth";

export const http = axios.create({
  baseURL: "/",
  timeout: 20000,
});

// 真正要访问 Telegram 的动作（加群 / 探测 / 导入 / 在线补搜）每次都要新建连接、
// 解析实体，实测加盟一次约 30 秒——按默认 20 秒会在客户端先超时，
// 而服务端其实已经成功了，用户看到的却是报错。
export const TELEGRAM_TIMEOUT = 180000;

http.interceptors.request.use((config) => {
  if (auth.token) {
    config.headers.Authorization = `Bearer ${auth.token}`;
  }
  return config;
});

http.interceptors.response.use(
  (response) => response,
  (error) => {
    const status = error.response?.status;
    const code = error.response?.data?.code;
    const detail = error.response?.data?.detail || error.message || "请求失败";

    if (status === 401) {
      auth.clear();
      if (!window.location.pathname.startsWith("/login")) {
        window.location.replace("/login");
      }
    } else if (status === 403 && code === "AUTH_PASSWORD_CHANGE_REQUIRED") {
      auth.update({ must_change_password: true });
      if (window.location.pathname !== "/change-password") {
        window.location.replace("/change-password");
      }
    } else if (status === 403 && code === "TENANT_INACTIVE") {
      // P4：账号到期 / 停用后后端会拦下写操作，前端立刻切只读态并弹出横幅
      auth.update({
        tenant_status: error.response?.data?.tenant_status || "expired",
      });
    }

    const normalized = new Error(detail);
    normalized.status = status;
    normalized.code = code;
    return Promise.reject(normalized);
  },
);

export const authApi = {
  login: (username, password) => http.post("/api/auth/login", { username, password }),
  check: () => http.get("/api/auth/check"),
  logout: () => http.post("/api/auth/logout"),
  logoutAll: () => http.post("/api/auth/logout-all"),
  changePassword: (currentPassword, newPassword) =>
    http.patch("/api/auth/password", {
      current_password: currentPassword,
      new_password: newPassword,
    }),
};

// 代理工作台（P3）：代理与平台账号共用，会员账号访问会 403
export const agentApi = {
  quota: () => http.get("/api/agent/quota"),
  stats: () => http.get("/api/agent/stats"),
  expiring: (params) => http.get("/api/agent/expiring", { params }),
  templates: () => http.get("/api/agent/templates"),
  subordinates: () => http.get("/api/agent/subordinates"),
  ledger: (params) => http.get("/api/agent/ledger", { params }),
  openMember: (payload) => http.post("/api/agent/members", payload),
  openTrial: (payload) => http.post("/api/agent/trials", payload),
  openAgent: (payload) => http.post("/api/agent/agents", payload),
  allocate: (payload) => http.post("/api/agent/allocate", payload),
  reclaim: (payload) => http.post("/api/agent/reclaim", payload),
  adjust: (id, payload) => http.post(`/api/agent/${id}/adjust`, payload),
  renew: (id, payload) => http.post(`/api/agent/${id}/renew`, payload),
  upgrade: (id, payload) => http.post(`/api/agent/${id}/upgrade`, payload),
  setEnabled: (id, enabled) => http.post(`/api/agent/${id}/enable`, { enabled }),
};

// 平台后台（P5）：只有平台账号可访问，代理与会员一律 403
export const platformApi = {
  overview: (params) => http.get("/api/platform/overview", { params }),
  agents: (params) => http.get("/api/platform/agents", { params }),
  agentTree: (id) => http.get(`/api/platform/agents/${id}/tree`),
  members: (params) => http.get("/api/platform/members", { params }),
  openMember: (payload) => http.post("/api/platform/members", payload),
  renewMember: (id, payload) => http.post(`/api/platform/members/${id}/renew`, payload),
  setPlan: (id, payload) => http.post(`/api/platform/members/${id}/plan`, payload),
  setEnabled: (id, payload) => http.post(`/api/platform/accounts/${id}/enable`, payload),
  adjust: (id, payload) => http.post(`/api/platform/accounts/${id}/adjust`, payload),
  updateAccount: (id, payload) => http.patch(`/api/platform/accounts/${id}`, payload),
  removeAccount: (id) => http.delete(`/api/platform/accounts/${id}`),
  expiry: (params) => http.get("/api/platform/expiry", { params }),
  ledger: (params) => http.get("/api/platform/ledger", { params }),
  ledgerExportUrl: (params) => {
    const query = new URLSearchParams(
      Object.entries(params || {}).filter(
        ([, value]) => value !== undefined && value !== null && value !== "",
      ),
    );
    return `/api/platform/ledger.csv?${query.toString()}`;
  },
};

export const usersApi = {
  list: (params) => http.get("/api/users", { params }),
  create: (payload) => http.post("/api/users", payload),
  update: (id, payload) => http.patch(`/api/users/${id}`, payload),
  remove: (id) => http.delete(`/api/users/${id}`),
  revokeSessions: (id) => http.post(`/api/users/${id}/revoke-sessions`),
};

export const logsApi = {
  audit: (params) => http.get("/api/audit", { params }),
  loginHistory: (params) => http.get("/api/login-history", { params }),
  status: () => http.get("/api/system/status"),
};

export const accountsApi = {
  list: (params) => http.get("/api/accounts", { params }),
  create: (payload) => http.post("/api/accounts", payload),
  update: (id, payload) => http.patch(`/api/accounts/${id}`, payload),
  remove: (id) => http.delete(`/api/accounts/${id}`),
  loginStart: (id, forceSms = false) =>
    http.post(
      `/api/accounts/${id}/login/start`,
      { force_sms: forceSms },
      { timeout: TELEGRAM_TIMEOUT },
    ),
  loginVerify: (id, code) =>
    http.post(`/api/accounts/${id}/login/verify`, { code }, { timeout: TELEGRAM_TIMEOUT }),
  loginPassword: (id, password) =>
    http.post(`/api/accounts/${id}/login/password`, { password }, { timeout: TELEGRAM_TIMEOUT }),
  loginCancel: (id) => http.post(`/api/accounts/${id}/login/cancel`, null, { timeout: 5000 }),
  loginStatus: (id) => http.get(`/api/accounts/${id}/login/status`),
  refreshCredentials: (id) => http.post(`/api/accounts/${id}/credentials/refresh`),
  importAccounts: (payload) => http.post("/api/accounts/import", payload),
  codeUrl: (id) => http.get(`/api/accounts/${id}/code-url`),
  fetchLoginCode: (id, payload) =>
    http.post(`/api/accounts/${id}/code/fetch`, payload, { timeout: 320000 }),
  autoLogin: (accountIds) => http.post("/api/accounts/auto-login", { account_ids: accountIds }),
  autoLoginStatus: () => http.get("/api/accounts/auto-login/status"),
  autoLoginStop: (accountIds) =>
    http.post("/api/accounts/auto-login/stop", { account_ids: accountIds }),
};

export const uploadApi = {
  image: (file) => {
    const form = new FormData();
    form.append("file", file);
    return http.post("/api/uploads/image", form, { timeout: TELEGRAM_TIMEOUT });
  },
  video: (file) => {
    const form = new FormData();
    form.append("file", file);
    return http.post("/api/uploads/video", form, { timeout: TELEGRAM_TIMEOUT });
  },
};

export const outreachApi = {
  settings: () => http.get("/api/outreach/settings"),
  updateSettings: (payload) => http.patch("/api/outreach/settings", payload),
  capacity: () => http.get("/api/outreach/capacity"),
  records: (params) => http.get("/api/outreach/records", { params }),
  contactMessages: (id, params = {}) =>
    http.get(`/api/outreach/contacts/${id}/messages`, { params }),
  recordsExportUrl: (params) => {
    const query = new URLSearchParams();
    Object.entries(params || {}).forEach(([key, value]) => {
      if (value !== undefined && value !== null && value !== "") query.append(key, value);
    });
    return `/api/outreach/records.csv?${query.toString()}`;
  },
  previewQueue: (params = {}) => http.get("/api/outreach/queue/preview", { params }),
  planQueue: (params = {}) => http.post("/api/outreach/queue/plan", null, { params }),
  clearQueue: () => http.post("/api/outreach/queue/clear"),
  deleteTasks: (taskIds) => http.post("/api/outreach/tasks/delete", { task_ids: taskIds }),
  taskDetail: (id) => http.get(`/api/outreach/tasks/${id}/detail`),
  confirmDelivery: (id, delivered) =>
    http.post(`/api/outreach/tasks/${id}/confirm-delivery`, { delivered }),
  retryTask: (id) => http.post(`/api/outreach/tasks/${id}/retry`),
  tasks: (params) => http.get("/api/outreach/tasks", { params }),
  contacts: (params) => http.get("/api/outreach/contacts", { params }),
  templates: (params) => http.get("/api/outreach/templates", { params }),
  createTemplate: (payload) => http.post("/api/outreach/templates", payload),
  updateTemplate: (id, payload) => http.patch(`/api/outreach/templates/${id}`, payload),
  removeTemplate: (id) => http.delete(`/api/outreach/templates/${id}`),
  adoptTemplate: (id) => http.post(`/api/outreach/templates/${id}/adopt`),
  runtimeStatus: () => http.get("/api/outreach/runtime/status"),
  pauseRuntime: () => http.post("/api/outreach/runtime/pause"),
  resumeRuntime: () => http.post("/api/outreach/runtime/resume"),
  dispatch: (limit = 1) => http.post("/api/outreach/queue/dispatch", null, { params: { limit } }),
  takeover: (id) => http.post(`/api/outreach/contacts/${id}/takeover`),
  resumeAuto: (id) => http.post(`/api/outreach/contacts/${id}/resume-auto`),
  handoff: (id) => http.post(`/api/outreach/contacts/${id}/handoff`),
  batchRetire: (payload) => http.post("/api/outreach/accounts/batch-retire", payload),
  setParticipation: (accountIds, enabled) =>
    http.post("/api/outreach/accounts/participation", {
      account_ids: accountIds,
      enabled,
    }),
  suppress: (id) => http.post(`/api/outreach/contacts/${id}/suppress`),
  unsuppress: (id) => http.delete(`/api/outreach/contacts/${id}/suppress`),
};

export const botsApi = {
  list: (params) => http.get("/api/bots", { params }),
  create: (payload) => http.post("/api/bots", payload),
  update: (id, payload) => http.patch(`/api/bots/${id}`, payload),
  remove: (id) => http.delete(`/api/bots/${id}`),
};

export const sourcesApi = {
  list: (params) => http.get("/api/sources", { params }),
  available: (params) => http.get("/api/sources/available", { params }),
  tags: () => http.get("/api/sources/tags"),
  sync: () => http.post("/api/sources/sync"),
  add: (payload) => http.post("/api/sources", payload),
  update: (id, payload) => http.patch(`/api/sources/${id}`, payload),
  remove: (id) => http.delete(`/api/sources/${id}`),
  batchTags: (payload) => http.post("/api/sources/tags/batch", payload),
};

export const targetsApi = {
  list: (params) => http.get("/api/targets", { params }),
  available: (params) => http.get("/api/targets/available", { params }),
  sync: () => http.post("/api/targets/sync"),
  add: (payload) => http.post("/api/targets", payload),
  update: (id, payload) => http.patch(`/api/targets/${id}`, payload),
  remove: (id) => http.delete(`/api/targets/${id}`),
  batchTags: (payload) => http.post("/api/targets/tags/batch", payload),
};

export const routesApi = {
  list: (params) => http.get("/api/routes", { params }),
  detail: (id) => http.get(`/api/routes/${id}`),
  create: (payload) => http.post("/api/routes", payload),
  matrix: (payload) => http.post("/api/routes/matrix", payload),
  update: (id, payload) => http.patch(`/api/routes/${id}`, payload),
  remove: (id) => http.delete(`/api/routes/${id}`),
  addTargets: (id, chatIds) => http.post(`/api/routes/${id}/targets`, { chat_ids: chatIds }),
  setTargetEnabled: (id, chatId, enabled) =>
    http.patch(`/api/routes/${id}/targets/${chatId}`, { enabled }),
  removeTarget: (id, chatId) => http.delete(`/api/routes/${id}/targets/${chatId}`),
  resetProgress: (id, chatId, confirm) =>
    http.post(`/api/routes/${id}/targets/${chatId}/reset`, { confirm }),
};

export const adAssetsApi = {
  list: (params) => http.get("/api/ad-assets", { params }),
  detail: (id) => http.get(`/api/ad-assets/${id}`),
  create: (payload) => http.post("/api/ad-assets", payload),
  update: (id, payload) => http.patch(`/api/ad-assets/${id}`, payload),
  remove: (id, force) => http.delete(`/api/ad-assets/${id}`, { params: { force } }),
};

export const uploadsApi = {
  image: (formData) =>
    http.post("/api/uploads/image", formData, {
      headers: { "Content-Type": "multipart/form-data" },
    }),
  remove: (filename) => http.delete(`/api/uploads/image/${filename}`),
};

export const jobsApi = {
  list: (params) => http.get("/api/jobs", { params }),
  stats: () => http.get("/api/jobs/stats"),
  retryFailed: () => http.post("/api/jobs/retry-failed"),
  retryOne: (id) => http.post(`/api/jobs/${id}/retry`),
  skipOne: (id) => http.post(`/api/jobs/${id}/skip`),
};

export const runtimeApi = {
  status: () => http.get("/api/runtime/status"),
  start: () => http.post("/api/runtime/start"),
  // 一键启动：打开租户运行开关 + 把所有线路恢复启用（P4-06）
  startAll: () => http.post("/api/runtime/start-all"),
  restart: () => http.post("/api/runtime/restart"),
  pause: () => http.post("/api/runtime/pause"),
  resume: () => http.post("/api/runtime/resume"),
  stop: () => http.post("/api/runtime/stop"),
};

export const keywordsApi = {
  list: (kind) => http.get("/api/keyword-groups", { params: kind ? { kind } : {} }),
  seed: () => http.post("/api/keyword-groups/seed"),
  create: (payload) => http.post("/api/keyword-groups", payload),
  update: (id, payload) => http.patch(`/api/keyword-groups/${id}`, payload),
  remove: (id) => http.delete(`/api/keyword-groups/${id}`),
  addKeyword: (groupId, payload) => http.post(`/api/keyword-groups/${groupId}/keywords`, payload),
  updateKeyword: (id, payload) => http.patch(`/api/keywords/${id}`, payload),
  removeKeyword: (id) => http.delete(`/api/keywords/${id}`),
  match: (payload) => http.post("/api/keyword-groups/match", payload),
};

export const leadsApi = {
  list: (params) => http.get("/api/leads", { params }),
  stats: () => http.get("/api/leads/stats"),
  purgeDelivered: () => http.post("/api/leads/purge-delivered"),
  purgeAll: () => http.post("/api/leads/purge-all"),
  exportUrl: (params) => {
    const query = new URLSearchParams(
      Object.entries(params || {}).filter(([, value]) => value !== undefined && value !== null && value !== ""),
    );
    return `/api/leads/export.csv?${query.toString()}`;
  },
};

export const hotKeywordsApi = {
  list: (params) => http.get("/api/hot-keywords", { params }),
  stats: () => http.get("/api/hot-keywords/stats"),
  promote: (payload) => http.post("/api/hot-keywords/promote", payload),
};

export const resourcesApi = {
  list: (params) => http.get("/api/resources", { params }),
  overview: () => http.get("/api/resources/overview"),
  facets: () => http.get("/api/resources/facets"),
  counts: () => http.get("/api/resources/counts"),
  detail: (id) => http.get(`/api/resources/${id}`),
  discoverOnline: (payload) =>
    http.post("/api/resources/discover-online", payload, { timeout: TELEGRAM_TIMEOUT }),
  import: (payload) => http.post("/api/resources/import", payload, { timeout: TELEGRAM_TIMEOUT }),
  refresh: (payload) =>
    http.post("/api/resources/refresh", payload, { timeout: TELEGRAM_TIMEOUT }),
  refreshOne: (id, params) =>
    http.post(`/api/resources/${id}/refresh`, null, { params, timeout: TELEGRAM_TIMEOUT }),
  update: (id, payload) => http.patch(`/api/resources/${id}`, payload),
  adopt: (id, payload) => http.post(`/api/resources/${id}/adopt`, payload),
  // 加盟是同步执行的：实测一次约 30 秒，必须给它更长的超时
  join: (payload) => http.post("/api/resources/join", payload, { timeout: TELEGRAM_TIMEOUT }),
  directorySources: () => http.get("/api/resources/directory/sources"),
  exportUrl: (params) => {
    const query = new URLSearchParams();
    Object.entries(params || {}).forEach(([key, value]) => {
      if (value === undefined || value === null || value === "") return;
      if (Array.isArray(value)) {
        value.forEach((item) => query.append(key, item));
      } else {
        query.append(key, value);
      }
    });
    return `/api/resources/export.csv?${query.toString()}`;
  },
};

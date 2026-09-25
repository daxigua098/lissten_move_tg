import axios from "axios";

import { auth } from "./stores/auth";

export const http = axios.create({
  baseURL: "/",
  timeout: 20000,
});

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
    http.post(`/api/accounts/${id}/login/start`, { force_sms: forceSms }),
  loginVerify: (id, code) => http.post(`/api/accounts/${id}/login/verify`, { code }),
  loginPassword: (id, password) =>
    http.post(`/api/accounts/${id}/login/password`, { password }),
  loginCancel: (id) => http.post(`/api/accounts/${id}/login/cancel`),
  loginStatus: (id) => http.get(`/api/accounts/${id}/login/status`),
  refreshCredentials: (id) => http.post(`/api/accounts/${id}/credentials/refresh`),
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
  restart: () => http.post("/api/runtime/restart"),
  pause: () => http.post("/api/runtime/pause"),
  resume: () => http.post("/api/runtime/resume"),
  stop: () => http.post("/api/runtime/stop"),
};

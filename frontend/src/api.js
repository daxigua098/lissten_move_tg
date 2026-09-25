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
};

export const botsApi = {
  list: (params) => http.get("/api/bots", { params }),
  create: (payload) => http.post("/api/bots", payload),
  update: (id, payload) => http.patch(`/api/bots/${id}`, payload),
  remove: (id) => http.delete(`/api/bots/${id}`),
};

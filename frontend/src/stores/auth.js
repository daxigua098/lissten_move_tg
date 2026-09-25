import { reactive } from "vue";

const STORAGE_KEY = "tg-lead-auth";

function loadStored() {
  try {
    return JSON.parse(sessionStorage.getItem(STORAGE_KEY) || "null") || {};
  } catch {
    return {};
  }
}

const stored = loadStored();

export const auth = reactive({
  token: stored.token || "",
  username: stored.username || "",
  role: stored.role || "",
  mustChange: Boolean(stored.mustChange),
  isBuiltin: Boolean(stored.isBuiltin),

  get isAuthenticated() {
    return Boolean(this.token);
  },

  get isSuperAdmin() {
    return this.role === "super_admin";
  },

  set(payload) {
    this.token = payload.token || "";
    this.username = payload.username || "";
    this.role = payload.role || "";
    this.mustChange = Boolean(payload.must_change_password);
    this.isBuiltin = Boolean(payload.is_builtin);
    this.persist();
  },

  update(payload) {
    if (payload.username !== undefined) this.username = payload.username;
    if (payload.role !== undefined) this.role = payload.role;
    if (payload.must_change_password !== undefined) {
      this.mustChange = Boolean(payload.must_change_password);
    }
    if (payload.is_builtin !== undefined) this.isBuiltin = Boolean(payload.is_builtin);
    this.persist();
  },

  persist() {
    sessionStorage.setItem(
      STORAGE_KEY,
      JSON.stringify({
        token: this.token,
        username: this.username,
        role: this.role,
        mustChange: this.mustChange,
        isBuiltin: this.isBuiltin,
      }),
    );
  },

  clear() {
    this.token = "";
    this.username = "";
    this.role = "";
    this.mustChange = false;
    this.isBuiltin = false;
    sessionStorage.removeItem(STORAGE_KEY);
  },
});

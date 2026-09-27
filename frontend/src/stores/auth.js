import { reactive } from "vue";

const STORAGE_KEY = "tg-lead-auth";

// 与后端 ROLE_RANK 对齐：只有平台账号内部还看角色等级
export const ROLE_RANK = { viewer: 1, sub_admin: 2, super_admin: 3 };

// 功能块显示名，菜单与页面内提示共用
export const MODULE_LABELS = {
  carry: "搬运帖子",
  monitor: "监听会员",
  discovery: "资源发现",
};

const ROLE_LABELS = {
  super_admin: "超级管理员",
  sub_admin: "子管理员",
  viewer: "只读",
  owner: "会员",
};

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
  // P2 身份契约扩展字段；老会话里没有时按平台账号处理，保持原有行为
  accountType: stored.accountType || "platform",
  tenantId: stored.tenantId ?? null,
  tenantStatus: stored.tenantStatus || "active",
  expiresAt: stored.expiresAt || null,
  modules: Array.isArray(stored.modules) ? stored.modules : [],
  limits: stored.limits && typeof stored.limits === "object" ? stored.limits : {},

  get isAuthenticated() {
    return Boolean(this.token);
  },

  get isPlatform() {
    return this.accountType === "platform";
  },

  get isAgent() {
    return this.accountType === "agent";
  },

  get isMember() {
    return this.accountType === "member";
  },

  get roleRank() {
    return ROLE_RANK[this.role] || 0;
  },

  get isSuperAdmin() {
    return this.isPlatform && this.role === "super_admin";
  },

  /** 到期 / 停用后全站进只读态（P4 落地，P2 恒为 active） */
  get isReadOnly() {
    return this.tenantStatus !== "active";
  },

  get roleLabel() {
    if (this.isAgent) return "代理";
    if (this.isMember) return "会员";
    return ROLE_LABELS[this.role] || ROLE_LABELS.viewer;
  },

  /** 会员按功能块放行；平台账号不受功能块限制 */
  hasModule(code) {
    return this.isPlatform || this.modules.includes(code);
  },

  hasAnyModule(codes) {
    return this.isPlatform || codes.some((code) => this.modules.includes(code));
  },

  set(payload) {
    this.token = payload.token || "";
    this.apply(payload);
    this.persist();
  },

  update(payload) {
    this.apply(payload);
    this.persist();
  },

  apply(payload) {
    if (payload.username !== undefined) this.username = payload.username || "";
    if (payload.role !== undefined) this.role = payload.role || "";
    if (payload.must_change_password !== undefined) {
      this.mustChange = Boolean(payload.must_change_password);
    }
    if (payload.is_builtin !== undefined) this.isBuiltin = Boolean(payload.is_builtin);
    if (payload.account_type !== undefined) this.accountType = payload.account_type || "platform";
    if (payload.tenant_id !== undefined) this.tenantId = payload.tenant_id ?? null;
    if (payload.tenant_status !== undefined) this.tenantStatus = payload.tenant_status || "active";
    if (payload.expires_at !== undefined) this.expiresAt = payload.expires_at || null;
    if (payload.modules !== undefined) this.modules = Array.isArray(payload.modules) ? payload.modules : [];
    if (payload.limits !== undefined) {
      this.limits = payload.limits && typeof payload.limits === "object" ? payload.limits : {};
    }
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
        accountType: this.accountType,
        tenantId: this.tenantId,
        tenantStatus: this.tenantStatus,
        expiresAt: this.expiresAt,
        modules: this.modules,
        limits: this.limits,
      }),
    );
  },

  clear() {
    this.token = "";
    this.username = "";
    this.role = "";
    this.mustChange = false;
    this.isBuiltin = false;
    this.accountType = "platform";
    this.tenantId = null;
    this.tenantStatus = "active";
    this.expiresAt = null;
    this.modules = [];
    this.limits = {};
    sessionStorage.removeItem(STORAGE_KEY);
  },
});
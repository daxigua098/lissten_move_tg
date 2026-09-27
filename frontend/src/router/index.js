import { createRouter, createWebHistory } from "vue-router";

import { auth, ROLE_RANK } from "../stores/auth";

const AgentConsole = () => import("../views/AgentConsole.vue");
const ChangePassword = () => import("../views/ChangePassword.vue");
const ConfigTabs = () => import("../views/ConfigTabs.vue");
const LibraryTabs = () => import("../views/LibraryTabs.vue");
const Leads = () => import("../views/Leads.vue");
const Login = () => import("../views/Login.vue");
const OpsTabs = () => import("../views/OpsTabs.vue");
const OutreachTabs = () => import("../views/OutreachTabs.vue");
const OverviewTabs = () => import("../views/OverviewTabs.vue");
const PlatformConsole = () => import("../views/PlatformConsole.vue");
const ResourceTabs = () => import("../views/ResourceTabs.vue");
const SystemTabs = () => import("../views/SystemTabs.vue");

// 路由 meta 约定（三层一致的前端那两层：菜单 + 路由）：
//   role        平台账号内部角色下限
//   modules     会员账号需要的功能块（命中任意一个即可）
//   platformOnly 平台账号专属（会员一律跳回运行总览）
//   accountType 指定账号类型专属（代理工作台）
const routes = [
  { path: "/login", name: "login", component: Login, meta: { public: true } },
  { path: "/change-password", name: "change-password", component: ChangePassword },
  { path: "/", name: "dashboard", component: OverviewTabs },
  { path: "/agent", name: "agent", component: AgentConsole, meta: { accountType: "agent" } },
  // 平台后台：只有平台超管能进，会员与代理一律打回自己的首页
  {
    path: "/platform",
    name: "platform",
    component: PlatformConsole,
    meta: { role: "super_admin", platformOnly: true },
  },
  // 合并后的入口；子页面用 ?tab= 定位
  {
    path: "/config",
    name: "config",
    component: ConfigTabs,
    meta: { role: "sub_admin", modules: ["carry", "monitor"] },
  },
  {
    path: "/library",
    name: "library",
    component: LibraryTabs,
    meta: { role: "sub_admin", modules: ["monitor"] },
  },
  {
    path: "/leads",
    name: "leads",
    component: Leads,
    meta: { role: "sub_admin", modules: ["monitor"] },
  },
  {
    path: "/resources",
    name: "resources",
    component: ResourceTabs,
    meta: { role: "sub_admin", modules: ["discovery"] },
  },
  // 「账号与机器人」「冷触达」是基础能力，平台（超管）与会员都能进，代理不能
  { path: "/ops", name: "ops", component: OpsTabs, meta: { role: "super_admin" } },
  {
    path: "/outreach",
    name: "outreach",
    component: OutreachTabs,
    meta: { role: "sub_admin", modules: ["outreach"] },
  },
  {
    path: "/system",
    name: "system",
    component: SystemTabs,
    meta: { role: "sub_admin", platformOnly: true },
  },
  // 旧地址保留重定向，收藏夹与脚本不受影响
  { path: "/sources", redirect: { path: "/config", query: { tab: "sources" } } },
  { path: "/targets", redirect: { path: "/config", query: { tab: "targets" } } },
  { path: "/routes", redirect: { path: "/config", query: { tab: "routes" } } },
  { path: "/ad-assets", redirect: { path: "/config", query: { tab: "ads" } } },
  {
    path: "/keywords",
    name: "keywords",
    redirect: { path: "/library", query: { tab: "keywords" } },
  },
  { path: "/hot-keywords", redirect: { path: "/library", query: { tab: "hot" } } },
  {
    path: "/resource-discovery",
    redirect: { path: "/resources", query: { tab: "library" } },
  },
  { path: "/jobs", redirect: { path: "/", query: { tab: "jobs" } } },
  { path: "/accounts", redirect: { path: "/ops", query: { tab: "accounts" } } },
  { path: "/bots", redirect: { path: "/ops", query: { tab: "bots" } } },
  { path: "/audit", redirect: { path: "/system", query: { tab: "audit" } } },
  { path: "/users", redirect: { path: "/system", query: { tab: "users" } } },
  {
    path: "/login-history",
    redirect: { path: "/system", query: { tab: "login-history" } },
  },
  { path: "/:pathMatch(.*)*", redirect: "/" },
];

const router = createRouter({
  history: createWebHistory(),
  routes,
});

/** 登录后按账号类型落到各自的首页。 */
export function homeRoute() {
  return auth.isAgent ? { name: "agent" } : { name: "dashboard" };
}

function canAccess(meta) {
  // 代理账号：只有代理工作台，业务页面一律打回
  if (auth.isAgent) {
    return meta.accountType === "agent";
  }
  // 会员账号：平台专属页面不给，功能块命中才放行
  if (auth.isMember) {
    if (meta.platformOnly || meta.accountType === "agent") return false;
    if (meta.modules && !auth.hasAnyModule(meta.modules)) return false;
    return true;
  }
  // 平台账号：沿用内部角色分级
  if (meta.accountType === "agent") return false;
  if (meta.role && auth.roleRank < (ROLE_RANK[meta.role] || 0)) return false;
  return true;
}

router.beforeEach((to) => {
  if (to.meta.public) {
    return auth.isAuthenticated && to.name === "login" ? homeRoute() : true;
  }
  if (!auth.isAuthenticated) {
    return { name: "login" };
  }
  // 改密页对所有账号类型（含代理）都放行。
  // 代理账号的 canAccess 只认代理工作台，若不在这里提前返回，
  // 「代理 + 首登必须改密」会变成 change-password ⇄ agent 的无限重定向，
  // 表现就是点了登录没有任何反应。
  if (to.name === "change-password") {
    return true;
  }
  if (auth.mustChange) {
    return { name: "change-password" };
  }
  if (!canAccess(to.meta)) {
    return homeRoute();
  }
  return true;
});

export default router;
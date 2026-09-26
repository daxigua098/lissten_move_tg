import { createRouter, createWebHistory } from "vue-router";

import { auth } from "../stores/auth";
import ChangePassword from "../views/ChangePassword.vue";
import ConfigTabs from "../views/ConfigTabs.vue";
import LibraryTabs from "../views/LibraryTabs.vue";
import Leads from "../views/Leads.vue";
import Login from "../views/Login.vue";
import OpsTabs from "../views/OpsTabs.vue";
import OverviewTabs from "../views/OverviewTabs.vue";
import SystemTabs from "../views/SystemTabs.vue";

const ROLE_RANK = { viewer: 1, sub_admin: 2, super_admin: 3 };

const routes = [
  { path: "/login", name: "login", component: Login, meta: { public: true } },
  { path: "/change-password", name: "change-password", component: ChangePassword },
  // 合并后的 6 个入口；子页面用 ?tab= 定位
  { path: "/", name: "dashboard", component: OverviewTabs },
  { path: "/config", name: "config", component: ConfigTabs, meta: { role: "sub_admin" } },
  { path: "/library", name: "library", component: LibraryTabs, meta: { role: "sub_admin" } },
  { path: "/leads", name: "leads", component: Leads, meta: { role: "sub_admin" } },
  { path: "/ops", name: "ops", component: OpsTabs, meta: { role: "super_admin" } },
  { path: "/system", name: "system", component: SystemTabs, meta: { role: "sub_admin" } },
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

router.beforeEach((to) => {
  if (to.meta.public) {
    return auth.isAuthenticated && to.name === "login" ? { name: "dashboard" } : true;
  }
  if (!auth.isAuthenticated) {
    return { name: "login" };
  }
  if (auth.mustChange && to.name !== "change-password") {
    return { name: "change-password" };
  }
  if (to.meta.role && (ROLE_RANK[auth.role] || 0) < ROLE_RANK[to.meta.role]) {
    return { name: "dashboard" };
  }
  return true;
});

export default router;

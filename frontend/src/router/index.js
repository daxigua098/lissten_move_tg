import { createRouter, createWebHistory } from "vue-router";

import { auth } from "../stores/auth";
import Accounts from "../views/Accounts.vue";
import AdAssets from "../views/AdAssets.vue";
import Audit from "../views/Audit.vue";
import Bots from "../views/Bots.vue";
import ChangePassword from "../views/ChangePassword.vue";
import Dashboard from "../views/Dashboard.vue";
import HotKeywords from "../views/HotKeywords.vue";
import Jobs from "../views/Jobs.vue";
import Keywords from "../views/Keywords.vue";
import Leads from "../views/Leads.vue";
import Login from "../views/Login.vue";
import LoginHistory from "../views/LoginHistory.vue";
import Routes from "../views/Routes.vue";
import Sources from "../views/Sources.vue";
import Targets from "../views/Targets.vue";
import Users from "../views/Users.vue";

const ROLE_RANK = { viewer: 1, sub_admin: 2, super_admin: 3 };

const routes = [
  { path: "/login", name: "login", component: Login, meta: { public: true } },
  { path: "/change-password", name: "change-password", component: ChangePassword },
  { path: "/", name: "dashboard", component: Dashboard },
  { path: "/users", name: "users", component: Users, meta: { role: "super_admin" } },
  { path: "/accounts", name: "accounts", component: Accounts, meta: { role: "super_admin" } },
  { path: "/bots", name: "bots", component: Bots, meta: { role: "super_admin" } },
  { path: "/sources", name: "sources", component: Sources, meta: { role: "sub_admin" } },
  { path: "/targets", name: "targets", component: Targets, meta: { role: "sub_admin" } },
  { path: "/routes", name: "routes", component: Routes, meta: { role: "sub_admin" } },
  { path: "/ad-assets", name: "ad-assets", component: AdAssets, meta: { role: "sub_admin" } },
  { path: "/keywords", name: "keywords", component: Keywords, meta: { role: "sub_admin" } },
  { path: "/hot-keywords", name: "hot-keywords", component: HotKeywords, meta: { role: "sub_admin" } },
  { path: "/leads", name: "leads", component: Leads, meta: { role: "sub_admin" } },
  { path: "/jobs", name: "jobs", component: Jobs },
  { path: "/audit", name: "audit", component: Audit },
  { path: "/login-history", name: "login-history", component: LoginHistory, meta: { role: "super_admin" } },
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

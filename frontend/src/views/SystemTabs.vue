<script setup>
import TabbedViews from "../components/TabbedViews.vue";
import Audit from "./Audit.vue";
import LoginHistory from "./LoginHistory.vue";
import Users from "./Users.vue";

const tabs = [
  { name: "audit", label: "审计日志", component: Audit, role: "sub_admin" },
  { name: "users", label: "账户管理", component: Users, role: "super_admin" },
  { name: "login-history", label: "登录历史", component: LoginHistory, role: "super_admin" },
];
</script>

<template>
  <TabbedViews :tabs="tabs" />
</template>

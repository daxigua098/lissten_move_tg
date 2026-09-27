<script setup>
import TabbedViews from "../components/TabbedViews.vue";
import Accounts from "./Accounts.vue";
import Bots from "./Bots.vue";
import OutreachAccounts from "./OutreachAccounts.vue";

const tabs = [
  { name: "accounts", label: "执行账号池（采集/监听）", component: Accounts, role: "super_admin" },
  { name: "outreach-accounts", label: "发信息账号", component: OutreachAccounts, role: "super_admin" },
  { name: "bots", label: "控制 Bot", component: Bots, role: "super_admin" },
];
</script>

<template>
  <TabbedViews :tabs="tabs" />
</template>

<script setup>
import TabbedViews from "../components/TabbedViews.vue";
import Accounts from "./Accounts.vue";
import Bots from "./Bots.vue";

const tabs = [
  { name: "accounts", label: "执行账号池", component: Accounts, role: "super_admin" },
  { name: "bots", label: "控制 Bot", component: Bots, role: "super_admin" },
];
</script>

<template>
  <TabbedViews :tabs="tabs" />
</template>

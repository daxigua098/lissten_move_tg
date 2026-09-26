<script setup>
import TabbedViews from "../components/TabbedViews.vue";
import AdAssets from "./AdAssets.vue";
import Routes from "./Routes.vue";
import Sources from "./Sources.vue";
import Targets from "./Targets.vue";

const tabs = [
  { name: "sources", label: "监听源", component: Sources, role: "sub_admin" },
  { name: "targets", label: "接收组", component: Targets, role: "sub_admin" },
  { name: "routes", label: "线路管理", component: Routes, role: "sub_admin" },
  { name: "ads", label: "广告素材", component: AdAssets, role: "sub_admin" },
];
</script>

<template>
  <TabbedViews :tabs="tabs" />
</template>

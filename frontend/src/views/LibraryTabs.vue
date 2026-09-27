<script setup>
import TabbedViews from "../components/TabbedViews.vue";
import HotKeywords from "./HotKeywords.vue";
import Keywords from "./Keywords.vue";

const tabs = [
  { name: "keywords", label: "关键词 / 排除词 / 归并", component: Keywords, role: "sub_admin", module: "monitor" },
  { name: "hot", label: "热门关键词", component: HotKeywords, role: "sub_admin", module: "monitor" },
];
</script>

<template>
  <TabbedViews :tabs="tabs" />
</template>

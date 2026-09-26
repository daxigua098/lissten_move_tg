<script setup>
import TabbedViews from "../components/TabbedViews.vue";
import DirectorySync from "./DirectorySync.vue";
import ResourceTasks from "./ResourceTasks.vue";
import Resources from "./Resources.vue";

const tabs = [
  { name: "library", label: "资源库", component: Resources, role: "sub_admin" },
  { name: "directory", label: "目录同步", component: DirectorySync, role: "sub_admin" },
  { name: "tasks", label: "采集任务", component: ResourceTasks, role: "sub_admin" },
];
</script>

<template>
  <TabbedViews :tabs="tabs" />
</template>

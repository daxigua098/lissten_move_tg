<script setup>
import TabbedViews from "../components/TabbedViews.vue";
import Dashboard from "./Dashboard.vue";
import Jobs from "./Jobs.vue";

const tabs = [
  { name: "overview", label: "运行总览", component: Dashboard },
  { name: "jobs", label: "投递任务", component: Jobs },
];
</script>

<template>
  <TabbedViews :tabs="tabs" />
</template>

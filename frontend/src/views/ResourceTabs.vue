<script setup>
import TabbedViews from "../components/TabbedViews.vue";
import Resources from "./Resources.vue";

const tabs = [
  { name: "library", label: "资源库", component: Resources, role: "sub_admin", module: "discovery" },
];
</script>

<template>
  <TabbedViews :tabs="tabs" />
</template>

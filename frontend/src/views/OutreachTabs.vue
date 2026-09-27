<script setup>
import TabbedViews from "../components/TabbedViews.vue";
import Contacts from "./outreach/Contacts.vue";
import Queue from "./outreach/Queue.vue";
import Settings from "./outreach/Settings.vue";
import Templates from "./outreach/Templates.vue";

const tabs = [
  { name: "queue", label: "队列", component: Queue, role: "sub_admin" },
  { name: "contacts", label: "联系人", component: Contacts, role: "sub_admin" },
  { name: "templates", label: "话术模板", component: Templates, role: "sub_admin" },
  { name: "settings", label: "策略", component: Settings, role: "sub_admin" },
];
</script>

<template>
  <TabbedViews :tabs="tabs" />
</template>

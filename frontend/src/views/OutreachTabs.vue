<script setup>
import TabbedViews from "../components/TabbedViews.vue";
import Contacts from "./outreach/Contacts.vue";
import OutreachRecords from "./outreach/OutreachRecords.vue";
import Queue from "./outreach/Queue.vue";
import SendAccounts from "./outreach/SendAccounts.vue";
import Settings from "./outreach/Settings.vue";
import Templates from "./outreach/Templates.vue";

const tabs = [
  { name: "queue", label: "队列", component: Queue, role: "sub_admin" },
  { name: "send-accounts", label: "发送账号", component: SendAccounts, role: "sub_admin" },
  { name: "contacts", label: "联系人", component: Contacts, role: "sub_admin" },
  { name: "records", label: "发送记录", component: OutreachRecords, role: "sub_admin" },
  { name: "templates", label: "话术模板", component: Templates, role: "sub_admin" },
  { name: "settings", label: "策略", component: Settings, role: "sub_admin" },
];
</script>

<template>
  <TabbedViews :tabs="tabs" />
</template>

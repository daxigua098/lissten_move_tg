<script setup>
import { computed, ref, watch } from "vue";
import { useRoute, useRouter } from "vue-router";

import { auth, ROLE_RANK } from "../stores/auth";

const props = defineProps({
  // [{ name, label, component, role? }]
  tabs: { type: Array, required: true },
});

const route = useRoute();
const router = useRouter();

/** 标签可见性：会员看功能块，平台账号看角色，代理在这里没有可看的业务标签。 */
function tabVisible(item) {
  if (auth.isMember) {
    return !item.module || auth.hasModule(item.module);
  }
  if (item.role) {
    return auth.roleRank >= (ROLE_RANK[item.role] || 0);
  }
  return true;
}

const visibleTabs = computed(() => props.tabs.filter(tabVisible));

function currentFromQuery() {
  const wanted = String(route.query.tab || "");
  const found = visibleTabs.value.find((item) => item.name === wanted);
  return found ? found.name : visibleTabs.value[0]?.name || "";
}

const active = ref(currentFromQuery());

watch(
  () => route.query.tab,
  () => {
    const next = currentFromQuery();
    if (next && next !== active.value) active.value = next;
  },
);

watch(visibleTabs, () => {
  if (!visibleTabs.value.some((item) => item.name === active.value)) {
    active.value = visibleTabs.value[0]?.name || "";
  }
});

function onChange(value) {
  router.replace({ query: { ...route.query, tab: value } });
}
</script>

<template>
  <div class="tabbed">
    <el-tabs v-model="active" class="tabbed-bar" @tab-change="onChange">
      <el-tab-pane
        v-for="item in visibleTabs"
        :key="item.name"
        :label="item.label"
        :name="item.name"
        lazy
      >
        <component :is="item.component" />
      </el-tab-pane>
    </el-tabs>
  </div>
</template>

<style scoped>
/* 合并页面后，子页面自己的大标题与侧栏菜单重复，这里统一隐藏 */
.tabbed :deep(.page-title) {
  display: none;
}

.tabbed-bar :deep(.el-tabs__header) {
  margin-bottom: 12px;
}

.tabbed-bar :deep(.el-tabs__item) {
  font-size: 14px;
}
</style>

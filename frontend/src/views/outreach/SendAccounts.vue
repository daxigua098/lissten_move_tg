<script setup>
import { ElMessage } from "element-plus";
import { nextTick, onMounted, onUnmounted, ref } from "vue";

import { accountsApi, outreachApi } from "../../api";

const loading = ref(false);
const rows = ref([]);
const tableRef = ref(null);
const selectedIds = ref(new Set());
const lastSync = ref("");
let timer = null;

/** 勾选 = 参与冷触达（READY / 冷却中 / 额度用尽都算参与） */
function participating(row) {
  return ["NEW", "READY", "COOLING", "CAPPED"].includes(row.outreach?.state);
}

function blocked(row) {
  return ["LIMITED", "DISABLED"].includes(row.outreach?.state);
}

async function load() {
  loading.value = true;
  try {
    const { data } = await accountsApi.list({ purpose: "outreach", limit: 200 });
    // 只同步"已经登录"的发信息账号
    rows.value = data.items.filter((row) => row.status === "active");
    selectedIds.value = new Set(rows.value.filter(participating).map((row) => row.id));
    lastSync.value = new Date().toLocaleTimeString("zh-CN");
    await nextTick();
    syncSelection();
  } catch (error) {
    ElMessage.error(error.message);
  } finally {
    loading.value = false;
  }
}

function syncSelection() {
  for (const row of rows.value) {
    tableRef.value?.toggleRowSelection(row, selectedIds.value.has(row.id));
  }
}

async function apply(ids, enabled) {
  if (!ids.length) return;
  try {
    const { data } = await outreachApi.setParticipation(ids, enabled);
    if (data.skipped?.length) {
      ElMessage.warning(data.skipped.map((item) => item.reason).join("；"));
    } else {
      ElMessage.success(enabled ? "已加入冷触达发送池" : "已移出冷触达发送池");
    }
  } catch (error) {
    ElMessage.error(error.message);
  } finally {
    await load();
  }
}

async function onSelect(selection, row) {
  const enabled = selection.includes(row);
  if (enabled) {
    selectedIds.value.add(row.id);
  } else {
    selectedIds.value.delete(row.id);
  }
  await apply([row.id], enabled);
}

async function onSelectAll(selection) {
  const enabled = selection.length > 0;
  const targets = (enabled ? selection : rows.value).map((row) => row.id);
  if (!targets.length) return;
  selectedIds.value = new Set(enabled ? targets : []);
  await apply(targets, enabled);
}

function fmt(value) {
  return value ? new Date(value).toLocaleString("zh-CN") : "-";
}

function percent(value) {
  return value === null || value === undefined ? "-" : `${Math.round(value * 100)}%`;
}

onMounted(() => {
  load();
  timer = setInterval(load, 15000); // 实时同步：登录成功的账号会自动出现
});

onUnmounted(() => {
  if (timer) clearInterval(timer);
});
</script>

<template>
  <div>
    <div class="toolbar">
      <h2 class="page-title">发送账号</h2>
      <span class="card-hint">
        已登录 {{ rows.length }} 个 · 已勾选 {{ selectedIds.size }} 个 · 上次同步 {{ lastSync || "-" }}
      </span>
      <div class="spacer" />
      <el-button size="small" @click="load">立即同步</el-button>
    </div>

    <el-alert
      class="panel"
      type="warning"
      :closable="false"
      show-icon
      title="只有勾选的账号才会发送冷触达"
      description="勾选=参与（运营态置为可用），取消=暂停。冷触达策略对这些账号一律生效：每次首触冷却、每日额度与账号档位、跨账号只联系一次、熔断开关、账号被限制时自动停发并冻结会话。"
    />

    <el-table
      ref="tableRef"
      v-loading="loading"
      :data="rows"
      size="small"
      border
      class="panel"
      row-key="id"
      @select="onSelect"
      @select-all="onSelectAll"
    >
      <el-table-column type="selection" width="42" :selectable="(row) => !blocked(row)" />
      <el-table-column prop="id" label="ID" width="60" />
      <el-table-column prop="name" label="别名" min-width="130" />
      <el-table-column prop="phone_masked" label="手机号" width="120" />
      <el-table-column label="档位" width="130">
        <template #default="{ row }">{{ row.outreach?.tier_label || "-" }}</template>
      </el-table-column>
      <el-table-column label="运营态" width="120">
        <template #default="{ row }">
          <el-tag size="small" :type="blocked(row) ? 'danger' : 'success'">
            {{ row.outreach?.state_label || "-" }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column label="今日额度" width="100">
        <template #default="{ row }">
          {{ row.outreach?.today_sent ?? 0 }} / {{ row.outreach?.daily_cap ?? "-" }}
        </template>
      </el-table-column>
      <el-table-column label="冷却至" width="160">
        <template #default="{ row }">{{ fmt(row.outreach?.cooldown_until) }}</template>
      </el-table-column>
      <el-table-column label="7日成功/回复" width="140">
        <template #default="{ row }">
          {{ percent(row.outreach?.success_rate_7d) }} / {{ percent(row.outreach?.reply_rate_7d) }}
        </template>
      </el-table-column>
      <el-table-column label="在跟会话" width="100">
        <template #default="{ row }">{{ row.outreach?.active_conversation_count ?? 0 }}</template>
      </el-table-column>
    </el-table>

    <el-alert
      class="panel"
      type="info"
      :closable="false"
      show-icon
      title="实时同步"
      description="列表每 15 秒自动刷新，只显示状态为「正常」的发信息账号；新登录成功的账号会自动出现，当前在冷却或额度用完的账号仍算参与，只是会等到可用时再发。"
    />
  </div>
</template>

<style scoped>
.panel {
  margin-top: 12px;
}
</style>

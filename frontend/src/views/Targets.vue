<script setup>
import { ElMessage, ElMessageBox } from "element-plus";
import { computed, onMounted, ref } from "vue";

import { targetsApi } from "../api";

const loading = ref(false);
const available = ref([]);
const targets = ref([]);
const selectedAvailable = ref([]);
const selectedTargets = ref([]);
const role = ref("content");
const checkAccess = ref(true);
const keyword = ref("");

const filteredAvailable = computed(() => {
  const text = keyword.value.trim();
  if (!text) return available.value;
  return available.value.filter(
    (item) => (item.title || "").includes(text) || (item.username || "").includes(text),
  );
});

async function load() {
  loading.value = true;
  try {
    const [pool, current] = await Promise.all([
      targetsApi.available({ limit: 300 }),
      targetsApi.list({ limit: 300 }),
    ]);
    available.value = pool.data.items;
    targets.value = current.data.items;
    selectedAvailable.value = [];
    selectedTargets.value = [];
  } catch (error) {
    ElMessage.error(error.message);
  } finally {
    loading.value = false;
  }
}

async function addSelected() {
  if (!selectedAvailable.value.length) {
    ElMessage.warning("请先在左侧勾选群组");
    return;
  }
  try {
    const { data } = await targetsApi.add({
      chat_ids: selectedAvailable.value,
      role: role.value,
      check_access: checkAccess.value,
    });
    if (data.added?.length) {
      ElMessage.success(`已加入 ${data.added.length} 个接收组`);
    }
    for (const failure of data.failures || []) {
      ElMessage.warning(`${failure.input}：${failure.reason}`);
    }
    load();
  } catch (error) {
    ElMessage.error(error.message);
  }
}

async function removeSelected() {
  if (!selectedTargets.value.length) {
    ElMessage.warning("请先在右侧勾选要移出的接收组");
    return;
  }
  try {
    for (const chatId of selectedTargets.value) {
      await targetsApi.remove(chatId);
    }
    ElMessage.success(`已移出 ${selectedTargets.value.length} 个接收组`);
    load();
  } catch (error) {
    ElMessage.error(error.message);
  }
}

async function updateTarget(row, payload) {
  try {
    await targetsApi.update(row.id, payload);
  } catch (error) {
    ElMessage.error(error.message);
    load();
  }
}

async function renameChat(row) {
  try {
    const { value } = await ElMessageBox.prompt(
      "起一个好认的名字，列表里会优先显示它（留空则恢复显示原标题）",
      "给群/频道起备注名",
      {
        inputValue: row.display_name || "",
        inputPlaceholder: row.title || row.username || "",
        confirmButtonText: "保存",
        cancelButtonText: "取消",
      },
    );
    await targetsApi.update(row.id, { display_name: value ?? "" });
    ElMessage.success("备注名已更新");
    load();
  } catch (error) {
    if (error?.message && !error.message.includes("cancel")) {
      ElMessage.error(error.message);
    }
  }
}

function canPostLabel(value) {
  if (value === true) return { text: "可发帖", type: "success" };
  if (value === false) return { text: "无发帖权限", type: "danger" };
  return { text: "未检测", type: "info" };
}

onMounted(load);
</script>

<template>
  <div v-loading="loading">
    <div class="toolbar">
      <h2 class="page-title">接收组</h2>
      <span class="card-hint">已加入 {{ targets.length }} 个 · 可选 {{ available.length }} 个</span>
      <div class="spacer" />
      <el-select v-model="role" size="small" style="width: 150px">
        <el-option label="用途：内容接收" value="content" />
        <el-option label="用途：线索接收" value="lead" />
      </el-select>
      <el-checkbox v-model="checkAccess" size="small">入库前做权限预检</el-checkbox>
      <el-button size="small" @click="load">刷新</el-button>
    </div>

    <el-card shadow="never">
      <div class="transfer">
        <div class="panel">
          <div class="panel-head">
            <span>可选群组 / 频道</span>
            <el-input v-model="keyword" size="small" placeholder="搜索" style="width: 140px" />
          </div>
          <div class="panel-body">
            <el-checkbox-group v-model="selectedAvailable" class="list">
              <label v-for="item in filteredAvailable" :key="item.id" class="row">
                <el-checkbox :value="item.id" />
                <span class="grow">
                  <span class="chat-name">{{ item.name || item.title || item.username }}</span>
                  <span class="card-hint">
                    {{ item.chat_type_label }}
                    <template v-if="item.username"> · @{{ item.username }}</template>
                  </span>
                </span>
              </label>
              <div v-if="!filteredAvailable.length" class="card-hint empty">
                没有可选项。先在「监听源」页同步群组池，或直接输入链接添加。
              </div>
            </el-checkbox-group>
          </div>
        </div>

        <div class="arrows">
          <el-button size="small" @click="addSelected">&rarr;</el-button>
          <el-button size="small" @click="removeSelected">&larr;</el-button>
        </div>

        <div class="panel">
          <div class="panel-head">
            <span>已是接收组</span>
            <span class="card-hint">{{ selectedTargets.length }} 项已勾选</span>
          </div>
          <div class="panel-body">
            <el-checkbox-group v-model="selectedTargets" class="list">
              <label v-for="item in targets" :key="item.id" class="row">
                <el-checkbox :value="item.id" />
                <span class="grow">
                  <span class="chat-name">{{ item.name || item.title || item.username }}</span>
                  <span class="card-hint">
                    {{ item.chat_type_label }}
                    <template v-if="item.is_private"> · 私有</template>
                  </span>
                </span>
                <el-tag size="small" :type="canPostLabel(item.can_post).type">
                  {{ canPostLabel(item.can_post).text }}
                </el-tag>
                <el-select
                  :model-value="item.target_role"
                  size="small"
                  style="width: 120px"
                  @change="(value) => updateTarget(item, { role: value })"
                >
                  <el-option label="内容接收" value="content" />
                  <el-option label="线索接收" value="lead" />
                </el-select>
                <el-switch
                  v-model="item.target_enabled"
                  size="small"
                  @change="() => updateTarget(item, { enabled: item.target_enabled })"
                />
                <el-button size="small" link @click.prevent="renameChat(item)">改名</el-button>
              </label>
              <div v-if="!targets.length" class="card-hint empty">
                还没有接收组，从左侧勾选后点 → 添加。
              </div>
            </el-checkbox-group>
          </div>
        </div>
      </div>
    </el-card>

    <el-alert
      class="panel-gap"
      type="warning"
      :closable="false"
      show-icon
      title="接收组必须能发帖"
      description="执行账号需要已加入该群/频道并有发言权限；权限预检会标记「无发帖权限」的目标，A 线投递前还会再校验一次。用途选「线索接收」的群接收 B 线的会员线索卡片。"
    />
  </div>
</template>

<style scoped>
.transfer {
  display: grid;
  grid-template-columns: minmax(0, 1fr) 52px minmax(0, 1fr);
  gap: 10px;
}

.panel {
  border: 1px solid var(--tg-border);
  border-radius: 10px;
  display: flex;
  flex-direction: column;
  background: #fbfcfe;
}

.panel-head {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  padding: 8px 10px;
  border-bottom: 1px solid var(--tg-border);
  font-size: 13px;
  color: #64748b;
  justify-content: space-between;
}

.panel-body {
  padding: 8px 10px;
  max-height: 340px;
  overflow-y: auto;
}

.list {
  display: flex;
  flex-direction: column;
  gap: 4px;
  width: 100%;
}

.row {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 6px 8px;
  background: #fff;
  border: 1px solid var(--tg-border);
  border-radius: 8px;
}

.grow {
  flex: 1;
  min-width: 0;
}

.grow .card-hint {
  display: block;
  margin-top: 2px;
}

.empty {
  padding: 16px 4px;
}

.arrows {
  display: flex;
  flex-direction: column;
  justify-content: center;
  gap: 8px;
}

.panel-gap {
  margin-top: 12px;
}

@media (max-width: 980px) {
  .transfer {
    grid-template-columns: minmax(0, 1fr);
  }

  .arrows {
    flex-direction: row;
  }
}
</style>

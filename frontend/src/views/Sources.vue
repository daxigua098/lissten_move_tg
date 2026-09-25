<script setup>
import { ElMessage, ElMessageBox } from "element-plus";
import { computed, onMounted, reactive, ref } from "vue";

import { sourcesApi } from "../api";

const loading = ref(false);
const syncing = ref(false);
const available = ref([]);
const sources = ref([]);
const selectedAvailable = ref([]);
const selectedSources = ref([]);
const manual = reactive({ input: "", join: false, tags: "" });
const keyword = ref("");

const filteredAvailable = computed(() => {
  const text = keyword.value.trim();
  if (!text) return available.value;
  return available.value.filter(
    (item) => (item.title || "").includes(text) || (item.username || "").includes(text),
  );
});

function parseTags(text) {
  return String(text || "")
    .split(/[\s,，、]+/)
    .filter(Boolean);
}

async function load() {
  loading.value = true;
  try {
    const [pool, current] = await Promise.all([
      sourcesApi.available({ limit: 300 }),
      sourcesApi.list({ limit: 300 }),
    ]);
    available.value = pool.data.items;
    sources.value = current.data.items;
    selectedAvailable.value = [];
    selectedSources.value = [];
  } catch (error) {
    ElMessage.error(error.message);
  } finally {
    loading.value = false;
  }
}

async function syncDialogs() {
  syncing.value = true;
  try {
    const { data } = await sourcesApi.sync();
    ElMessage.success(
      `已从 ${data.account} 同步：新增 ${data.created} 个、更新 ${data.updated} 个`,
    );
    load();
  } catch (error) {
    ElMessage.error(error.message);
  } finally {
    syncing.value = false;
  }
}

async function addSelected() {
  if (!selectedAvailable.value.length) {
    ElMessage.warning("请先在左侧勾选群组");
    return;
  }
  try {
    const { data } = await sourcesApi.add({
      chat_ids: selectedAvailable.value,
      tags: parseTags(manual.tags),
    });
    reportResult(data);
    load();
  } catch (error) {
    ElMessage.error(error.message);
  }
}

async function addManual() {
  if (!manual.input.trim()) {
    ElMessage.warning("请输入链接或标识");
    return;
  }
  try {
    const { data } = await sourcesApi.add({
      inputs: manual.input
        .split(/[\n,，\s]+/)
        .map((item) => item.trim())
        .filter(Boolean),
      join: manual.join,
      tags: parseTags(manual.tags),
    });
    reportResult(data);
    manual.input = "";
    load();
  } catch (error) {
    ElMessage.error(error.message);
  }
}

function reportResult(data) {
  if (data.added?.length) {
    ElMessage.success(`已加入 ${data.added.length} 个监听源`);
  }
  for (const failure of data.failures || []) {
    ElMessage.warning(`${failure.input}：${failure.reason}`);
  }
}

async function removeSelected() {
  if (!selectedSources.value.length) {
    ElMessage.warning("请先在右侧勾选要移出的监听源");
    return;
  }
  try {
    for (const chatId of selectedSources.value) {
      await sourcesApi.remove(chatId);
    }
    ElMessage.success(`已移出 ${selectedSources.value.length} 个监听源`);
    load();
  } catch (error) {
    ElMessage.error(error.message);
  }
}

async function toggleEnabled(row) {
  try {
    await sourcesApi.update(row.id, { enabled: row.source_enabled });
  } catch (error) {
    ElMessage.error(error.message);
    load();
  }
}

async function editTags(row) {
  try {
    const { value } = await ElMessageBox.prompt(
      "多个标签用逗号分隔（会覆盖原有标签）",
      `${row.name || row.title || row.username} 的标签`,
      { inputValue: (row.tags || []).join(","), confirmButtonText: "保存", cancelButtonText: "取消" },
    );
    await sourcesApi.update(row.id, { tags: parseTags(value) });
    ElMessage.success("标签已更新");
    load();
  } catch (error) {
    if (error?.message && !error.message.includes("cancel")) {
      ElMessage.error(error.message);
    }
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
    await sourcesApi.update(row.id, { display_name: value ?? "" });
    ElMessage.success("备注名已更新");
    load();
  } catch (error) {
    if (error?.message && !error.message.includes("cancel")) {
      ElMessage.error(error.message);
    }
  }
}

onMounted(load);
</script>

<template>
  <div v-loading="loading">
    <div class="toolbar">
      <h2 class="page-title">监听源</h2>
      <span class="card-hint">已加入 {{ sources.length }} 个 · 可选 {{ available.length }} 个</span>
      <div class="spacer" />
      <el-button size="small" :loading="syncing" @click="syncDialogs">同步已加入的群组</el-button>
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
                  <b class="chat-name">{{ item.name || item.title || item.username }}</b>
                  <span class="card-hint">
                    {{ item.chat_type_label }}
                    <template v-if="item.username"> · @{{ item.username }}</template>
                    <template v-if="item.member_count"> · {{ item.member_count }} 人</template>
                  </span>
                </span>
              </label>
              <div v-if="!filteredAvailable.length" class="card-hint empty">
                还没有可选群组。点右上角「同步已加入的群组」把执行账号已加入的群拉过来。
              </div>
            </el-checkbox-group>
          </div>
          <div class="panel-foot">
            <el-input
              v-model="manual.input"
              size="small"
              placeholder="手动添加：t.me/xxx 或 @username，可多个"
            />
            <el-input v-model="manual.tags" size="small" placeholder="标签（可选，逗号分隔）" />
            <el-checkbox v-model="manual.join">允许执行账号加入私有邀请链接</el-checkbox>
            <el-button type="primary" size="small" @click="addManual">添加输入项</el-button>
          </div>
        </div>

        <div class="arrows">
          <el-button size="small" @click="addSelected">&rarr;</el-button>
          <el-button size="small" @click="removeSelected">&larr;</el-button>
        </div>

        <div class="panel">
          <div class="panel-head">
            <span>已成为监听源</span>
            <span class="card-hint">{{ selectedSources.length }} 项已勾选</span>
          </div>
          <div class="panel-body">
            <el-checkbox-group v-model="selectedSources" class="list">
              <label v-for="item in sources" :key="item.id" class="row">
                <el-checkbox :value="item.id" />
                <span class="grow">
                  <b class="chat-name">{{ item.name || item.title || item.username }}</b>
                  <span class="card-hint">
                    {{ item.chat_type_label }}
                    <template v-if="item.is_private"> · 私有</template>
                  </span>
                  <span v-if="item.tags?.length" class="tags">
                    <el-tag v-for="tag in item.tags" :key="tag" size="small" type="info">{{ tag }}</el-tag>
                  </span>
                </span>
                <el-switch
                  v-model="item.source_enabled"
                  size="small"
                  @change="() => toggleEnabled(item)"
                />
                <el-button size="small" link type="primary" @click.prevent="editTags(item)">标签</el-button>
                <el-button size="small" link @click.prevent="renameChat(item)">改名</el-button>
              </label>
              <div v-if="!sources.length" class="card-hint empty">还没有监听源，从左侧勾选后点 → 添加。</div>
            </el-checkbox-group>
          </div>
        </div>
      </div>
    </el-card>

    <el-alert
      class="panel-gap"
      type="info"
      :closable="false"
      show-icon
      title="同一个群可以既是监听源、又是接收组"
      description="私人邀请链接必须先勾选「允许执行账号加入」，否则无法识别群组；来源池来自执行账号已加入的群，需要账号已登录。"
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

.panel-head,
.panel-foot {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  padding: 8px 10px;
}

.panel-head {
  border-bottom: 1px solid var(--tg-border);
  font-size: 13px;
  color: #64748b;
  justify-content: space-between;
}

.panel-foot {
  border-top: 1px solid var(--tg-border);
  flex-direction: column;
  align-items: stretch;
}

.panel-body {
  padding: 8px 10px;
  max-height: 320px;
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

.chat-name {
  font-weight: 500;
  margin-right: 6px;
  word-break: break-word;
  white-space: normal;
}

.grow .card-hint {
  display: block;
  margin-top: 2px;
}

.tags {
  display: inline-flex;
  gap: 4px;
  margin-left: 6px;
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

@media (max-width: 900px) {
  .transfer {
    grid-template-columns: minmax(0, 1fr);
  }

  .arrows {
    flex-direction: row;
  }
}
</style>

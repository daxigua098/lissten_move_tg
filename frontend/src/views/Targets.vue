<script setup>
import { ElMessage, ElMessageBox } from "element-plus";
import { computed, onMounted, ref } from "vue";

import { targetsApi } from "../api";
import { TARGET_ROLE_OPTIONS } from "../targetRoles";

const loading = ref(false);
const syncing = ref(false);
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

async function syncDialogs() {
  syncing.value = true;
  try {
    const { data } = await targetsApi.sync();
    ElMessage.success(
      `已从 ${data.account} 同步：新增 ${data.created} 个、更新 ${data.updated} 个`,
    );
    await load();
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
    if (payload.enabled !== undefined) {
      ElMessage.success(
        payload.enabled
          ? `已开启「${row.name}」的投递`
          : `已停用「${row.name}」在所有线路上的投递`,
      );
    } else if (payload.role !== undefined) {
      ElMessage.success("用途已更新");
    }
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
      <el-select v-model="role" size="small" style="width: 230px">
        <el-option
          v-for="item in TARGET_ROLE_OPTIONS"
          :key="item.value"
          :label="item.fullLabel"
          :value="item.value"
        />
      </el-select>
      <el-tooltip placement="bottom-start" :show-after="150">
        <template #content>
          <div class="role-tip">
            <p v-for="item in TARGET_ROLE_OPTIONS" :key="item.value">
              <b>{{ item.label }}</b>：{{ item.hint }}
            </p>
            <p>用途只是分类标签，不限制投递；真正决定发到哪个群的是线路里勾选的接收目标。</p>
          </div>
        </template>
        <span class="role-help">用途说明</span>
      </el-tooltip>
      <el-checkbox v-model="checkAccess" size="small">入库前做权限预检</el-checkbox>
      <el-button size="small" :loading="syncing" @click="syncDialogs">
        同步账号里的群组
      </el-button>
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
                没有可选项。点右上角「同步账号里的群组」把执行账号已加入的群/频道拉过来；
                如果账号是刚建的群，在 Telegram 里进一次群再同步即可。
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
                <span class="row-line">
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
                </span>
                <span class="row-line row-actions">
                  <el-select
                    :model-value="item.target_role"
                    size="small"
                    style="width: 210px"
                    @change="(value) => updateTarget(item, { role: value })"
                  >
                    <el-option
                      v-for="option in TARGET_ROLE_OPTIONS"
                      :key="option.value"
                      :label="option.fullLabel"
                      :value="option.value"
                    />
                  </el-select>
                  <el-switch
                    v-model="item.target_enabled"
                    size="small"
                    @change="() => updateTarget(item, { enabled: item.target_enabled })"
                  />
                  <el-button size="small" link @click.prevent="renameChat(item)">改名</el-button>
                </span>
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
      description="投递前会校验发言权限；列表里的开关是总开关，关掉即停用该群在所有线路上的投递。用途只是分类标签，不限制投递。"
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

@media (max-width: 980px) {
  .transfer {
    grid-template-columns: minmax(0, 1fr);
  }

  .arrows {
    flex-direction: row;
  }
}
</style>

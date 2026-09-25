<script setup>
import { ElMessage, ElMessageBox } from "element-plus";
import { computed, onMounted, ref } from "vue";

import { targetsApi } from "../api";
import { TARGET_ROLE_OPTIONS } from "../targetRoles";

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
      description="执行账号需要已加入该群/频道并有发言权限；权限预检会标记「无发帖权限」的目标，A 线投递前还会再校验一次。用途选「线索接收」的群接收 B 线的会员线索卡片。"
    />

    <el-card shadow="never" class="panel-gap">
      <template #header>用途说明：「内容接收」和「线索接收」分别是什么</template>
      <table class="role-table">
        <thead>
          <tr>
            <th>用途</th>
            <th>对应业务线</th>
            <th>群里会出现什么</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="item in TARGET_ROLE_OPTIONS" :key="item.value">
            <td class="role-cell">
              <el-tag size="small" :type="item.tagType">{{ item.label }}</el-tag>
              <span class="card-hint">{{ item.fullLabel }}</span>
            </td>
            <td>{{ item.value === "content" ? "A 线 · 搬运帖子" : "B 线 · 监听会员" }}</td>
            <td>{{ item.hint }}</td>
          </tr>
        </tbody>
      </table>
      <p class="card-hint role-note">
        用途只是分类标签，不会限制投递：真正决定"发到哪个群"的是线路里勾选的接收目标。
        同一个群今天当内容落点、明天又可以挂到 B 线，改这个下拉即可。
      </p>
      <p class="card-hint role-note">
        列表里的开关是<b>总开关</b>：关掉会同时停用这个群在<b>所有线路</b>上的投递；
        只想停某一条线路，去「线路管理」里关那条线路的目标开关。
      </p>
    </el-card>
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

.role-table {
  width: 100%;
  border-collapse: collapse;
  font-size: 13px;
}

.role-help {
  font-size: 13px;
  color: var(--tg-muted);
  cursor: help;
  border-bottom: 1px dashed currentColor;
}

.role-tip {
  max-width: 300px;
  line-height: 1.7;
}

.role-tip p {
  margin: 0 0 6px;
}

.role-table th,
.role-table td {
  border-bottom: 1px solid var(--tg-border);
  padding: 8px 10px;
  text-align: left;
  vertical-align: top;
  line-height: 1.6;
}

.role-table th {
  color: var(--tg-muted);
  font-weight: 500;
  white-space: nowrap;
}

.role-cell {
  white-space: nowrap;
}

.role-cell .card-hint {
  margin-left: 6px;
}

.role-note {
  margin: 10px 0 0;
  line-height: 1.7;
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

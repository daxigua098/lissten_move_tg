<script setup>
import { ElMessage, ElMessageBox } from "element-plus";
import { computed, onMounted, reactive, ref } from "vue";

import { keywordsApi } from "../api";

const loading = ref(false);
const groups = ref([]);
const activeGroupId = ref(null);
const kind = ref("keyword");
const keywordForm = reactive({ word: "", aliases: "" });
const matchForm = reactive({ text: "", sensitivity: "loose", exclude_group_ids: [] });
const matchResult = ref(null);
const excludeGroups = ref([]);

const activeGroup = computed(
  () => groups.value.find((item) => item.id === activeGroupId.value) || null,
);

const kindLabel = computed(() => (kind.value === "exclude" ? "排除词组" : "关键词组"));

async function load() {
  loading.value = true;
  try {
    const [current, others] = await Promise.all([
      keywordsApi.list(kind.value),
      keywordsApi.list(kind.value === "exclude" ? "keyword" : "exclude"),
    ]);
    groups.value = current.data.items;
    excludeGroups.value = others.data.items;
    if (!groups.value.some((item) => item.id === activeGroupId.value)) {
      activeGroupId.value = groups.value[0]?.id ?? null;
    }
  } catch (error) {
    ElMessage.error(error.message);
  } finally {
    loading.value = false;
  }
}

async function switchKind(value) {
  kind.value = value;
  activeGroupId.value = null;
  matchResult.value = null;
  await load();
}

async function seed() {
  try {
    const { data } = await keywordsApi.seed();
    if (data.created) {
      ElMessage.success(`已导入 ${data.created} 个预置词组`);
    } else {
      ElMessage.info("已经有词组了，没有重复导入");
    }
    await load();
  } catch (error) {
    ElMessage.error(error.message);
  }
}

async function createGroup() {
  try {
    const { value } = await ElMessageBox.prompt(
      "例如：联系方式 / 资源求助 / 体育赛事",
      "新建关键词组",
      { confirmButtonText: "创建", cancelButtonText: "取消" },
    );
    await keywordsApi.create({ name: value, kind: kind.value });
    ElMessage.success("已创建");
    await load();
  } catch (error) {
    if (error?.message && !error.message.includes("cancel")) {
      ElMessage.error(error.message);
    }
  }
}

async function renameGroup(group) {
  try {
    const { value } = await ElMessageBox.prompt("新的组名", "重命名关键词组", {
      inputValue: group.name,
      confirmButtonText: "保存",
      cancelButtonText: "取消",
    });
    await keywordsApi.update(group.id, { name: value });
    await load();
  } catch (error) {
    if (error?.message && !error.message.includes("cancel")) {
      ElMessage.error(error.message);
    }
  }
}

async function removeGroup(group) {
  try {
    await ElMessageBox.confirm(
      `删除「${group.name}」会连同组内 ${group.keyword_count} 个关键词一起删掉，确认？`,
      "删除关键词组",
      { type: "warning", confirmButtonText: "删除", cancelButtonText: "取消" },
    );
    await keywordsApi.remove(group.id);
    ElMessage.success("已删除");
    await load();
  } catch (error) {
    if (error?.message && !error.message.includes("cancel")) {
      ElMessage.error(error.message);
    }
  }
}

async function addKeyword() {
  if (!activeGroupId.value) return;
  if (!keywordForm.word.trim()) {
    ElMessage.warning("请填写主词");
    return;
  }
  try {
    await keywordsApi.addKeyword(activeGroupId.value, {
      group_id: activeGroupId.value,
      word: keywordForm.word.trim(),
      aliases: keywordForm.aliases,
    });
    keywordForm.word = "";
    keywordForm.aliases = "";
    ElMessage.success("已添加");
    await load();
  } catch (error) {
    ElMessage.error(error.message);
  }
}

async function editKeyword(row) {
  try {
    const { value } = await ElMessageBox.prompt(
      "主词（命中后显示的就是它）",
      "修改关键词",
      { inputValue: row.word, confirmButtonText: "下一步", cancelButtonText: "取消" },
    );
    const aliases = await ElMessageBox.prompt(
      "别名：逗号 / 顿号 / 换行分隔。例：体育 的别名写 篮球、足球、乒乓球",
      "别名（可留空）",
      { inputValue: row.aliases, confirmButtonText: "保存", cancelButtonText: "取消" },
    );
    await keywordsApi.updateKeyword(row.id, { word: value, aliases: aliases.value });
    ElMessage.success("已保存");
    await load();
  } catch (error) {
    if (error?.message && !error.message.includes("cancel")) {
      ElMessage.error(error.message);
    }
  }
}

async function toggleKeyword(row) {
  try {
    await keywordsApi.updateKeyword(row.id, { enabled: row.enabled });
  } catch (error) {
    ElMessage.error(error.message);
    load();
  }
}

async function removeKeyword(row) {
  try {
    await keywordsApi.removeKeyword(row.id);
    ElMessage.success("已删除");
    await load();
  } catch (error) {
    ElMessage.error(error.message);
  }
}

async function runMatch() {
  if (!matchForm.text.trim()) {
    ElMessage.warning("先粘贴一段消息文本");
    return;
  }
  try {
    const { data } = await keywordsApi.match({
      text: matchForm.text,
      sensitivity: matchForm.sensitivity,
      exclude_group_ids: matchForm.exclude_group_ids,
    });
    matchResult.value = data;
  } catch (error) {
    ElMessage.error(error.message);
  }
}

onMounted(load);
</script>

<template>
  <div v-loading="loading">
    <div class="toolbar">
      <h2 class="page-title">词库</h2>
      <span class="card-hint">
        {{ kindLabel }} 共 {{ groups.length }} 组 · 关键词组判断"算不算线索"，排除词组负责"挡掉噪声"
      </span>
      <div class="spacer" />
      <el-button size="small" @click="seed">导入预置词库</el-button>
      <el-button size="small" type="primary" @click="createGroup">新建{{ kindLabel }}</el-button>
      <el-button size="small" @click="load">刷新</el-button>
    </div>

    <el-radio-group :model-value="kind" class="kind-switch" @change="switchKind">
      <el-radio-button value="keyword">关键词组（判断命中）</el-radio-button>
      <el-radio-button value="exclude">排除词组（命中即忽略）</el-radio-button>
    </el-radio-group>

    <div class="columns">
      <el-card shadow="never">
        <template #header>{{ kindLabel }}（{{ groups.length }}）</template>
        <div class="group-list">
          <div
            v-for="item in groups"
            :key="item.id"
            class="group-row"
            :class="{ active: item.id === activeGroupId }"
            @click="activeGroupId = item.id"
          >
            <span class="grow">
              <span class="chat-name">{{ item.name }}</span>
              <span class="card-hint">{{ item.keyword_count }} 个词</span>
            </span>
            <el-button size="small" link @click.stop="renameGroup(item)">改名</el-button>
            <el-button size="small" link type="danger" @click.stop="removeGroup(item)">
              删除
            </el-button>
          </div>
          <p v-if="!groups.length" class="card-hint">
            <template v-if="kind === 'exclude'">
              还没有排除词组。点右上角「导入预置词库」会写入「通用噪声」「广告推广号」两组，
              再按需改；线路里可以多选叠加多组。
            </template>
            <template v-else>
              还没有词组。点右上角「导入预置词库」可以先来一套常用的（联系方式、资源求助、
              引流合作、体育赛事、博彩相关），再按需改。
            </template>
          </p>
        </div>
      </el-card>

      <el-card shadow="never">
        <template #header>
          <span v-if="activeGroup">{{ activeGroup.name }} 的关键词</span>
          <span v-else>{{ kindLabel }}的词</span>
        </template>
        <div class="add-row">
          <el-input v-model="keywordForm.word" size="small" placeholder="主词（如 体育）" />
          <el-input
            v-model="keywordForm.aliases"
            size="small"
            placeholder="别名（如 篮球,足球,乒乓球），逗号分隔"
          />
          <el-button size="small" type="primary" :disabled="!activeGroupId" @click="addKeyword">
            添加
          </el-button>
        </div>
        <el-table :data="activeGroup?.keywords || []" size="small" border class="table-gap">
          <el-table-column prop="word" label="主词" width="120" />
          <el-table-column label="别名" min-width="240">
            <template #default="{ row }">
              <el-tag v-for="alias in row.alias_list" :key="alias" size="small" class="alias-tag">
                {{ alias }}
              </el-tag>
              <span v-if="!row.alias_list.length" class="card-hint">无</span>
            </template>
          </el-table-column>
          <el-table-column label="启用" width="80">
            <template #default="{ row }">
              <el-switch v-model="row.enabled" size="small" @change="() => toggleKeyword(row)" />
            </template>
          </el-table-column>
          <el-table-column label="操作" width="130">
            <template #default="{ row }">
              <el-button size="small" link @click="editKeyword(row)">编辑</el-button>
              <el-button size="small" link type="danger" @click="removeKeyword(row)">删除</el-button>
            </template>
          </el-table-column>
        </el-table>
      </el-card>
    </div>

    <el-card shadow="never" class="panel-gap">
      <template #header>试跑：粘贴一段消息，看看会不会命中</template>
      <div class="add-row">
        <el-input
          v-model="matchForm.text"
          type="textarea"
          :rows="3"
          placeholder="例如：今晚有篮球赛吗，求推荐"
        />
        <el-select v-model="matchForm.sensitivity" size="small" style="width: 140px">
          <el-option label="宽松（宁可多报）" value="loose" />
          <el-option label="标准" value="standard" />
          <el-option label="严格（宁可少报）" value="strict" />
        </el-select>
        <el-select
          v-model="matchForm.exclude_group_ids"
          multiple
          size="small"
          collapse-tags
          placeholder="叠加排除词组（可选）"
          style="width: 220px"
        >
          <el-option
            v-for="item in excludeGroups"
            :key="item.id"
            :label="item.name"
            :value="item.id"
          />
        </el-select>
        <el-button size="small" type="primary" @click="runMatch">试跑</el-button>
      </div>
      <div v-if="matchResult" class="match-result">
        <p class="card-hint">
          参与匹配的关键词 {{ matchResult.keyword_total }} 个，排除词
          {{ matchResult.exclude_total }} 个，命中 {{ matchResult.hits.length }} 个
        </p>
        <el-tag
          v-for="hit in matchResult.hits"
          :key="hit.keyword + hit.matched"
          :type="hit.mode === 'contains' ? 'success' : 'warning'"
          class="alias-tag"
        >
          {{ hit.keyword }}（命中「{{ hit.matched }}」· {{ hit.mode }} · {{ hit.score }}）
        </el-tag>
        <p v-if="!matchResult.hits.length" class="card-hint">
          没有命中：可以把这句话里的词加进关键词，或者给主词补别名。
        </p>
      </div>
    </el-card>

    <el-alert
      class="panel-gap"
      type="info"
      :closable="false"
      show-icon
      title="别名就是「语义相近」的做法"
      description="体育 挂上 篮球、足球、乒乓球，说这些词都算命中体育。这样不用接 AI 模型，命中原因也完全看得懂；想放宽到没见过的新词，再考虑加语义增强。"
    />
  </div>
</template>

<style scoped>
.columns {
  display: grid;
  grid-template-columns: minmax(0, 320px) minmax(0, 1fr);
  gap: 12px;
}

.kind-switch {
  margin-bottom: 12px;
}

.group-list {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.group-row {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 10px;
  border: 1px solid var(--tg-border);
  border-radius: 8px;
  background: #fff;
  cursor: pointer;
}

.group-row.active {
  border-color: var(--tg-accent);
  background: #f3f7ff;
}

.grow {
  flex: 1;
  min-width: 0;
}

.add-row {
  display: flex;
  gap: 8px;
  align-items: flex-start;
  flex-wrap: wrap;
}

.add-row .el-input {
  flex: 1;
  min-width: 180px;
}

.table-gap {
  margin-top: 10px;
}

.alias-tag {
  margin: 0 4px 2px 0;
}

.panel-gap {
  margin-top: 12px;
}

.match-result {
  margin-top: 10px;
  line-height: 1.8;
}

@media (max-width: 900px) {
  .columns {
    grid-template-columns: minmax(0, 1fr);
  }
}
</style>

<script setup>
import { ElMessage, ElMessageBox } from "element-plus";
import { computed, reactive, ref, watch } from "vue";

import { adAssetsApi, keywordsApi, routesApi } from "../api";
import {
  collectRoleMismatches,
  targetRoleFullLabel,
  targetRoleShortLabel,
  targetRoleTagType,
} from "../targetRoles";

const props = defineProps({
  modelValue: { type: Boolean, default: false },
  routeId: { type: Number, default: null },
  sources: { type: Array, default: () => [] },
  targets: { type: Array, default: () => [] },
});
const emit = defineEmits(["update:modelValue", "saved"]);

const loading = ref(false);
const saving = ref(false);
const assets = ref([]);
const keywordGroups = ref([]);
const excludeGroups = ref([]);
// 打开时的原始值：用来判断这次保存要不要重启运行时才生效
const original = reactive({ sourceIds: [], businessType: null });
const RESTART_HINT = "改了监听源或业务类型，去「运行总览」点重启才生效";

/** 监听源、业务类型是启动时注册的，动了这两样必须重启运行时。 */
function needsRestart() {
  const before = [...original.sourceIds].sort().join(",");
  const after = [...form.source_chat_ids].sort().join(",");
  return before !== after || original.businessType !== form.business_type;
}
const detailTargets = ref([]);
const addingTargetIds = ref([]);
const form = reactive({
  name: "",
  source_chat_ids: [],
  business_type: "A",
  exec_account_id: null,
  notify_bot_id: null,
  priority: 100,
  delay_seconds: 1.0,
  hourly_limit: null,
  daily_limit: null,
  enabled: true,
  a: {},
  b: {},
});

const visible = computed({
  get: () => props.modelValue,
  set: (value) => emit("update:modelValue", value),
});
const isEdit = computed(() => Boolean(props.routeId));
const targetRoleWarnings = computed(() =>
  collectRoleMismatches(
    form.business_type,
    detailTargets.value.filter((item) => item.enabled !== false),
  ),
);
/** 选了挂广告但没选素材：后端会拦下，这里提前提示，别等点保存才报错。 */
const adAssetMissing = computed(
  () => form.business_type === "A" && form.a.ad_policy !== "none" && !form.a.ad_asset_id,
);
/** 关键词组在配置里存成逗号分隔的 ID，界面上给人看的是组名多选。 */
const keywordGroupIds = computed({
  get: () => splitIds(form.b.keyword_ids_text),
  set: (value) => {
    form.b.keyword_ids_text = (value || []).join(",");
  },
});
/** 排除词：配置里存的是数组，界面用可增删的标签编辑（每条线路各自一套）。 */
const excludeWords = computed({
  get: () => splitLines(form.b.exclude_text),
  set: (value) => {
    form.b.exclude_text = (value || []).join(",");
  },
});
/** 线路名留空时用「监听源 → 接收目标」自动命名，避免提交空名字被后端打回。 */
const suggestedName = computed(() => {
  const picked = form.source_chat_ids
    .map((id) => props.sources.find((item) => item.id === id))
    .filter(Boolean);
  if (!picked.length) return "";
  const first = picked[0].name || picked[0].title || picked[0].username || "";
  const sourceName = picked.length > 1 ? `${first} 等 ${picked.length} 个源` : first;
  if (!sourceName) return "";
  const targetNames = detailTargets.value
    .map((item) => item.name || item.title || item.username)
    .filter(Boolean);
  return targetNames.length ? `${sourceName} → ${targetNames.join("、")}` : sourceName;
});

function splitLines(text) {
  return String(text || "")
    .split(/[\n,，、]+/)
    .map((item) => item.trim())
    .filter(Boolean);
}

function splitIds(text) {
  return splitLines(text)
    .map((item) => Number(item))
    .filter((item) => Number.isInteger(item) && item > 0);
}

function defaults() {
  return {
    a: {
      content_types: ["text", "photo", "video"],
      strip_url: true,
      strip_mention: true,
      strip_buttons: true,
      strip_phone: false,
      promo_text: "",
      transfer_mode: "copy",
      text_mode: "clean",
      album_aggregate: true,
      ad_policy: "nth",
      ad_nth: 3,
      ad_asset_id: null,
      history_backfill: true,
      history_limit: 500,
      backfill_rate_seconds: 2.0,
      skip_pinned: true,
    },
    b: {
      listen_mode: "all",
      keyword_ids_text: "",
      exclude_group_ids: [],
      sensitivity: "loose",
      match_contains: true,
      match_fuzzy: true,
      match_semantic: false,
      exclude_text: "机器人, 客服, 管理",
      skip_bots: true,
      skip_admins: true,
      min_text_length: 4,
      capture_phone: true,
      capture_contact: true,
      hit_cooldown_minutes: 10,
      push_card_on_all: false,
      lead_template: "",
    },
  };
}

function resetForm() {
  const blank = defaults();
  original.sourceIds = [];
  original.businessType = null;
  Object.assign(form, {
    name: "",
    source_chat_ids: props.sources[0] ? [props.sources[0].id] : [],
    business_type: "A",
    exec_account_id: null,
    notify_bot_id: null,
    priority: 100,
    delay_seconds: 1.0,
    hourly_limit: null,
    daily_limit: null,
    enabled: true,
    a: blank.a,
    b: blank.b,
  });
  detailTargets.value = [];
}

function applyDetail(data) {
  const blank = defaults();
  original.sourceIds = data.source_chat_ids?.length
    ? [...data.source_chat_ids]
    : data.source?.chat_id
      ? [data.source.chat_id]
      : [];
  original.businessType = data.business_type;
  Object.assign(form, {
    name: data.name,
    source_chat_ids: data.source_chat_ids?.length
      ? [...data.source_chat_ids]
      : data.source?.chat_id
        ? [data.source.chat_id]
        : [],
    business_type: data.business_type,
    exec_account_id: data.exec_account_id,
    notify_bot_id: data.notify_bot_id,
    priority: data.priority,
    delay_seconds: data.delay_seconds,
    hourly_limit: data.hourly_limit,
    daily_limit: data.daily_limit,
    enabled: data.enabled,
    a: {
      ...blank.a,
      ...data.a_config,
      promo_text: (data.a_config.promo_blacklist || []).join("\n"),
    },
    b: {
      ...blank.b,
      ...data.b_config,
      keyword_ids_text: (data.b_config.keyword_group_ids || []).join(","),
      exclude_text: (data.b_config.exclude_keywords || []).join(", "),
    },
  });
  detailTargets.value = data.targets;
}

function buildAConfig() {
  const { promo_text: promoText, ...rest } = form.a;
  return { ...rest, promo_blacklist: splitLines(promoText) };
}

function buildBConfig() {
  const { keyword_ids_text: idsText, exclude_text: excludeText, ...rest } = form.b;
  return {
    ...rest,
    keyword_group_ids: splitIds(idsText),
    exclude_keywords: splitLines(excludeText),
  };
}

async function loadAssets() {
  try {
    const { data } = await adAssetsApi.list({ limit: 200 });
    assets.value = data.items;
  } catch {
    assets.value = [];
  }
}

async function loadKeywordGroups() {
  try {
    const [keywordResult, excludeResult] = await Promise.all([
      keywordsApi.list("keyword"),
      keywordsApi.list("exclude"),
    ]);
    keywordGroups.value = keywordResult.data.items;
    excludeGroups.value = excludeResult.data.items;
  } catch {
    keywordGroups.value = [];
    excludeGroups.value = [];
  }
}

async function loadDetail() {
  loading.value = true;
  try {
    const { data } = await routesApi.detail(props.routeId);
    applyDetail(data);
  } catch (error) {
    ElMessage.error(error.message);
  } finally {
    loading.value = false;
  }
}

watch(
  () => [props.modelValue, props.routeId],
  async ([open, routeId]) => {
    if (!open) return;
    await loadAssets();
    await loadKeywordGroups();
    if (routeId) {
      await loadDetail();
    } else {
      resetForm();
    }
  },
);

async function save() {
  const name = (form.name || "").trim() || suggestedName.value;
  if (!name) {
    ElMessage.warning("请填写线路名，或先选择监听源与接收目标（会自动命名）");
    return;
  }
  if (adAssetMissing.value) {
    ElMessage.warning(
      assets.value.length
        ? "挂广告需要先选择广告素材，或把「挂广告频率」改成「不挂」"
        : "还没有广告素材：先去「广告素材库」新建一条，或把「挂广告频率」改成「不挂」",
    );
    return;
  }
  if (!detailTargets.value.length) {
    ElMessage.warning("请先选择接收目标：在「接收目标」下面的下拉里选一个群，选中就会自动加进来");
    return;
  }
  saving.value = true;
  try {
    const payload = {
      name,
      source_chat_ids: form.source_chat_ids,
      business_type: form.business_type,
      exec_account_id: form.exec_account_id,
      notify_bot_id: form.notify_bot_id,
      priority: form.priority,
      delay_seconds: form.delay_seconds,
      hourly_limit: form.hourly_limit,
      daily_limit: form.daily_limit,
      enabled: form.enabled,
      a_config: buildAConfig(),
      b_config: buildBConfig(),
    };
    if (isEdit.value) {
      await routesApi.update(props.routeId, payload);
      ElMessage.success(needsRestart() ? RESTART_HINT : "线路已保存（配置、目标、启停立即生效）");
    } else {
      await routesApi.create({
        ...payload,
        target_chat_ids: detailTargets.value.map((item) => item.chat_id),
      });
      ElMessage.success(`线路已创建；${RESTART_HINT}`);
    }
    emit("saved");
    visible.value = false;
  } catch (error) {
    ElMessage.error(error.message);
  } finally {
    saving.value = false;
  }
}

function pushLocalTarget(targetId) {
  const target = props.targets.find((item) => item.id === targetId);
  if (!target) return false;
  // 已在列表里的不重复添加
  if (detailTargets.value.some((item) => item.chat_id === target.id)) return false;
  detailTargets.value.push({
    chat_id: target.id,
    name: target.name,
    title: target.title,
    username: target.username,
    target_role: target.target_role,
    target_role_label: target.target_role_label,
    enabled: true,
    can_post: target.can_post,
    last_delivered_message_id: 0,
    backfill_status: "idle",
  });
  return true;
}

async function addTargets() {
  if (!addingTargetIds.value.length) {
    ElMessage.warning("请选择要追加的接收目标");
    return;
  }
  if (!isEdit.value) {
    addingTargetIds.value.forEach(pushLocalTarget);
    addingTargetIds.value = [];
    return;
  }
  try {
    const { data } = await routesApi.addTargets(props.routeId, addingTargetIds.value);
    addingTargetIds.value = [];
    await loadDetail();
    const skipped = data.skipped?.length || 0;
    ElMessage.success(
      skipped
        ? `已在列表里的 ${skipped} 个被跳过，新增 ${data.added.length} 个目标`
        : `已加入 ${data.added.length} 个目标（只为新目标补齐历史）`,
    );
  } catch (error) {
    ElMessage.error(error.message);
  }
}

/** 下拉里选中即加入列表，不用再点「追加」——少这一步就不会漏。 */
async function onPickTargets(ids) {
  if (!ids?.length) return;
  addingTargetIds.value = ids;
  await addTargets();
}

async function removeTarget(row) {
  if (!isEdit.value) {
    detailTargets.value = detailTargets.value.filter((item) => item.chat_id !== row.chat_id);
    return;
  }
  try {
    await routesApi.removeTarget(props.routeId, row.chat_id);
    await loadDetail();
    ElMessage.success("已移除该接收目标");
  } catch (error) {
    ElMessage.error(error.message);
  }
}

async function toggleTarget(row) {
  if (!isEdit.value) return;
  try {
    await routesApi.setTargetEnabled(props.routeId, row.chat_id, row.enabled);
  } catch (error) {
    ElMessage.error(error.message);
    await loadDetail();
  }
}

async function resetProgress(row) {
  if (!isEdit.value) return;
  try {
    await ElMessageBox.confirm(
      `重置后「${row.title || row.username}」会重新搬运历史消息，可能产生重复内容；其他目标不受影响。`,
      "重置搬运进度",
      { type: "warning", confirmButtonText: "输入 RESET 确认" },
    );
  } catch {
    return;
  }
  try {
    const { value } = await ElMessageBox.prompt("请输入 RESET 以确认", "危险操作", {
      inputPlaceholder: "RESET",
      inputValidator: (input) => (input || "").trim().toUpperCase() === "RESET" || "请输入 RESET",
      confirmButtonText: "确认重置",
      cancelButtonText: "取消",
    });
    await routesApi.resetProgress(props.routeId, row.chat_id, value);
    ElMessage.success("进度已重置");
    await loadDetail();
  } catch (error) {
    if (error?.message && !error.message.includes("cancel")) {
      ElMessage.error(error.message);
    }
  }
}
</script>

<template>
  <el-drawer v-model="visible" :title="isEdit ? '编辑线路' : '新建线路'" size="640px">
    <div v-loading="loading" class="editor">
      <el-divider content-position="left">基础信息</el-divider>
      <el-form label-position="top">
        <el-form-item label="线路名">
          <el-input
            v-model="form.name"
            :placeholder="
              suggestedName ? `留空自动用：${suggestedName}` : '例如：素材频道 → 主频道'
            "
          />
        </el-form-item>
        <el-form-item label="业务类型">
          <el-radio-group v-model="form.business_type">
            <el-radio-button value="A">A 搬运帖子</el-radio-button>
            <el-radio-button value="B">B 监听会员</el-radio-button>
          </el-radio-group>
        </el-form-item>
        <el-form-item label="监听源（可多选）">
          <el-select
            v-model="form.source_chat_ids"
            multiple
            collapse-tags
            placeholder="选几个就同时监听几个源"
            style="width: 100%"
          >
            <el-option
              v-for="item in sources"
              :key="item.id"
              :label="item.name || item.title || item.username"
              :value="item.id"
            />
          </el-select>
          <p class="card-hint">
            多选时会按「一个源一条线路」落库（水位线与历史补齐才准确），
            界面上算同一条线路：同一套配置、同一组接收目标，改一次全部生效。
          </p>
        </el-form-item>
        <div class="two-cols">
          <el-form-item label="执行账号 ID（留空用默认）">
            <el-input v-model.number="form.exec_account_id" placeholder="默认" />
          </el-form-item>
          <el-form-item label="通知 Bot ID（留空用默认）">
            <el-input v-model.number="form.notify_bot_id" placeholder="默认" />
          </el-form-item>
          <el-form-item label="投递间隔（秒）">
            <el-input v-model.number="form.delay_seconds" />
          </el-form-item>
          <el-form-item label="优先级">
            <el-input v-model.number="form.priority" />
          </el-form-item>
          <el-form-item label="每小时上限">
            <el-input v-model.number="form.hourly_limit" placeholder="不限" />
          </el-form-item>
          <el-form-item label="每日上限">
            <el-input v-model.number="form.daily_limit" placeholder="不限" />
          </el-form-item>
        </div>
      </el-form>

      <el-divider content-position="left">接收目标</el-divider>
      <div class="targets">
        <div v-for="item in detailTargets" :key="item.chat_id" class="target-row">
          <span class="grow">
            {{ item.name || item.title || item.username }}
            <el-tag size="small" :type="targetRoleTagType(item.target_role)">
              {{ targetRoleShortLabel(item.target_role) }}
            </el-tag>
            <el-tag v-if="item.can_post === false" size="small" type="danger">无发帖权限</el-tag>
          </span>
          <span class="card-hint">水位线 {{ item.last_delivered_message_id }}</span>
          <el-switch v-model="item.enabled" size="small" @change="() => toggleTarget(item)" />
          <el-button size="small" link type="warning" @click="resetProgress(item)">重置进度</el-button>
          <el-button size="small" link type="danger" @click="removeTarget(item)">移除</el-button>
        </div>
        <div v-if="!detailTargets.length" class="card-hint">
          还没有接收目标：用下面的下拉选一个群，选中就会自动加进来。
        </div>
      </div>
      <el-alert
        v-if="targetRoleWarnings.length"
        class="role-warn"
        type="error"
        :closable="false"
        show-icon
        title="接收目标的用途与线路业务类型不一致（只是提醒，不影响投递）"
      >
        <p v-for="item in targetRoleWarnings" :key="item.name">{{ item.name }}：{{ item.text }}</p>
      </el-alert>
      <div class="add-target">
        <el-select
          v-model="addingTargetIds"
          multiple
          collapse-tags
          placeholder="从接收组里选，选中即添加"
          style="flex: 1"
          @change="onPickTargets"
        >
          <el-option
            v-for="item in targets"
            :key="item.id"
            :label="`${item.name || item.title || item.username}（${targetRoleFullLabel(item.target_role)}）`"
            :value="item.id"
          />
        </el-select>
      </div>
      <p class="card-hint">
        在上面下拉里<b>选中就会自动加入</b>，不用再点按钮；新增目标只会为它补齐历史消息，
        已存在目标不会重复搬运。
      </p>

      <el-form v-if="form.business_type === 'A'" label-position="top">
        <el-divider content-position="left">A 线：内容与净化</el-divider>
        <el-form-item label="搬运内容类型">
          <el-checkbox-group v-model="form.a.content_types">
            <el-checkbox value="text">文字</el-checkbox>
            <el-checkbox value="photo">图片</el-checkbox>
            <el-checkbox value="video">视频</el-checkbox>
            <el-checkbox value="document">文档</el-checkbox>
            <el-checkbox value="poll">投票</el-checkbox>
          </el-checkbox-group>
        </el-form-item>
        <el-form-item label="净化规则">
          <div class="switch-grid">
            <el-checkbox v-model="form.a.strip_url">剥离链接</el-checkbox>
            <el-checkbox v-model="form.a.strip_mention">剥离 @提及</el-checkbox>
            <el-checkbox v-model="form.a.strip_buttons">剥离对方按钮</el-checkbox>
            <el-checkbox v-model="form.a.strip_phone">剥离电话号码</el-checkbox>
            <el-checkbox v-model="form.a.album_aggregate">相册合并投递</el-checkbox>
            <el-checkbox v-model="form.a.skip_pinned">跳过置顶消息</el-checkbox>
          </div>
        </el-form-item>
        <el-form-item label="文案处理（决定上面这些净化规则是否生效）">
          <el-radio-group v-model="form.a.text_mode">
            <el-radio value="clean">clean 净化后重新上传（去掉对方的链接与广告，慢一些）</el-radio>
            <el-radio value="keep">keep 原文直接转发（快，对方的广告会一起搬过去）</el-radio>
          </el-radio-group>
          <p class="card-hint">
            只有 clean 模式会执行「净化规则」与「推广词黑名单」；keep 模式原样转发，
            源群带的链接、@提及、广告词都会一起进目标群。
          </p>
        </el-form-item>
        <el-form-item label="推广词黑名单（一行一个，整行命中即删除）">
          <el-input v-model="form.a.promo_text" type="textarea" :rows="3" />
        </el-form-item>
        <el-form-item label="搬运模式">
          <el-radio-group v-model="form.a.transfer_mode">
            <el-radio value="copy">copy 去来源标记</el-radio>
            <el-radio value="forward">forward 保留来源</el-radio>
          </el-radio-group>
        </el-form-item>

        <el-divider content-position="left">A 线：广告策略</el-divider>
        <el-form-item label="挂广告频率">
          <el-radio-group v-model="form.a.ad_policy">
            <el-radio value="none">不挂</el-radio>
            <el-radio value="every">每条挂</el-radio>
            <el-radio value="nth">每 N 条挂 1 条</el-radio>
          </el-radio-group>
        </el-form-item>
        <el-form-item v-if="form.a.ad_policy === 'nth'" label="每多少条挂 1 条">
          <el-input v-model.number="form.a.ad_nth" style="width: 120px" />
        </el-form-item>
        <el-form-item label="广告素材（内容在「广告素材库」页维护）">
          <el-select
            v-model="form.a.ad_asset_id"
            clearable
            style="width: 100%"
            placeholder="选择素材"
          >
            <el-option
              v-for="item in assets"
              :key="item.id"
              :label="`${item.name}（被 ${item.reference_count} 条线路引用）`"
              :value="item.id"
            />
          </el-select>
        </el-form-item>
        <p class="card-hint" :class="{ 'warn-text': adAssetMissing }">
          {{
            adAssetMissing
              ? "还没选素材，保存会被拦下：先去「广告素材库」新建一条，或把上面改成「不挂」。"
              : "选择「每条 / 每 N 条」时必须选素材，否则保存会被拦下。"
          }}
        </p>

        <el-divider content-position="left">A 线：历史补齐</el-divider>
        <el-form-item>
          <el-checkbox v-model="form.a.history_backfill">
            启用历史补齐（每个目标独立水位线）
          </el-checkbox>
        </el-form-item>
        <div class="two-cols">
          <el-form-item label="补齐范围（最近 N 条）">
            <el-input v-model.number="form.a.history_limit" />
          </el-form-item>
          <el-form-item label="补齐速率（秒/条）">
            <el-input v-model.number="form.a.backfill_rate_seconds" />
          </el-form-item>
        </div>
      </el-form>

      <el-form v-else label-position="top">
        <el-divider content-position="left">B 线：监听设置</el-divider>
        <el-form-item label="监听范围">
          <el-radio-group v-model="form.b.listen_mode">
            <el-radio value="all">全量监听（只入库，不刷屏）</el-radio>
            <el-radio value="keyword">仅关键词命中（推线索卡片）</el-radio>
          </el-radio-group>
        </el-form-item>
        <el-form-item label="关键词组（在「关键词词库」页维护）">
          <el-select
            v-model="keywordGroupIds"
            multiple
            collapse-tags
            placeholder="不选=所有启用的关键词都参与匹配"
            style="width: 100%"
          >
            <el-option
              v-for="item in keywordGroups"
              :key="item.id"
              :label="`${item.name}（${item.keyword_count} 个词）`"
              :value="item.id"
            />
          </el-select>
          <p class="card-hint">
            命中组里任意一个词（含别名）就算线索；全量监听时也会用这些词判断要不要推卡片。
            <span v-if="!keywordGroups.length">
              还没有词组，先去「关键词词库」页导入预置别名库。
            </span>
          </p>
        </el-form-item>
        <el-form-item label="敏感度">
          <el-radio-group v-model="form.b.sensitivity">
            <el-radio value="loose">宽松（宁可多报）</el-radio>
            <el-radio value="standard">标准</el-radio>
            <el-radio value="strict">严格（宁可少报）</el-radio>
          </el-radio-group>
        </el-form-item>
        <el-form-item label="匹配方式">
          <div class="switch-grid">
            <el-checkbox v-model="form.b.match_contains">包含匹配</el-checkbox>
            <el-checkbox v-model="form.b.match_fuzzy">模糊匹配</el-checkbox>
            <el-checkbox v-model="form.b.match_semantic">语义匹配（有成本）</el-checkbox>
          </div>
        </el-form-item>
        <el-form-item label="排除词（命中即整条忽略）">
          <el-select
            v-model="form.b.exclude_group_ids"
            multiple
            collapse-tags
            placeholder="从共享排除词库多选（可叠加多组）"
            style="width: 100%"
          >
            <el-option
              v-for="item in excludeGroups"
              :key="item.id"
              :label="`${item.name}（${item.keyword_count} 个词）`"
              :value="item.id"
            />
          </el-select>
          <p class="card-hint">
            排除词库在「关键词词库」页维护，可多选叠加多组；下面再加这条线路自己的补充词，
            两者取并集。
          </p>
          <el-select
            v-model="excludeWords"
            multiple
            filterable
            allow-create
            default-first-option
            :reserve-keyword="false"
            placeholder="输入一个词后按回车，可以加多个"
            style="width: 100%"
          >
            <el-option v-for="word in excludeWords" :key="word" :label="word" :value="word" />
          </el-select>
          <p class="card-hint">
            本条线路生效的排除词 = 上面选中的词组（可多组叠加）+ 下面这些自定义词，取并集。
            消息里出现任意一个，整条直接忽略（不记线索、也不推卡片）；全量监听同样生效。
          </p>
        </el-form-item>
        <div class="two-cols">
          <el-form-item label="最少字数">
            <el-input v-model.number="form.b.min_text_length" />
          </el-form-item>
          <el-form-item label="去重窗口（分钟）">
            <el-input v-model.number="form.b.hit_cooldown_minutes" />
          </el-form-item>
        </div>
        <el-form-item label="资料抓取与过滤">
          <div class="switch-grid">
            <el-checkbox v-model="form.b.capture_phone">抓手机号</el-checkbox>
            <el-checkbox v-model="form.b.capture_contact">抓微信 / QQ / 邮箱</el-checkbox>
            <el-checkbox v-model="form.b.skip_bots">跳过机器人</el-checkbox>
            <el-checkbox v-model="form.b.skip_admins">跳过管理员</el-checkbox>
            <el-checkbox v-model="form.b.push_card_on_all">全量模式也推卡片</el-checkbox>
          </div>
        </el-form-item>
        <el-form-item label="线索卡片模板（留空用系统默认）">
          <el-input v-model="form.b.lead_template" type="textarea" :rows="3" />
        </el-form-item>
      </el-form>
    </div>

    <template #footer>
      <el-button @click="visible = false">取消</el-button>
      <el-button type="primary" :loading="saving" @click="save">保存</el-button>
    </template>
  </el-drawer>
</template>

<style scoped>
.editor {
  padding-bottom: 12px;
}

.two-cols {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
  gap: 0 12px;
}

.targets {
  display: flex;
  flex-direction: column;
  gap: 6px;
  margin-bottom: 8px;
}

.target-row {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 6px 8px;
  border: 1px solid var(--tg-border);
  border-radius: 8px;
  background: #fff;
}

.grow {
  flex: 1;
  min-width: 0;
}

.add-target {
  display: flex;
  gap: 8px;
  align-items: center;
}

.role-warn {
  margin-top: 10px;
}

.role-warn p {
  margin: 2px 0;
  line-height: 1.6;
}

.warn-text {
  color: #e6a23c;
}

.switch-grid {
  display: flex;
  gap: 8px 18px;
  flex-wrap: wrap;
}
</style>

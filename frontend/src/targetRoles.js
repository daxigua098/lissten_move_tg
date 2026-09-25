/**
 * 接收组「用途」定义。
 *
 * 重要：用途只是给这个群打的分类标签，方便挑目标时一眼看出它属于哪条线。
 * 真正决定「发到哪个群」的是线路里勾选的接收目标，用途本身不限制投递。
 */

export const TARGET_ROLE_OPTIONS = [
  {
    value: "content",
    label: "内容接收",
    fullLabel: "内容接收（A 线搬运落点）",
    tagType: "success",
    hint: "接收 A 线搬过来的帖子：文字 / 图片 / 视频，按需附加广告。一般是对外的内容群、频道。",
  },
  {
    value: "lead",
    label: "线索接收",
    fullLabel: "线索接收（B 线线索卡片）",
    tagType: "warning",
    hint: "接收 B 线生成的会员线索卡片：谁在哪个群说了什么 + 可联系的账号信息。一般是内部线索池。",
  },
];

const BY_VALUE = new Map(TARGET_ROLE_OPTIONS.map((item) => [item.value, item]));

export function targetRoleShortLabel(value) {
  return BY_VALUE.get(value)?.label || value || "未设置";
}

export function targetRoleFullLabel(value) {
  return BY_VALUE.get(value)?.fullLabel || targetRoleShortLabel(value);
}

export function targetRoleTagType(value) {
  return BY_VALUE.get(value)?.tagType || "info";
}

/** 用途与线路业务类型不一致时给出提醒文案；一致或未设置时返回空串。 */
export function targetRoleMismatch(businessType, role) {
  if (businessType === "B" && role === "content") {
    return "标着「内容接收」的群挂在 B 线上，会收到会员线索卡片，可能刷屏对外内容群";
  }
  if (businessType === "A" && role === "lead") {
    return "标着「线索接收」的群挂在 A 线上，会收到搬运的帖子内容";
  }
  return "";
}

/** 收集一组目标里用途不匹配的提醒，返回 [{ name, text }]。 */
export function collectRoleMismatches(businessType, rows) {
  return rows
    .map((item) => ({
      name: item.name || item.title || item.username || `#${item.chat_id ?? item.id}`,
      text: targetRoleMismatch(businessType, item.target_role),
    }))
    .filter((item) => item.text);
}

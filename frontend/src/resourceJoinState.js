const JOIN_LABEL = {
  pending: "待加入",
  running: "加入中",
  success: "已加入",
  waiting_approval: "待审批",
  failed: "加入失败",
};

const JOIN_TAG = {
  pending: "warning",
  running: "warning",
  success: "success",
  waiting_approval: "warning",
  failed: "danger",
};

/** 账号到底进群没有：只认加退群任务；探测能读取不等于已加入。 */
export function isJoined(row) {
  const latest = row?.join;
  if (!latest) return false;
  if (latest.action === "leave") return latest.status !== "success";
  return latest.status === "success";
}

/** 公开资源未加入也可能被探测成功，界面要把“可读取”和“已加入”分开。 */
export function isReadable(row) {
  return row?.resource_state === "active";
}

export function joinState(row) {
  const latest = row?.join;
  if (!latest) return { label: "未加入", type: "info" };
  if (isJoined(row)) {
    if (latest.action === "leave" && latest.status !== "success") {
      return { label: "退出中", type: "warning" };
    }
    return { label: "已在群里", type: "success" };
  }
  if (latest.action === "leave" && latest.status === "success") {
    return { label: "已退出", type: "info" };
  }
  return {
    label: JOIN_LABEL[latest.status] || latest.status,
    type: JOIN_TAG[latest.status] || "info",
  };
}

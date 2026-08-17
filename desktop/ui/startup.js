const invoke = window.__TAURI__.core.invoke;

const stage = document.querySelector(".runtime-stage");
const title = document.querySelector("#status-title");
const detail = document.querySelector("#status-detail");
const actions = document.querySelector("#recovery-actions");
const coreNode = document.querySelector("#core-node");
const auxiliaryNode = document.querySelector("#auxiliary-node");
const workspaceNode = document.querySelector("#workspace-node");
const footerStatus = document.querySelector("#footer-status");
let bootstrapAttempt = 0;

function setStatus(nextTitle, nextDetail) {
  title.textContent = nextTitle;
  detail.textContent = nextDetail;
}

function setFailed(message) {
  document.body.classList.add("failed");
  stage.setAttribute("aria-busy", "false");
  coreNode.className = "service-node error";
  const cleanMessage = String(message || "")
    .replace(/^Error:\s*/i, "")
    .trim();
  setStatus("后台服务未能启动", cleanMessage || "请重试，或打开日志查看具体原因。");
  footerStatus.textContent = "后台未连接，可从日志查看具体原因";
  actions.hidden = false;
}

async function openConsole(path = "/") {
  workspaceNode.className = "service-node ready";
  setStatus("项目控制台已就绪", "正在打开本机工作台...");
  stage.setAttribute("aria-busy", "false");
  window.location.replace(`http://127.0.0.1:8765${path}`);
}

async function bootstrap() {
  const attempt = ++bootstrapAttempt;
  document.body.classList.remove("failed");
  actions.hidden = true;
  stage.setAttribute("aria-busy", "true");
  coreNode.className = "service-node active";
  auxiliaryNode.className = "service-node";
  workspaceNode.className = "service-node";
  setStatus("正在连接项目控制台", "检查本机后台服务状态...");
  footerStatus.textContent = "后台服务仅在当前电脑运行";
  const slowTimer = window.setTimeout(() => {
    if (attempt !== bootstrapAttempt) return;
    setStatus("后台仍在启动", "核心服务尚未响应，可以继续等待或使用下方恢复操作。");
    footerStatus.textContent = "启动时间超过 5 秒，可打开日志检查";
    actions.hidden = false;
  }, 5000);
  try {
    const initial = await invoke("runtime_status");
    if (!initial.ready) {
      setStatus("正在启动核心服务", "后台服务未运行，正在通过本机运行时启动。");
    }
    const result = await invoke("ensure_runtime");
    coreNode.className = "service-node ready";
    auxiliaryNode.className = result.auxiliaryReady
      ? "service-node ready"
      : "service-node active";
    await openConsole(result.lastRoute || "/");
  } catch (error) {
    setFailed(String(error));
  } finally {
    window.clearTimeout(slowTimer);
  }
}

document.querySelector("#retry-button").addEventListener("click", bootstrap);
document.querySelector("#logs-button").addEventListener("click", async () => {
  try {
    await invoke("open_logs");
  } catch (error) {
    setFailed(String(error));
  }
});
document.querySelector("#repair-button").addEventListener("click", async () => {
  try {
    setStatus("正在重新注册后台", "正在修复当前用户的后台启动任务...");
    const result = await invoke("repair_runtime");
    await openConsole(result.lastRoute || "/");
  } catch (error) {
    setFailed(String(error));
  }
});
document.querySelector("#import-button").addEventListener("click", async () => {
  try {
    const result = await invoke("import_legacy_data");
    if (result.cancelled) return;
    setStatus("旧数据已导入", "正在重新启动本机后台服务...");
    await bootstrap();
  } catch (error) {
    setFailed(String(error));
  }
});

bootstrap();

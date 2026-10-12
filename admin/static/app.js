"use strict";
const $ = id => document.getElementById(id);
let csrf = "", overview = null, pendingConfirm = null, page = "dashboard", busy = false;
let accountMode = "browser", oauthFlow = null, oauthTimer = null, oauthGeneration = 0;
let workspaceGeneration = 0, overviewGeneration = 0;
let modelCatalog = null, modelAccountId = null, modelLoading = false, modelError = "", modelGeneration = 0;
const labels = {
  dashboard: ["让每个账号，各尽其用", "WORKBUDDY WORKSPACE", "在这里查看服务运行、账号积分和请求表现。"],
  accounts: ["账号池", "ACCOUNT POOL", "集中管理登录凭据、积分与可用状态，让请求自动分配到可用账号。"],
  models: ["模型", "MODEL DIRECTORY", "读取手动 / 测试账号的远端配置，查看免费账号模型的倍率与优惠。"],
  keys: ["API 密钥", "CLIENT ACCESS", "为每个客户端分配独立密钥，让连接清晰可控。"],
  test: ["连接测试", "CONNECTION LAB", "从当前账号发起请求，确认模型能否正常响应。"],
  guide: ["接入指南", "GET CONNECTED", "从导入凭据到客户端接入，只需几步。"]
};
const esc = v => String(v ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const stamp = ts => ts ? new Date(ts).toLocaleString("zh-CN", {hour12:false}) : "未提供";
function toast(message) { $("toast").textContent = message; $("toast").hidden = false; clearTimeout(toast.timer); toast.timer = setTimeout(() => $("toast").hidden = true, 4500); }
function showLogin() {
  workspaceGeneration++;
  clearModelCatalog();
  csrf = ""; overview = null; $("workspace").hidden = true; $("login").hidden = false; $("boot").hidden = true;
  document.querySelectorAll("dialog[open]").forEach(d => d.close()); $("admin-key").value = "";
}
async function api(path, options = {}) {
  const method = options.method || "GET";
  const headers = {"Content-Type":"application/json", ...(method !== "GET" ? {"X-CSRF-Token":csrf} : {})};
  const response = await fetch("/admin/api/" + path, {method, headers, credentials:"same-origin", body:options.body === undefined ? undefined : JSON.stringify(options.body)});
  let data; try { data = await response.json(); } catch { throw new Error("服务器暂时不可用，请稍后重试"); }
  if (!response.ok) {
    if (response.status === 401 && path !== "login") showLogin();
    throw new Error(typeof data.detail === "string" ? data.detail : "操作失败，请检查输入后重试");
  }
  return data;
}
function goPage(next) {
  if (!(next in labels)) next = "dashboard";
  page = next;
  document.querySelectorAll(".page-panel").forEach(el => el.hidden = el.id !== "page-" + next);
  document.querySelectorAll("[data-page]").forEach(el => { el.classList.toggle("selected", el.dataset.page === next); el.setAttribute("aria-current", el.dataset.page === next ? "page" : "false"); });
  const [title, kicker, desc] = labels[next];
  $("page-title").replaceChildren(document.createTextNode(title));
  const dot = document.createElement("span"); dot.className = "title-dot"; dot.textContent = "."; $("page-title").append(dot);
  $("breadcrumb").textContent = next === "dashboard" ? "概览" : title; $("page-kicker").textContent = kicker; $("page-desc").textContent = desc;
  history.replaceState(null, "", "#" + next);
  $("refresh").innerHTML = next === "models" ? "<span>↻</span> 刷新模型" : "<span>↻</span> 刷新状态";
  if (next === "models") {
    syncModelAccount();
    renderModelCatalog();
    loadModelCatalog();
  } else {
    $("refresh").disabled = false;
  }
}
function render() {
  const {accounts, keys, models, uptime, events} = overview;
  const active = accounts.find(a => a.active && a.enabled);
  renderDashboard();
  $("account-count").textContent = accounts.length; $("account-badge").textContent = accounts.length;
  const rotating = overview.pool?.routing === "round_robin";
  $("active-name").textContent = rotating ? "账号池轮转" : active?.name || "未选择账号";
  $("active-state").textContent = rotating ? "新请求轮流分配，跳过暂停与冷却账号" : "新请求使用手动指定账号";
  $("test-account").textContent = active?.name || "尚未选择";
  $("uptime").textContent = uptime < 60 ? "已启动不到 1 分钟" : `持续运行 ${Math.floor(uptime / 3600)} 小时 ${Math.floor(uptime % 3600 / 60)} 分钟`;
  $("accounts-empty").hidden = accounts.length > 0;
  renderAccounts();
  const count = status => accounts.filter(a => a.pool_state === status).length;
  $("pool-counts").textContent = `全部 ${accounts.length}  ·  可用 ${count("available")}  ·  冷却 ${count("cooling")}  ·  暂停 ${count("paused")}  ·  耗尽 ${count("exhausted")}`;
  const known = accounts.filter(a => a.remaining !== null && a.remaining !== undefined);
  $("total-credits").textContent = known.length ? known.reduce((s,a) => s + a.remaining, 0).toLocaleString("zh-CN", {maximumFractionDigits:2}) + (known.length < accounts.length ? "（部分）" : "") : "待查询";
  if (!$("pool-settings").contains(document.activeElement)) {
    $("pool-routing").value = overview.pool?.routing || "manual"; $("auto-checkin").checked = overview.pool?.auto_checkin || false; $("checkin-time").value = overview.pool?.checkin_time || "09:00";
  }
  $("keys-body").innerHTML = keys.map(k => `<tr><td><strong>${esc(k.name)}</strong></td><td><code>${esc(k.hint)}</code></td><td class="muted mono">${esc(stamp(k.created * 1000))}</td><td class="align-right"><div class="actions"><button class="danger" data-revoke="${k.id}">撤销</button></div></td></tr>`).join("");
  const selected = $("model").value || "deepseek-v4-flash";
  $("model").replaceChildren(...models.map(model => { const o = document.createElement("option"); o.value = o.textContent = model; return o; }));
  if (models.includes(selected)) $("model").value = selected;
  $("test-submit").disabled = !active || busy;
  $("test-history").innerHTML = events.length ? events.map(e => `<div class="history-row"><span class="mono muted">${esc(stamp(e.time * 1000))}</span><strong>${esc(e.model)}</strong><span class="pill ${e.ok ? "green" : "red"}">${e.ok ? "成功" : "失败"}</span><span class="mono">${e.seconds}s</span></div>`).join("") : '<p class="history-empty">暂无测试记录，发送第一条测试消息。</p>';
  const modelAccountChanged = syncModelAccount();
  renderModelCatalog();
  if (page === "models" && modelAccountChanged) loadModelCatalog();
}
function renderDashboard() {
  const {accounts, metrics:m = {}, pool} = overview;
  const fmt = value => Number(value).toLocaleString("zh-CN",{maximumFractionDigits:2});
  const rate = value => value === null || value === undefined ? "—" : value.toFixed(1) + "%";
  $("dash-available").textContent = accounts.filter(a=>a.pool_state === "available").length + " / " + accounts.length;
  const known = accounts.filter(a=>typeof a.remaining === "number");
  $("dash-credits").textContent = known.length ? fmt(known.reduce((s,a)=>s+a.remaining,0)) : "—";
  $("dash-credit-note").textContent = known.length < accounts.length ? `已查询 ${known.length}/${accounts.length} 个账号，余额可能不完整` : accounts.some(a=>a.credits_stale) ? "包含待刷新余额，以最近一次查询为准" : "以最近一次上游查询为准";
  $("dash-requests").textContent = fmt(m.completed || 0);
  $("dash-request-note").textContent = `API ${m.api_count || 0} · 后台测试 ${m.test_count || 0}`;
  $("dash-success").textContent = rate(m.success_rate); $("dash-http").textContent = rate(m.http_success_rate);
  $("dash-failed").textContent = m.failed || 0; $("dash-inflight").textContent = m.in_flight || 0;
  $("dash-latency").textContent = m.avg_duration_ms === null || m.avg_duration_ms === undefined ? "—" : fmt(m.avg_duration_ms) + " ms";
  $("dash-since").textContent = "统计开始于 " + stamp((m.started_at || 0)*1000);
  $("dash-routing").textContent = pool?.routing === "round_robin" ? "轮流分配请求，自动跳过不可用账号" : "手动指定账号模式";
  const states={available:"可用",paused:"已暂停",cooling:"冷却中",exhausted:"积分耗尽",invalid:"凭据异常"};
  $("dash-account-list").innerHTML = accounts.slice(0,8).map(a=>`<div class="dashboard-account"><span class="account-icon">${esc(a.name.slice(0,1))}</span><div><strong>${esc(a.name)}</strong><small class="cell-note">${esc(a.uid || a.nickname)}</small></div><div class="dashboard-account-credit"><strong>${a.remaining === null || a.remaining === undefined ? "待查询" : fmt(a.remaining)}</strong><small class="cell-note">积分</small></div><span class="pill ${a.pool_state === 'available' ? 'green' : 'amber'}">${states[a.pool_state] || '待查询'}</span></div>`).join("") || '<p class="history-empty">尚未添加账号，点击“添加账号”开始。</p>';
  if(accounts.length > 8) $("dash-account-list").insertAdjacentHTML("beforeend",'<p class="muted">更多账号请前往账号池查看。</p>');
  const outcomes={success:"完成",stream_error:"流式错误",interrupted:"未完整结束",http_error:"请求失败"};
  $("dash-recent-body").innerHTML=(m.recent || []).map(r=>`<tr><td class="mono muted">${esc(stamp(r.time*1000))}</td><td>${r.source === 'test' ? '后台测试' : 'API'}<small class="cell-note mono">${esc(r.path)}</small></td><td><span class="pill ${r.ok ? 'green' : 'red'}">${r.status ?? '—'}</span><small class="cell-note">${outcomes[r.outcome] || '请求失败'}</small></td><td class="align-right mono">${fmt(r.duration_ms)} ms</td></tr>`).join("") || '<tr><td colspan="4" class="history-empty">尚无请求记录。发起 API 调用或后台测试后，这里会自动更新。</td></tr>';
}
document.querySelectorAll("[data-dashboard-page]").forEach(b=>b.addEventListener("click",()=>goPage(b.dataset.dashboardPage)));
$("dash-add").addEventListener("click",()=>openAccount());
$("dash-base").textContent=location.origin+"/v1";
$("dash-copy").addEventListener("click",()=>copy(location.origin+"/v1"));
function renderAccounts() {
  const query = $("account-search").value.toLowerCase(), filter = $("account-filter").value;
  const rows = overview.accounts.filter(a => (!query || [a.name,a.nickname,a.uid].join(" ").toLowerCase().includes(query)) && (filter === "all" || a.pool_state === filter));
  const labels = {available:["可用","green"],cooling:["冷却中","amber"],paused:["已暂停",""],exhausted:["积分耗尽","amber"],invalid:["凭据异常","red"]};
  $("accounts-body").innerHTML = rows.map(a => {
    const status = labels[a.pool_state] || ["待查询", ""];
    const credits = a.remaining === null || a.remaining === undefined ? "—" : Number(a.remaining).toLocaleString("zh-CN",{maximumFractionDigits:2});
    return `<tr><td><div class="account-cell"><span class="account-icon">${esc(a.name.slice(0,1))}</span><div><strong>${esc(a.name)}${a.active ? '<span class="mini-active">手动 / 测试账号</span>' : ""}</strong><small>${esc(a.uid || a.nickname)}</small></div></div></td><td><span class="pill ${status[1]}">${status[0]}</span><small class="cell-note">${a.today_checked_in ? "今日已签到" : "今日未确认签到"}</small>${a.cooldown_until > Date.now()/1000 ? `<small class="cell-note">至 ${esc(stamp(a.cooldown_until*1000))}</small>` : ""}</td><td><strong class="credit-number">${credits}</strong><small class="cell-note">${a.credits_updated ? esc(stamp(a.credits_updated*1000)) : "点击查询积分"}${a.credits_stale && a.credits_updated ? " · 待刷新" : ""}</small>${a.last_error ? `<small class="cell-note field-error">${esc(a.last_error)}</small>` : ""}</td><td class="mono">${esc(stamp(a.expires_at))}<small class="cell-note">${a.expired ? "已到期 · 调用时尝试刷新" : "支持自动刷新"}</small></td><td><div class="actions pool-actions">${a.enabled ? `<button data-action="status" data-id="${a.id}" title="查询积分与签到状态">查询积分</button><button data-action="checkin" data-id="${a.id}">签到</button><button data-action="refresh" data-id="${a.id}">刷新凭据</button>` : ""}${a.enabled && !a.active ? `<button class="switch" data-action="activate" data-id="${a.id}">设为手动 / 测试</button>` : ""}<button data-action="rename" data-id="${a.id}">备注</button><button data-action="toggle" data-id="${a.id}">${a.enabled ? "暂停" : "恢复"}</button><button class="danger" data-action="delete" data-id="${a.id}">删除</button></div></td></tr>`;
  }).join("") || (overview.accounts.length ? '<tr><td colspan="5" class="muted">没有符合筛选条件的账号。</td></tr>' : "");
}
$("account-search").addEventListener("input", () => { if(overview) renderAccounts(); });
$("account-filter").addEventListener("change", () => { if(overview) renderAccounts(); });

function selectedModelAccount() {
  return overview?.accounts.find(account => account.active && account.enabled) || null;
}

function clearModelCatalog(accountId = null) {
  modelGeneration++;
  modelAccountId = accountId;
  modelCatalog = null;
  modelLoading = false;
  modelError = "";
  $("models-body").replaceChildren();
}

function syncModelAccount() {
  const accountId = selectedModelAccount()?.id || null;
  if (accountId === modelAccountId) return false;
  clearModelCatalog(accountId);
  return true;
}

function creditNumber(value) {
  if (value === null || value === undefined) return null;
  const normalized = String(value).trim().replace(/^[x×]\s*/i, "").replace(/\s*[x×]$/i, "");
  if (!/^\d+(?:\.\d+)?$/.test(normalized)) return null;
  const result = Number(normalized);
  return Number.isFinite(result) ? result : null;
}

function creditLabel(value) {
  if (value === null || value === undefined || String(value).trim() === "") return "未标注";
  if (creditNumber(value) === null) return String(value).trim();
  return "x" + String(value).trim().replace(/^[x×]\s*/i, "").replace(/\s*[x×]$/i, "");
}

function hasFreePromotion(model) {
  return model.promotion?.active === true && creditNumber(model.promotion.discounted_credits) === 0;
}

function modelReasoningEfforts(model) {
  return Array.isArray(model.reasoning_efforts) ? model.reasoning_efforts.filter(effort => typeof effort === "string" && effort) : [];
}

function modelPiConfig(model) {
  // JSON-quoted strings are valid YAML and keep values such as "off" as strings.
  const quoted = value => JSON.stringify(String(value));
  const credits = model.effective_credits || model.credits;
  const rate = creditNumber(credits) === null ? credits : creditLabel(credits).slice(1) + "x";
  const promotion = model.promotion?.active ? model.promotion.label || (hasFreePromotion(model) ? "当前限免" : "") : "";
  const suffix = [rate, promotion].filter(Boolean).join(" ");
  const name = (model.name || model.id) + (suffix ? ` (${suffix})` : "");
  const lines = [`- id: ${quoted(model.id)}`, `  name: ${quoted(name)}`];
  for (const [field, value] of [["contextWindow", model.max_input_tokens], ["maxTokens", model.max_output_tokens]]) {
    if (Number.isFinite(value) && value >= 0) lines.push(`  ${field}: ${value}`);
  }
  lines.push("  input:", "    - text");
  if (model.supports_images) lines.push("    - image");
  const efforts = modelReasoningEfforts(model);
  if (efforts.length) {
    lines.push("  reasoningEfforts:");
    for (const effort of efforts) lines.push(`    ${quoted(effort)}: ${quoted(effort)}`);
  }
  return lines.join("\n") + "\n";
}

function renderModelCapabilities(model) {
  const limits = [["最大输入 tokens", model.max_input_tokens], ["最大输出 tokens", model.max_output_tokens]];
  const details = limits.filter(([, value]) => Number.isFinite(value) && value >= 0)
    .map(([label, value]) => `<div><dt>${label}</dt><dd>${esc(value.toLocaleString("en-US", {maximumFractionDigits: 20}))}</dd></div>`);
  const efforts = modelReasoningEfforts(model);
  if (efforts.length) {
    const values = efforts.map(esc).join(", ");
    details.push(`<div><dt>思考强度</dt><dd>${values}</dd></div>`);
  }
  return details.length ? `<dl class="model-capability-details">${details.join("")}</dl>` : "";
}

function renderModelRow(model) {
  const promotion = model.promotion;
  const capabilities = [model.supports_images ? "支持图片" : "", model.supports_reasoning ? "支持推理" : ""].filter(Boolean);
  const routeLabel = model.is_dynamic_route === true ? '<span class="pill" title="根据路由策略动态选择实际模型">动态模型路由</span>' : "";
  let promotionHtml = '<span class="muted">暂无优惠配置</span>';
  if (promotion) {
    const label = promotion.label || (hasFreePromotion(model) ? "当前限免" : "优惠活动");
    promotionHtml = `<span class="pill ${promotion.active ? "green" : ""}">${esc(label)}</span>${promotion.active ? "" : '<span class="model-promotion-state">未生效</span>'}`;
    if (promotion.description) promotionHtml += `<small class="model-promotion-description">${esc(promotion.description)}</small>`;
    if (promotion.schedule_text) promotionHtml += `<small class="model-promotion-schedule">${esc(promotion.schedule_text)}</small>`;
  }
  return `<tr>
    <td data-label="模型 / ID"><div class="model-identity"><div class="model-name-row"><strong>${esc(model.name || model.id)}</strong>${routeLabel}</div>${model.description ? `<small class="model-description">${esc(model.description)}</small>` : ""}<div class="model-id-row"><code>${esc(model.id)}</code><button class="copy-button" data-model-copy="${esc(model.id)}" aria-label="复制模型 ID ${esc(model.id)}">复制</button><button class="copy-button" data-model-config-copy="${esc(model.id)}" aria-label="复制配置 ${esc(model.id)}">复制配置</button></div>${capabilities.length ? `<small class="model-capabilities">${capabilities.join(" · ")}</small>` : ""}${renderModelCapabilities(model)}</div></td>
    <td data-label="消费倍率"><div class="model-rate"><strong>${esc(creditLabel(model.effective_credits))}</strong><small>原始 ${esc(creditLabel(model.credits))}</small></div></td>
    <td data-label="优惠"><div class="model-promotion">${promotionHtml}</div></td>
  </tr>`;
}

function renderModelCatalog() {
  const account = selectedModelAccount();
  const catalog = account && modelAccountId === account.id ? modelCatalog : null;
  $("models-account").textContent = account?.name || "未选择账号";
  $("models-updated").textContent = catalog?.fetched_at ? "上次同步 " + stamp(catalog.fetched_at * 1000) : "尚未同步远端配置";
  $("models-content").setAttribute("aria-busy", String(modelLoading));
  $("models-search").disabled = $("models-filter").disabled = !catalog;
  if (page === "models") $("refresh").disabled = !account || modelLoading;

  const warning = modelError || catalog?.error || (catalog?.stale ? "当前为上次成功获取的配置，请刷新后确认最新优惠。" : "");
  $("models-error").hidden = !warning;
  $("models-error").textContent = warning ? (catalog ? "刷新未完成，保留上次配置。" : "") + warning : "";
  const status = modelLoading ? [catalog ? "正在更新" : "正在同步", "amber"] : warning ? [catalog ? "缓存待刷新" : "查询失败", "amber"] : catalog ? ["已同步", "green"] : ["等待查询", ""];
  $("models-sync-state").textContent = status[0];
  $("models-sync-state").className = "pill " + status[1];

  const models = catalog?.models || [];
  const query = $("models-search").value.trim().toLowerCase();
  const filter = $("models-filter").value;
  const rows = models.filter(model => {
    const matchesQuery = !query || [model.name, model.id].join(" ").toLowerCase().includes(query);
    const matchesFilter = filter === "all" || (filter === "offers" ? Boolean(model.promotion) : hasFreePromotion(model));
    return matchesQuery && matchesFilter;
  });
  $("models-count").textContent = String(models.length);
  $("models-catalog-note").textContent = catalog ? `显示 ${rows.length} / ${models.length} 个模型` : "";
  $("models-table").hidden = !rows.length;
  $("models-body").innerHTML = rows.map(renderModelRow).join("");
  $("models-empty").hidden = rows.length > 0;
  $("models-empty-action").hidden = false;
  if (!account) {
    $("models-empty-title").textContent = "先选择查询账号";
    $("models-empty-description").textContent = "在账号池中选择一个已启用的“手动 / 测试”账号，即可读取模型配置。";
    $("models-empty-action").textContent = "前往账号池";
  } else if (modelLoading && !catalog) {
    $("models-empty-title").textContent = "正在读取模型配置";
    $("models-empty-description").textContent = "正在获取当前账号的模型、倍率与优惠信息…";
    $("models-empty-action").hidden = true;
  } else if (!catalog) {
    $("models-empty-title").textContent = modelError ? "暂时无法获取模型" : "尚未获取模型配置";
    $("models-empty-description").textContent = "可重试查询，或前往账号池检查当前账号状态。";
    $("models-empty-action").textContent = "重试查询";
  } else if (!models.length) {
    $("models-empty-title").textContent = "当前配置没有可展示的模型";
    $("models-empty-description").textContent = "上游未返回符合免费账号范围且标注费率的模型，可以稍后刷新。";
    $("models-empty-action").textContent = "刷新模型";
  } else {
    $("models-empty-title").textContent = "没有符合条件的模型";
    $("models-empty-description").textContent = "试试其他名称或 ID，或者清除筛选条件。";
    $("models-empty-action").textContent = "清除筛选";
  }
}

async function loadModelCatalog(force = false) {
  syncModelAccount();
  const account = selectedModelAccount();
  if (!csrf || !account || page !== "models" || modelLoading) return;
  const generation = ++modelGeneration;
  const accountId = account.id;
  modelLoading = true;
  modelError = "";
  renderModelCatalog();
  try {
    const result = await api(force ? "models/refresh" : "models", force ? {method:"POST"} : {});
    if (generation !== modelGeneration || selectedModelAccount()?.id !== accountId || !csrf) return;
    if (result.account?.id !== accountId) {
      modelCatalog = null;
      modelError = "查询账号已变更，请稍后刷新。";
      await refresh();
      return;
    }
    modelCatalog = result;
  } catch (error) {
    if (generation !== modelGeneration || selectedModelAccount()?.id !== accountId || !csrf) return;
    modelError = error.message;
  } finally {
    if (generation === modelGeneration) {
      modelLoading = false;
      renderModelCatalog();
    }
  }
}

$("models-choose-account").addEventListener("click", () => goPage("accounts"));
$("models-search").addEventListener("input", renderModelCatalog);
$("models-filter").addEventListener("change", renderModelCatalog);
$("models-empty-action").addEventListener("click", () => {
  if (!selectedModelAccount()) return goPage("accounts");
  if (modelCatalog?.models.length) {
    $("models-search").value = "";
    $("models-filter").value = "all";
    renderModelCatalog();
  } else {
    loadModelCatalog(true);
  }
});
$("models-body").addEventListener("click", event => {
  const button = event.target.closest("[data-model-copy]");
  if (button) return copy(button.dataset.modelCopy);
  const configButton = event.target.closest("[data-model-config-copy]");
  const model = configButton && modelCatalog?.models.find(model => model.id === configButton.dataset.modelConfigCopy);
  if (model) copy(modelPiConfig(model));
});

async function refresh() {
  const generation = workspaceGeneration;
  const requestGeneration = ++overviewGeneration;
  $("refresh").disabled = true;
  try {
    const result = await api("overview");
    if (generation !== workspaceGeneration || requestGeneration !== overviewGeneration || !csrf) return;
    overview = result;
    render();
    $("load-error").hidden = true;
  }
  catch (e) {
    if (generation !== workspaceGeneration || requestGeneration !== overviewGeneration || !csrf) return;
    $("load-error").textContent = e.message;
    $("load-error").hidden = false;
    throw e;
  }
  finally { $("refresh").disabled = page === "models" ? !selectedModelAccount() || modelLoading : false; }
}
async function enter() { $("boot").hidden = true; $("login").hidden = true; $("workspace").hidden = false; goPage(location.hash.slice(1)); await refresh(); }
$("login-form").addEventListener("submit", async e => {
  e.preventDefault(); const button = e.submitter; button.disabled = true; $("login-error").textContent = "";
  try { const data = await api("login", {method:"POST",body:{key:$("admin-key").value.trim()}}); csrf = data.csrf; $("admin-key").value = ""; await enter(); }
  catch (error) { $("login-error").textContent = error.message; }
  finally { button.disabled = false; }
});
$("logout").addEventListener("click", async () => {
  const request = api("logout", {method:"POST"});
  showLogin();
  renderModelCatalog();
  try { await request; } catch (e) { toast(e.message); }
});
async function batchAction(action) {
  $("refresh").disabled = $("batch-checkin").disabled = true;
  try {
    const result = await api("pool/actions/"+action,{method:"POST"});
    $("pool-result").hidden = false;
    $("pool-result").textContent = result.results.map(r => `${overview.accounts.find(a=>a.id===r.id)?.name || "账号"}：${r.message}`).join("；") || "没有启用的账号";
    await refresh();
  } catch(e) { toast(e.message); } finally { $("refresh").disabled = $("batch-checkin").disabled = false; }
}
$("refresh").addEventListener("click", () => {
  if (page === "models") return loadModelCatalog(true);
  if (page === "accounts") return batchAction("status");
  refresh().catch(e => toast(e.message));
});
$("batch-checkin").addEventListener("click", () => batchAction("checkin"));
$("pool-settings").addEventListener("submit", async e => { e.preventDefault();e.submitter.disabled=true;try {await api("pool/settings",{method:"PATCH",body:{routing:$("pool-routing").value,auto_checkin:$("auto-checkin").checked,checkin_time:$("checkin-time").value}});await refresh();toast("账号池设置已保存");}catch(err){toast(err.message);}finally{e.submitter.disabled=false;} });
setInterval(async () => {
  if (!csrf || !overview || document.hidden || document.querySelector("dialog[open]")) return;
  if (page !== "models" && $("refresh").disabled) return;
  try {
    await refresh();
    if (page === "models") await loadModelCatalog();
  } catch {}
}, 30000);
document.querySelectorAll("[data-page]").forEach(b => b.addEventListener("click", () => goPage(b.dataset.page)));
$("go-guide").addEventListener("click", () => goPage("guide"));
document.querySelectorAll(".close-dialog").forEach(b => b.addEventListener("click", () => b.closest("dialog").close()));
function clearOAuth() {
  oauthGeneration++; clearTimeout(oauthTimer);
  const previous = oauthFlow; oauthFlow = null;
  if (previous && csrf) api("oauth/" + previous.id, {method:"DELETE"}).catch(() => {});
  $("oauth-link-box").hidden = true; $("oauth-open").removeAttribute("href");
  $("oauth-start").disabled = false; $("oauth-start").textContent = "生成登录链接 ↗";
  $("oauth-retry").hidden = true; $("account-name").disabled = false;
  $("oauth-status").textContent = "登录链接 5 分钟内有效。同一账号重新登录会更新凭据。";
  $("oauth-status").className = "banner oauth-status";
}
function setAccountMode(mode) {
  clearOAuth(); accountMode = mode;
  $("browser-login-panel").hidden = mode !== "browser";
  $("file-import-panel").hidden = $("account-import").hidden = mode !== "file";
  $("mode-browser").setAttribute("aria-pressed", String(mode === "browser"));
  $("mode-file").setAttribute("aria-pressed", String(mode === "file"));
  $("account-error").textContent = "";
}
$("mode-browser").addEventListener("click", () => setAccountMode("browser"));
$("mode-file").addEventListener("click", () => setAccountMode("file"));
$("account-dialog").addEventListener("close", () => { clearOAuth(); $("account-form").reset(); $("file-label").textContent = "选择或拖入 .info / .json 文件"; $("account-error").textContent = ""; });
function openAccount() { setAccountMode("browser"); $("account-dialog").showModal(); }
async function pollOAuth(generation) {
  if (generation !== oauthGeneration || !oauthFlow) return;
  const flow = oauthFlow;
  if (Date.now() >= flow.expires_at) { clearOAuth(); $("oauth-status").textContent = "登录链接已过期，请重新生成。"; return; }
  try {
    const result = await api("oauth/" + flow.id + "/poll", {method:"POST"});
    if (generation !== oauthGeneration) return;
    if (result.status === "success") {
      $("account-dialog").close(); await refresh();
      toast(result.updated ? "授权成功，账号凭据已更新" : "授权成功，账号已添加；可在列表中切换使用"); return;
    }
    if (result.status === "expired") { clearOAuth(); $("oauth-status").textContent = "登录链接已过期，请重新生成。"; return; }
    $("oauth-status").className = "banner oauth-status";
    $("oauth-status").textContent = `等待你在官方页面完成登录… 链接剩余 ${Math.max(1, Math.ceil((flow.expires_at - Date.now()) / 1000))} 秒。`;
    oauthTimer = setTimeout(() => pollOAuth(generation), 3000);
  } catch (e) {
    if (generation !== oauthGeneration) return;
    $("oauth-status").className = "banner warning oauth-status"; $("oauth-status").textContent = e.message;
    $("oauth-retry").hidden = false;
  }
}
$("oauth-start").addEventListener("click", async () => {
  clearOAuth(); const generation = oauthGeneration;
  $("oauth-start").disabled = true; $("oauth-start").textContent = "正在生成…";
  $("account-error").textContent = "";
  try {
    const flow = await api("oauth/start", {method:"POST",body:{name:$("account-name").value.trim() || undefined}});
    if (generation !== oauthGeneration) { api("oauth/" + flow.id, {method:"DELETE"}).catch(() => {}); return; }
    oauthFlow = flow; $("oauth-open").href = flow.url; $("oauth-link-box").hidden = false;
    $("oauth-start").textContent = "重新生成链接"; $("account-name").disabled = true;
    $("oauth-status").textContent = "登录链接已就绪，请打开官方页面完成登录。";
    oauthTimer = setTimeout(() => pollOAuth(generation), 3000);
  } catch (e) { if (generation === oauthGeneration) $("account-error").textContent = e.message; }
  finally { if (generation === oauthGeneration) $("oauth-start").disabled = false; }
});
$("oauth-copy").addEventListener("click", () => { if (oauthFlow) copy(oauthFlow.url); });
$("oauth-retry").addEventListener("click", () => { $("oauth-retry").hidden = true; pollOAuth(oauthGeneration); });
$("add-account").addEventListener("click", openAccount); $("empty-add").addEventListener("click", openAccount);
async function readFile(file) {
  if (!file) return;
  if (file.size > 1024 * 1024) throw new Error("文件超过 1 MB，请选择登录凭据文件");
  $("credential-json").value = (await file.text()).replace(/^\uFEFF/, "");
  $("file-label").textContent = file.name;
}
$("credential-file").addEventListener("change", e => { readFile(e.target.files[0]).catch(err => $("account-error").textContent = err.message); });
$("file-drop").addEventListener("dragover", e => { e.preventDefault(); $("file-drop").classList.add("drag-over"); });
$("file-drop").addEventListener("dragleave", () => $("file-drop").classList.remove("drag-over"));
$("file-drop").addEventListener("drop", e => { e.preventDefault(); $("file-drop").classList.remove("drag-over"); readFile(e.dataTransfer.files[0]).catch(err => $("account-error").textContent = err.message); });
$("account-form").addEventListener("submit", async e => {
  if (accountMode !== "file") { e.preventDefault(); return; }
  e.preventDefault(); e.submitter.disabled = true; $("account-error").textContent = "";
  try {
    let credential; try { credential = JSON.parse($("credential-json").value); } catch { throw new Error("请选择文件或粘贴有效的 JSON 内容"); }
    await api("accounts", {method:"POST", body:{name:$("account-name").value.trim() || undefined, credential}});
    $("account-dialog").close(); await refresh(); toast("账号已导入");
  } catch (err) { $("account-error").textContent = err.message; } finally { e.submitter.disabled = false; }
});
function confirmAction(title, desc, callback, rename = null) {
  $("confirm-title").textContent = title; $("confirm-desc").textContent = desc; $("confirm-error").textContent = "";
  $("rename-label").hidden = $("rename-value").hidden = rename === null; $("rename-value").value = rename || "";
  pendingConfirm = callback; $("confirm-dialog").showModal();
}
$("confirm-form").addEventListener("submit", async e => { e.preventDefault(); e.submitter.disabled = true; try { await pendingConfirm(); $("confirm-dialog").close(); await refresh(); toast("操作已保存"); } catch (err) { $("confirm-error").textContent = err.message; } finally { e.submitter.disabled = false; } });
$("accounts-body").addEventListener("click", async e => {
  const b = e.target.closest("[data-action]"); if (!b) return;
  const a = overview.accounts.find(x => x.id === b.dataset.id); if (!a) return;
  if (["status","checkin","refresh"].includes(b.dataset.action)) {
    b.disabled = true;
    try { const result=await api(`accounts/${a.id}/actions/${b.dataset.action}`,{method:"POST"});toast(result.message);await refresh(); }
    catch(err){toast(err.message);} finally {b.disabled=false;} return;
  }
  const update = async body => {
    const result = await api("accounts/" + a.id, {method:"PATCH", body});
    overviewGeneration++;
    if (body.active || (a.active && body.enabled !== undefined)) clearModelCatalog();
    return result;
  };
  if (b.dataset.action === "activate") confirmAction("设置手动 / 测试账号？", `设为「${a.name}」。仅在手动调度模式和连接测试中使用；账号池轮转模式仍自动分配。`, () => update({active:true}));
  if (b.dataset.action === "rename") confirmAction("编辑账号备注", "备注仅用于在管理后台识别账号。", () => update({name:$("rename-value").value}), a.name);
  if (b.dataset.action === "toggle") confirmAction(a.enabled ? "暂停这个账号？" : "恢复这个账号？", "暂停后不参与新请求分配和自动签到，已开始的请求继续完成。恢复后重新参与轮转。", () => update({enabled:!a.enabled}));
  if (b.dataset.action === "delete") confirmAction("删除这个账号？", `「${a.name}」将从账号列表移除。服务器保留恢复副本。${a.active ? "当前调用账号将被清空。" : ""}`, async () => {
    await api("accounts/" + a.id, {method:"DELETE"});
    overviewGeneration++;
    if (a.active) clearModelCatalog();
  });
});
$("keys-body").addEventListener("click", e => { const b = e.target.closest("[data-revoke]"); if (!b) return; const k = overview.keys.find(x => x.id === b.dataset.revoke); confirmAction("撤销客户端密钥？", `使用「${k.name}」的客户端将无法发起新请求。此操作不可撤销。`, () => api("keys/" + k.id, {method:"DELETE"})); });
$("add-key").addEventListener("click", () => { $("key-form").hidden = false; $("created-key").hidden = true; $("key-dialog").showModal(); });
$("key-dialog").addEventListener("close", () => { $("key-form").reset(); $("new-key-value").textContent = ""; $("key-error").textContent = ""; });
$("key-form").addEventListener("submit", async e => { e.preventDefault(); e.submitter.disabled = true; try { const d = await api("keys", {method:"POST",body:{name:$("key-name").value}}); $("key-form").hidden = true; $("created-key").hidden = false; $("new-key-value").textContent = d.key; await refresh(); } catch (err) { $("key-error").textContent = err.message; } finally { e.submitter.disabled = false; } });
async function copy(value) { try { await navigator.clipboard.writeText(value); toast("已复制"); } catch { toast("无法自动复制，请手动选择文本复制"); } }
$("copy-key").addEventListener("click", () => copy($("new-key-value").textContent));
document.querySelectorAll("[data-copy]").forEach(b => b.addEventListener("click", () => copy(b.dataset.copy)));
$("base-url").textContent = location.origin + "/v1"; $("anthropic-url").textContent = location.origin;
$("copy-base").addEventListener("click", () => copy(location.origin + "/v1")); $("copy-anthropic").addEventListener("click", () => copy(location.origin));
$("host-label").textContent = location.host;
$("test-form").addEventListener("submit", async e => {
  e.preventDefault(); busy = true; $("test-submit").disabled = true; $("test-submit").textContent = "正在调用…"; $("test-status").className = "pill amber"; $("test-status").textContent = "请求中"; $("test-output").textContent = "正在等待上游响应，最长约 90 秒…"; $("test-meta").textContent = "";
  try { const r = await api("test", {method:"POST",body:{model:$("model").value,prompt:$("test-prompt").value}}); $("test-status").className = "pill " + (r.ok ? "green" : "red"); $("test-status").textContent = r.ok ? "连接成功" : "调用失败"; $("test-output").textContent = r.ok ? r.answer : r.error; $("test-meta").textContent = `${r.seconds}s${r.status ? " · HTTP " + r.status : ""}${r.usage?.total_tokens !== undefined ? " · " + r.usage.total_tokens + " tokens" : ""}`; }
  catch (err) { $("test-status").className = "pill red"; $("test-status").textContent = "请求失败"; $("test-output").textContent = err.message; }
  finally { busy = false; $("test-submit").textContent = "发送测试 ↗"; await refresh().catch(() => {}); }
});
(async () => { try { const s = await api("session"); csrf = s.csrf; await enter(); } catch (e) { if (!csrf) showLogin(); } })();

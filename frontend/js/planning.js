/* PlanPilot 前端 · planning
   计划中心、方案对比、风险与版本撤销
   ---------------------------------------------------------
   由 app.js 拆分而来。按业务域归档，加载顺序见 index.html；
   所有函数仍是全局声明（页面有 107 处内联 onclick/onchange 依赖它们）。
   ========================================================= */

function setPlanningScope(scope) {
  state.planningScope = scope;
  const now = new Date();
  if (scope === "upcoming") {
    $("planStart").value = toDateTimeLocalValue(startOfDay(now));
    $("planDays").value = 7;
  } else if (scope === "week") {
    syncPlanningWeekRange();
  }
  for (const name of ["upcoming", "week"]) {
    const button = $(`planScope${name[0].toUpperCase()}${name.slice(1)}`);
    button?.classList.toggle("active", name === scope);
  }
  closePlanComparison();
  closePlanAlternatives();
  renderPlanningOverview();
  renderTimetables();
}

function syncPlanningWeekRange() {
  const now = new Date();
  const weekStart = state.weekStart || startOfWeek(now);
  const weekEnd = addDays(weekStart, 7);
  const rangeStart = weekStart <= now && now < weekEnd ? now : weekStart;
  $("planStart").value = toDateTimeLocalValue(rangeStart);
  $("planDays").value = Math.max(
    1,
    Math.ceil((weekEnd - rangeStart) / (24 * 60 * 60 * 1000)),
  );
}

function setPlanningMode(mode) {
  const labels = {
    auto: "系统会根据任务数量、日期跨度、地点与约束自动选择",
    quick: "使用现有规则引擎，适合少量任务和简单约束",
    optimized: "使用 CP-SAT 全局求解，适合一周计划和复杂约束",
  };
  const input = $("planMode");
  if (input) {
    input.value = mode;
  }
  for (const name of ["auto", "quick", "optimized"]) {
    const button = $(`planMode${name[0].toUpperCase()}${name.slice(1)}`);
    button?.classList.toggle("active", name === mode);
  }
  if ($("planModeHint")) {
    $("planModeHint").textContent = labels[mode] || labels.auto;
  }
  closePlanAlternatives();
}

function markPlanningScopeCustom() {
  // 用户手动改了 planStart / planDays 输入框；范围不再是任何预设。
  // planningRange() 在 scope 不是 upcoming/week 时回退到自定义的 start + days。
  if (state.planningScope !== "custom") {
    state.planningScope = "custom";
    for (const name of ["upcoming", "week"]) {
      const button = $(`planScope${name[0].toUpperCase()}${name.slice(1)}`);
      button?.classList.toggle("active", false);
    }
  }
  closePlanAlternatives();
  renderPlanningOverview();
}

function planningRange() {
  const rawStart = $("planStart")?.value;
  const start = rawStart ? new Date(rawStart) : new Date();
  const days = Math.max(1, Number($("planDays")?.value) || 1);
  const end = state.planningScope === "upcoming"
    ? addDays(startOfDay(new Date()), 7)
    : state.planningScope === "week"
      ? addDays(startOfWeek(start), 7)
      : addDays(start, days);
  return { start, end, days };
}

function renderPlanningOverview() {
  const container = $("planningOverview");
  if (!container) {
    return;
  }
  const { start, end } = planningRange();
  const schedules = state.schedules.filter((item) => {
    const itemStart = parseDate(item.start_time);
    return itemStart && itemStart >= start && itemStart < end
      && ["proposed", "accepted"].includes(item.status);
  });
  const arrangedTaskIds = new Set(schedules.map((item) => item.task_id));
  const pendingTasks = state.tasks.filter(
    (task) => ["pending", "in_progress"].includes(task.status)
      && !arrangedTaskIds.has(task.id),
  ).length;
  const minutes = schedules.reduce((total, item) => {
    const itemStart = parseDate(item.start_time);
    const itemEnd = parseDate(item.end_time);
    return total + (itemStart && itemEnd ? Math.max(0, (itemEnd - itemStart) / 60000) : 0);
  }, 0);
  const locked = schedules.filter((item) => item.is_locked).length;
  container.innerHTML = `
    <span><strong>${schedules.length}</strong> 个时间块</span>
    <span><strong>${(minutes / 60).toFixed(1)}</strong> 小时</span>
    <span><strong>${pendingTasks}</strong> 个待安排任务</span>
    <span><strong>${locked}</strong> 个已锁定</span>
  `;
}

async function generateSchedule() {
  const container = $("scheduleResult");
  container.innerHTML = `<div class="empty">正在规划…</div>`;

  try {
    const payload = collectPlanPayload();

    const result = await api("/schedules/generate", {
      method: "POST",
      body: JSON.stringify(payload),
    });

    state.lastPlanBatchId = result.batch_id || null;
    await loadAllData();
    renderSchedule(result);
    renderPlanAnalysis(result.analysis, "规划完成");
    closePlanComparison();
  } catch (error) {
    container.innerHTML = `
      <div class="empty error">
        规划失败：${escapeHtml(error.message)}
      </div>
    `;
  }
}

function collectPlanPayload() {
  const startValue = $("planStart").value;
  const payload = {
    days: Number($("planDays").value) || 7,
    planning_mode: $("planMode")?.value || "auto",
  };
  const gapValue = $("planGap").value;
  const strategy = $("planStrategy").value;
  if (gapValue !== "") {
    payload.gap_minutes = Number(gapValue);
  }
  if (strategy) {
    payload.strategy = strategy;
  }
  if (startValue) {
    payload.start = new Date(startValue).toISOString();
  }
  return payload;
}

async function previewSchedule() {
  try {
    const result = await api("/schedules/preview", {
      method: "POST",
      body: JSON.stringify(collectPlanPayload()),
    });
    renderPlanAnalysis(result.analysis, "预览结果");
    renderPlanComparison(result.comparison, "规划方案对比");
  } catch (error) {
    renderPlanAnalysis({ score: 0, issues: [error.message] }, "预览失败");
  }
}

async function loadPlanAlternatives() {
  const panel = $("planAlternatives");
  const grid = $("planAlternativesGrid");
  panel.classList.remove("hidden");
  grid.innerHTML = '<div class="empty">正在生成三套可行方案...</div>';
  panel.scrollIntoView({ behavior: "smooth", block: "nearest" });
  try {
    const payload = collectPlanPayload();
    const result = await api("/schedules/alternatives", {
      method: "POST",
      body: JSON.stringify(payload),
    });
    state.planAlternatives = result;
    state.planAlternativesRequest = payload;
    renderPlanAlternatives(result);
  } catch (error) {
    state.planAlternatives = null;
    state.planAlternativesRequest = null;
    grid.innerHTML = `<div class="empty error">方案生成失败：${escapeHtml(error.message)}</div>`;
  }
}

function renderPlanAlternatives(result) {
  const alternatives = result.alternatives || [];
  $("planAlternativesGrid").innerHTML = alternatives.map((alternative) => {
    const metrics = alternative.metrics || {};
    const schedules = alternative.schedules || [];
    const timeline = schedules.slice(0, 6).map((item) => {
      const task = state.tasks.find((candidate) => candidate.id === item.task_id);
      const taskMeta = task
        ? [
            `优先级：${getPriorityName(task.priority)}`,
            task.deadline ? `截止：${formatDateTime(task.deadline)}` : "无截止时间",
          ].join(" · ")
        : "";
      return `<li><span>${escapeHtml(planComparisonTime(item))}</span><div><strong>${escapeHtml(task?.title || `任务 #${item.task_id}`)}</strong><small>${escapeHtml(taskMeta)}</small></div></li>`;
    }).join("");
    const remaining = Math.max(0, schedules.length - 6);
    const strengths = (alternative.strengths || []).slice(0, 3);
    const tradeoffs = (alternative.tradeoffs || []).slice(0, 2);
    const unscheduled = (alternative.unscheduled || []).map(
      (item) => `<li><strong>${escapeHtml(item.title)}</strong><span>${escapeHtml(item.reason)}</span></li>`,
    ).join("");
    return `
      <article class="plan-alternative ${alternative.recommended ? "recommended" : ""}">
        <div class="plan-alternative-head">
          <div><h4>${escapeHtml(alternative.title)}</h4><p>${escapeHtml(alternative.summary)}</p></div>
          ${alternative.recommended ? '<span class="recommended-badge">推荐</span>' : ""}
        </div>
        <div class="plan-alternative-outcomes">${(alternative.outcome_labels || []).map((item) => `<span>${escapeHtml(item)}</span>`).join("")}</div>
        <dl class="plan-alternative-policy">
          <div><dt>优先规则</dt><dd>${escapeHtml(alternative.selection_policy || "")}</dd></div>
          <div><dt>安排节奏</dt><dd>${escapeHtml(alternative.pace || "")}</dd></div>
        </dl>
        <div class="plan-alternative-metrics">
          <div><strong>${escapeHtml(alternative.analysis?.score ?? 0)}</strong><span>计划评分</span></div>
          <div><strong>${escapeHtml(metrics.scheduled_tasks || 0)}</strong><span>已安排任务</span></div>
          <div><strong>${escapeHtml(formatMinutesCompact(metrics.max_daily_minutes || 0))}</strong><span>单日最高</span></div>
          <div><strong>${escapeHtml(metrics.days_used || 0)}</strong><span>占用天数</span></div>
          <div><strong>${escapeHtml(formatMinutesCompact(metrics.idle_minutes || 0))}</strong><span>任务间空档</span></div>
          <div><strong>${escapeHtml(metrics.changed_blocks || 0)}</strong><span>变更时间块</span></div>
        </div>
        ${alternative.recommendation_reason ? `<p class="plan-alternative-reason"><strong>推荐依据</strong>${escapeHtml(alternative.recommendation_reason)}</p>` : ""}
        <div class="plan-alternative-explanation">
          <div><strong>优势</strong><ul>${strengths.map((item) => `<li>${escapeHtml(item)}</li>`).join("")}</ul></div>
          <div><strong>取舍</strong><ul>${tradeoffs.map((item) => `<li>${escapeHtml(item)}</li>`).join("")}</ul></div>
        </div>
        <ol class="plan-alternative-timeline">${timeline}</ol>
        ${remaining ? `<p class="plan-alternative-more">另有 ${remaining} 个时间块</p>` : ""}
        ${unscheduled ? `<div class="plan-alternative-unscheduled"><strong>未安排</strong><ul>${unscheduled}</ul></div>` : ""}
        <div class="plan-alternative-actions">
          <button class="ghost-button" type="button" onclick="showAlternativeComparison('${alternative.profile}')">查看变更</button>
          <button class="primary-button" type="button" onclick="applyPlanAlternative('${alternative.profile}')">应用此方案</button>
        </div>
      </article>
    `;
  }).join("");
}

function formatMinutesCompact(minutes) {
  if (minutes < 60) {
    return `${minutes}分`;
  }
  const hours = Math.floor(minutes / 60);
  const rest = minutes % 60;
  return rest ? `${hours}时${rest}分` : `${hours}小时`;
}

function showAlternativeComparison(profile) {
  const alternative = state.planAlternatives?.alternatives?.find(
    (item) => item.profile === profile,
  );
  if (alternative) {
    renderPlanComparison(alternative.comparison, `${alternative.title} · 变更详情`);
  }
}

async function applyPlanAlternative(profile) {
  const alternative = state.planAlternatives?.alternatives?.find(
    (item) => item.profile === profile,
  );
  if (!alternative || !confirm(`确定应用「${alternative.title}」吗？当前范围内未锁定的待确认安排将被替换。`)) {
    return;
  }
  try {
    const result = await api(`/schedules/alternatives/${profile}/apply`, {
      method: "POST",
      body: JSON.stringify(state.planAlternativesRequest || collectPlanPayload()),
    });
    state.lastPlanBatchId = result.batch_id || null;
    await loadAllData();
    renderSchedule(result);
    renderPlanAnalysis(result.analysis, `已应用：${alternative.title}`);
    closePlanAlternatives();
    closePlanComparison();
    loadPlanVersions().catch(handleError);
  } catch (error) {
    alert(`应用方案失败：${error.message}`);
  }
}

function closePlanAlternatives() {
  $("planAlternatives")?.classList.add("hidden");
  state.planAlternatives = null;
  state.planAlternativesRequest = null;
}

function renderPlanComparison(comparison, title, allowApply = false) {
  const panel = $("planComparison");
  if (!panel || !comparison) {
    return;
  }
  const counts = comparison.counts || {};
  if (!allowApply) {
    state.pendingReplan = null;
  }
  $("planComparisonTitle").textContent = title;
  $("planComparisonSummary").innerHTML = [
    ["保留", counts.preserved || 0, "preserved"],
    ["移动", counts.moved || 0, "moved"],
    ["新增", counts.added || 0, "added"],
    ["移除", counts.removed || 0, "removed"],
    ["不变", counts.unchanged || 0, "unchanged"],
  ].map(([label, value, type]) => `
    <div class="comparison-metric ${type}"><span>${label}</span><strong>${value}</strong></div>
  `).join("");

  const changed = (comparison.changes || []).filter((item) => item.type !== "unchanged");
  $("planComparisonRows").innerHTML = changed.length
    ? changed.map(renderPlanComparisonRow).join("")
    : '<div class="empty">当前方案无需调整</div>';
  $("planComparisonActions").classList.toggle("hidden", !allowApply);
  panel.classList.remove("hidden");
  panel.scrollIntoView({ behavior: "smooth", block: "nearest" });
}

function renderPlanComparisonRow(change) {
  const task = state.tasks.find((item) => item.id === change.task_id);
  const title = task?.title || `任务 #${change.task_id}`;
  const labels = { added: "新增", removed: "移除", moved: "调整" };
  const before = planComparisonTime(change.before);
  const after = planComparisonTime(change.after);
  const timeMarkup = change.type === "moved"
    ? `<span>${escapeHtml(before)}</span><b>→</b><span>${escapeHtml(after)}</span>`
    : `<span>${escapeHtml(after || before)}</span>`;
  return `
    <div class="plan-comparison-row ${change.type}">
      <span class="comparison-type">${labels[change.type] || change.type}</span>
      <div class="comparison-task"><strong>${escapeHtml(title)}</strong><small>${escapeHtml(change.reason || "")}</small></div>
      <div class="comparison-time">${timeMarkup}</div>
    </div>
  `;
}

function planComparisonTime(item) {
  if (!item) {
    return "";
  }
  const start = parseDate(item.start_time);
  const end = parseDate(item.end_time);
  if (!start) {
    return "";
  }
  return `${start.getMonth() + 1}/${start.getDate()} ${formatTime(start)}${end ? `–${formatTime(end)}` : ""}`;
}

function closePlanComparison() {
  $("planComparison")?.classList.add("hidden");
  state.pendingReplan = null;
  $("planComparisonActions")?.classList.add("hidden");
}

function renderPlanAnalysis(analysis, title = "计划分析") {
  const container = $("planAnalysis");
  if (!container || !analysis) {
    return;
  }
  const hours = ((analysis.scheduled_minutes || 0) / 60).toFixed(1);
  const issues = analysis.issues || [];
  const modeLabel = analysis.selected_mode === "optimized" ? "深度优化" : "快速安排";
  const modeNote = analysis.selection_reason
    ? `<div class="analysis-engine"><strong>${modeLabel}</strong><span>${escapeHtml(analysis.selection_reason)}</span></div>`
    : "";
  const fallbackNote = analysis.fallback_reason
    ? `<p class="analysis-fallback">深度优化暂未启用：${escapeHtml(analysis.fallback_reason)}</p>`
    : "";
  container.classList.remove("hidden");
  container.innerHTML = `
    <div class="score-ring"><strong>${escapeHtml(analysis.score ?? 0)}</strong><span>分</span></div>
    <div class="analysis-copy">
      <h3>${escapeHtml(title)}</h3>
      <p>已安排 ${escapeHtml(analysis.scheduled_count || 0)} 项，共 ${escapeHtml(hours)} 小时；未安排 ${escapeHtml(analysis.unscheduled_count || 0)} 项。</p>
      ${modeNote}
      ${fallbackNote}
      ${issues.length ? `<ul>${issues.map((item) => `<li>${escapeHtml(item)}</li>`).join("")}</ul>` : `<p class="analysis-good">当前计划没有明显风险。</p>`}
    </div>
  `;
}

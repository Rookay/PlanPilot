/* PlanPilot 前端 · evaluations
   Agent 评测页
   ---------------------------------------------------------
   由 app.js 拆分而来。按业务域归档，加载顺序见 index.html；
   所有函数仍是全局声明（页面有 107 处内联 onclick/onchange 依赖它们）。
   ========================================================= */

async function loadEvaluation() {
  const latest = await api("/evaluations/latest");
  state.evaluationsLoaded = true;
  state.evaluationRun = latest;
  renderEvaluation(latest);
}

async function runEvaluation() {
  const button = $("evaluationRunButton");
  if (button?.disabled) return;
  if (button) {
    button.disabled = true;
    button.textContent = "评测中";
  }
  try {
    const result = await api("/evaluations/run", { method: "POST" });
    state.evaluationRun = result;
    renderEvaluation(result);
  } catch (error) {
    alert(`评测失败：${error.message}`);
  } finally {
    if (button) {
      button.disabled = false;
      button.textContent = "运行评测";
    }
  }
}

function renderEvaluation(run) {
  const empty = $("evaluationEmpty");
  const results = $("evaluationResults");
  if (!run) {
    empty?.classList.remove("hidden");
    results?.classList.add("hidden");
    return;
  }
  empty?.classList.add("hidden");
  results?.classList.remove("hidden");
  $("evaluationScore").textContent = Number(run.score || 0).toFixed(1);
  const metrics = run.metrics || {};
  $("evaluationMetrics").innerHTML = [
    ["工具选择", `${metrics.tool_selection_accuracy ?? 0}%`],
    ["工具成功", `${metrics.tool_success_rate ?? 0}%`],
    ["约束通过", `${metrics.constraint_pass_rate ?? 0}%`],
    ["确认安全", `${metrics.confirmation_safety_rate ?? 0}%`],
    ["平均耗时", `${metrics.average_latency_ms ?? 0} ms`],
    ["模型 Tokens", `${metrics.total_model_tokens ?? 0}`],
    ["估算成本", `${Number(metrics.estimated_model_cost ?? 0).toFixed(6)}`],
  ].map(([label, value]) => `
    <div><span>${escapeHtml(label)}</span><strong>${escapeHtml(value)}</strong></div>
  `).join("");
  $("evaluationScenarioRows").innerHTML = (run.results || []).map((item) => `
    <tr>
      <td><strong>${escapeHtml(item.name)}</strong><small>${escapeHtml((item.actual_tools || []).join(" → "))}</small></td>
      <td><span class="evaluation-result ${item.passed ? "pass" : "fail"}">${item.passed ? "通过" : "失败"}</span></td>
      <td>${evaluationCheck(item.selection_passed)}</td>
      <td>${evaluationCheck(item.constraints_passed)}</td>
      <td>${evaluationCheck(item.confirmation_passed)}</td>
      <td>${escapeHtml(`${item.duration_ms} ms`)}</td>
    </tr>
  `).join("");
}

function evaluationCheck(value) {
  return `<span class="evaluation-check ${value ? "pass" : "fail"}">${value ? "通过" : "未通过"}</span>`;
}


/* =========================
   错误处理
========================= */

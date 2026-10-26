/* PlanPilot 前端 · main
   错误处理与启动
   ---------------------------------------------------------
   由 app.js 拆分而来。按业务域归档，加载顺序见 index.html；
   所有函数仍是全局声明（页面有 107 处内联 onclick/onchange 依赖它们）。
   ========================================================= */

function handleError(error) {
  console.error(error);
}


/* =========================
   初始化
========================= */

function init() {
  state.weekStart = startOfWeek(new Date());
  state.calendarStart = startOfDay(new Date());
  updateFutureInputMinimums();
  setPlanningScope("week");
  if ($("itineraryDate")) {
    $("itineraryDate").value = localDateInputValue();
  }
  Promise.all([loadAllData(), loadPreferences(), loadLocations()])
    .then(() => resetLocationForm())
    .catch(handleError);

  const chatInput = $("chatInput");
  if (chatInput) {
    chatInput.addEventListener("input", autoResizeChatInput);
    chatInput.addEventListener("keydown", (event) => {
      // Enter 发送，Shift+Enter 换行。
      if (event.key === "Enter" && !event.shiftKey) {
        event.preventDefault();
        sendChatMessage();
      }
    });
  }
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && !$("calendarDetailModal").classList.contains("hidden")) {
      closeCalendarItemDetails();
    }
  });
  document.addEventListener("pointermove", handleScheduleResizeMove);
  document.addEventListener("pointerup", finishScheduleResize);
  window.setInterval(() => loadAgentStatus().catch(handleError), 5000);
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", init);
} else {
  init();
}

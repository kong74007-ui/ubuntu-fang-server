/* 黄雀工作台 · 全局任务追踪器 (tasks.js)
 * 把"正在处理的任务"持久化到 localStorage，并在每页左导航(rail)注入"任务进行中"徽标。
 * 切页 / 刷新都不丢任务；点徽标回获客页(leads.html)续看结果。
 * 纯 vanilla、无依赖、全程 try/catch 降级——坏了顶多退回原行为，绝不让页面崩。
 * 只存 job_id 等元数据，不存名单本体(PII 不落浏览器)。
 */
(function () {
  "use strict";

  var KEY = "hq_jobs";
  var ACTIVE = { queued: 1, running: 1, pending: 1 };
  var MAX_HISTORY = 30; // 完成态最多留 30 条，进行中全留
  var mem = null;       // localStorage 不可用时的内存兜底
  var listeners = [];

  function now() { try { return Date.now(); } catch (e) { return 0; } }

  function read() {
    try {
      var raw = localStorage.getItem(KEY);
      if (!raw) return mem || [];
      var arr = JSON.parse(raw);
      return Array.isArray(arr) ? arr : [];
    } catch (e) { return mem || []; }
  }

  function write(arr) {
    try {
      var active = arr.filter(function (j) { return ACTIVE[j.status]; });
      var done = arr.filter(function (j) { return !ACTIVE[j.status]; }).slice(-MAX_HISTORY);
      var out = active.concat(done);
      mem = out;
      try { localStorage.setItem(KEY, JSON.stringify(out)); } catch (e) {}
      return out;
    } catch (e) { mem = arr; return arr; }
  }

  function list() { return read(); }

  function get(id) {
    var a = read();
    for (var i = 0; i < a.length; i++) {
      if (String(a[i].id) === String(id)) return a[i];
    }
    return null;
  }

  function upsert(job) {
    if (!job || job.id == null) return;
    var a = read(), found = false;
    for (var i = 0; i < a.length; i++) {
      if (String(a[i].id) === String(job.id)) {
        var merged = {};
        for (var k in a[i]) merged[k] = a[i][k];
        for (var k2 in job) merged[k2] = job[k2];
        merged.updatedAt = now();
        a[i] = merged;
        found = true;
        break;
      }
    }
    if (!found) {
      job.createdAt = job.createdAt || now();
      job.updatedAt = now();
      a.push(job);
    }
    write(a);
    emit();
  }

  function remove(id) {
    var a = read().filter(function (j) { return String(j.id) !== String(id); });
    write(a);
    emit();
  }

  function activeCount() {
    return read().filter(function (j) { return ACTIVE[j.status]; }).length;
  }

  function latestActive() {
    var a = read().filter(function (j) { return ACTIVE[j.status]; });
    a.sort(function (x, y) {
      return (y.updatedAt || y.createdAt || 0) - (x.updatedAt || x.createdAt || 0);
    });
    return a[0] || null;
  }

  function onChange(cb) { if (typeof cb === "function") listeners.push(cb); }

  function emit() {
    var n = activeCount();
    renderBadge();
    for (var i = 0; i < listeners.length; i++) {
      try { listeners[i](n); } catch (e) {}
    }
  }

  // ---- 徽标：注入到 rail 的 .spacer 之后 ----
  function renderBadge() {
    try {
      var rail = document.querySelector(".rail");
      if (!rail) return;
      var n = activeCount();
      var el = document.getElementById("hq-tasks-badge");
      if (!el) {
        el = document.createElement("a");
        el.id = "hq-tasks-badge";
        el.className = "navbtn";
        el.style.position = "relative";
        el.innerHTML =
          '<svg viewBox="0 0 24 24" style="width:22px;height:22px;stroke:currentColor;fill:none;stroke-width:1.8"><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/></svg>' +
          '<span class="lb">任务</span>' +
          '<span id="hq-tasks-count" style="position:absolute;top:5px;right:12px;min-width:16px;height:16px;padding:0 4px;border-radius:9px;background:#e7b24c;color:#0a0e16;font-size:10px;font-weight:800;line-height:16px;text-align:center;box-shadow:0 0 0 2px rgba(8,13,22,.92)"></span>';
        var spacer = rail.querySelector(".spacer");
        if (spacer) { spacer.insertAdjacentElement("afterend", el); }
        else { rail.appendChild(el); if (window.console) console.warn("[HQTasks] .rail .spacer 未找到，徽标兜底追加"); }
        // 在获客页点徽标不整页跳，只改 hash(同文档) → 触发 leads 的 hashchange 恢复
        el.addEventListener("click", function (ev) {
          try {
            if (/leads$/.test(location.pathname)) {
              ev.preventDefault();
              var la = latestActive();
              location.hash = "task=" + (la ? la.id : "");
              window.dispatchEvent(new HashChangeEvent("hashchange"));
            }
          } catch (e) {}
        });
      }
      var la2 = latestActive();
      el.setAttribute("href", "leads#task=" + (la2 ? la2.id : ""));
      var cnt = document.getElementById("hq-tasks-count");
      if (n > 0) {
        el.style.display = "";
        el.style.color = "#e7b24c";
        if (cnt) cnt.textContent = n > 99 ? "99+" : String(n);
      } else {
        el.style.display = "none";
      }
    } catch (e) { /* 徽标坏了不影响主功能 */ }
  }

  // 跨标签页同步：其它标签页写 localStorage 时刷新本页徽标
  try {
    window.addEventListener("storage", function (e) { if (e.key === KEY) emit(); });
  } catch (e) {}

  window.HQTasks = {
    list: list, get: get, upsert: upsert, remove: remove,
    activeCount: activeCount, latestActive: latestActive,
    onChange: onChange, renderBadge: renderBadge
  };

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", renderBadge);
  } else {
    renderBadge();
  }
})();

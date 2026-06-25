/* 黄雀工作台 · 登录守卫
 * 工作台页加载前同步执行：本地无登录 token → 跳转登录页（带回跳目标）。
 * 放在 <head> 里同步加载，避免未登录闪屏。 */
(function () {
  try {
    if (!localStorage.getItem("hq_token")) {
      var here = (location.pathname.split("/").pop() || "dashboard.html");
      location.replace("../login.html?redirect=" + encodeURIComponent(here));
    }
  } catch (e) { /* localStorage 不可用时不拦截，避免误锁 */ }
})();

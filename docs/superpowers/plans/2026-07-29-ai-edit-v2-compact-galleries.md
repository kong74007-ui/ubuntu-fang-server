# AI Edit V2 紧凑卡片列表实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将平台口播视频改为单排横向滑动，并将模板下拉框替换为紧凑的 `9:16` 预览图片卡片。

**Architecture:** 保持现有 V2 API、draft 协议和任务流程不变，只扩展已发布模板的展示元数据并替换前端选择控件。平台视频列表和模板列表都使用静态封面，不在列表阶段创建视频元素；模板选择状态由页面状态对象保存并在 `buildDraft()` 中转换为现有 `template_id`、`template_version` 字段。

**Tech Stack:** 原生 HTML/CSS/JavaScript、Python 模板目录、Node.js `node:test`、Python `unittest`、静态 SVG 资源。

## Global Constraints

- 平台视频卡片保持 `9:16`，只展示一排，超出宽度后横向滚动。
- 平台视频列表不得绑定视频 `src`，只有点击卡片后右侧播放器才加载视频。
- 模板预览保持 `9:16`，桌面端固定宽度约 `110px`，移动端约 `90px`。
- 模板选择使用图片卡片单选，不使用下拉框、不加载预览视频、不打开二级弹窗。
- 当前两套已发布模板必须分别提供站内静态预览图。
- `creation_mode`、`template_id`、`template_version` 和报价、任务、计费协议保持不变。

---

### Task 1: 平台视频单排横向列表

**Files:**
- Modify: `tests/test_ai_edit_v2_ui.js`
- Modify: `site/workbench/ai-edit-v2.html`

**Interfaces:**
- Consumes: 现有 `platformCard(item)` 输出和 `#platformGallery` 容器。
- Produces: 单排、不换行、可横向滚动的 `.platform-gallery`，固定宽度的 `.platform-card`。

- [ ] **Step 1: 写入失败测试**

在 `tests/test_ai_edit_v2_ui.js` 添加：

```js
test('platform gallery stays in one horizontal row', () => {
  const page = fs.readFileSync(pagePath, 'utf8');
  const rule = page.match(/\.platform-gallery\{([^}]*)\}/)?.[1] || '';
  assert.match(rule, /display:flex/);
  assert.match(rule, /overflow-x:auto/);
  assert.match(rule, /flex-wrap:nowrap/);
  const cardRule = page.match(/\.platform-card\{([^}]*)\}/)?.[1] || '';
  assert.match(cardRule, /flex:0 0 142px/);
});
```

- [ ] **Step 2: 运行测试并确认因旧网格布局失败**

Run: `node --test --test-name-pattern "platform gallery stays in one horizontal row" tests/test_ai_edit_v2_ui.js`

Expected: FAIL，因为 `.platform-gallery` 仍为 `display:grid` 且会换行。

- [ ] **Step 3: 实现最小 CSS 修改**

在 `site/workbench/ai-edit-v2.html` 中将平台列表改为：

```css
.platform-gallery{display:flex;flex-wrap:nowrap;gap:12px;margin-top:14px;overflow-x:auto;overflow-y:hidden;padding:1px 4px 10px 1px;scroll-snap-type:x proximity}
.platform-card{flex:0 0 142px;scroll-snap-align:start}
```

删除窄屏媒体查询中把 `.platform-gallery` 改回三列网格的规则；保留卡片 `9:16` 封面比例。

- [ ] **Step 4: 运行测试并确认通过**

Run: `node --test --test-name-pattern "platform gallery stays in one horizontal row|platform cards render lightweight covers" tests/test_ai_edit_v2_ui.js`

Expected: 2 tests PASS。

- [ ] **Step 5: 提交**

```bash
git add tests/test_ai_edit_v2_ui.js site/workbench/ai-edit-v2.html
git commit -m "fix(ai-edit-v2): keep platform cards in one row"
```

### Task 2: 模板图片卡片选择器

**Files:**
- Create: `site/assets/ai-edit-v2/templates/business-diagnostic.svg`
- Create: `site/assets/ai-edit-v2/templates/modern-documentary.svg`
- Modify: `server/content_domains/ai_edit_v2_templates.py`
- Modify: `site/workbench/ai-edit-v2.html`
- Modify: `tests/test_ai_edit_v2_templates.py`
- Modify: `tests/test_ai_edit_v2_ui.js`

**Interfaces:**
- Consumes: `GET /api/v2/edit/templates` 返回的已发布模板数组。
- Produces: 每个模板新增 `preview_image_url: str`；前端新增 `renderTemplates(items)`、`templateCard(item)`、`selectTemplate(id, version)`，并由 `state.selectedTemplate` 保存 `{id, version}`。

- [ ] **Step 1: 写后端失败测试**

在 `tests/test_ai_edit_v2_templates.py` 的目录测试中加入：

```python
for item in templates:
    self.assertRegex(
        item["preview_image_url"],
        r"^/assets/ai-edit-v2/templates/[a-z0-9-]+\.svg$",
    )
```

- [ ] **Step 2: 写前端失败测试**

在 `tests/test_ai_edit_v2_ui.js` 添加模板卡片行为测试，断言：

```js
assert.match(page, /id="templateGallery"/);
assert.doesNotMatch(page, /id="templateSelect"/);
assert.match(page, /function renderTemplates\(/);
assert.match(page, /function selectTemplate\(/);
assert.match(page, /\.template-card\{[^}]*flex:0 0 110px/);
assert.match(page, /\.template-card-media\{[^}]*aspect-ratio:9\/16/);
```

并执行 `templateCard(item)`，使用手写模板对象验证返回 HTML 包含 `<img loading="lazy" src="/assets/ai-edit-v2/templates/business-diagnostic.svg">` 且不包含 `<video`。

- [ ] **Step 3: 运行失败测试**

Run: `python -m unittest tests.test_ai_edit_v2_templates -v`

Expected: FAIL，缺少 `preview_image_url`。

Run: `node --test --test-name-pattern "template" tests/test_ai_edit_v2_ui.js`

Expected: FAIL，页面仍包含 `templateSelect`。

- [ ] **Step 4: 创建两张静态模板预览图**

创建两个 `viewBox="0 0 900 1600"` 的 SVG：

- `business-diagnostic.svg` 使用低饱和深色背景、暖金单一强调色、粗体商业标题、数据卡片和诊断标记。
- `modern-documentary.svg` 使用素材主导的中性色背景、冷青单一强调色、窄体纪实标题、时间地点标签和克制字幕条。

两张图都只展示视觉语言样例，不包含产品功效、价格或人物身份信息。

- [ ] **Step 5: 扩展模板目录元数据**

在两项 `_PUBLISHED_TEMPLATES` 中分别加入：

```python
"preview_image_url": "/assets/ai-edit-v2/templates/business-diagnostic.svg",
```

和：

```python
"preview_image_url": "/assets/ai-edit-v2/templates/modern-documentary.svg",
```

`list_published_templates()` 和现有 API 继续返回防御性副本，不新增供应商字段。

- [ ] **Step 6: 替换模板下拉框**

在 `site/workbench/ai-edit-v2.html`：

- 用 `<div id="templateGallery" class="template-gallery"></div>` 替换 `#templateSelect`。
- `state` 增加 `templates:[]` 和 `selectedTemplate:null`。
- `renderTemplates(items)` 渲染图片卡片；图片加载失败时使用带模板名的占位，不请求视频。
- `selectTemplate(id, version)` 更新单选状态并调用 `invalidateQuote()`。
- `buildDraft()` 在模板模式读取 `state.selectedTemplate`，缺失时抛出“请选择平台模板”，存在时写入现有 `template_id`、`template_version`。
- 模板接口加载完成后调用 `renderTemplates(data.items || [])`，删除 `templateSelect.onchange`。

使用以下紧凑布局：

```css
.template-gallery{display:flex;flex-wrap:nowrap;gap:10px;overflow-x:auto;padding:2px 2px 9px}
.template-card{flex:0 0 110px}
.template-card-media{display:block;aspect-ratio:9/16;overflow:hidden}
@media(max-width:700px){.template-card{flex-basis:90px}}
```

- [ ] **Step 7: 运行聚焦测试**

Run: `python -m unittest tests.test_ai_edit_v2_templates tests.test_ai_edit_v2_api -v`

Expected: PASS。

Run: `node --test tests/test_ai_edit_v2_ui.js`

Expected: PASS。

- [ ] **Step 8: 运行完整回归与静态检查**

Run: `python -m unittest tests.test_ai_edit_v2_api tests.test_ai_edit_v2_e2e -v`

Expected: 48 tests PASS。

Run: `node --test tests/test_ai_edit_v2_ui.js tests/test_ai_edit_dual_entry.js tests/test_cloud_shell_sidebar.js`

Expected: 所有测试 PASS。

Run: `git diff --check`

Expected: 无输出，退出码 0。

- [ ] **Step 9: 提交**

```bash
git add site/assets/ai-edit-v2/templates server/content_domains/ai_edit_v2_templates.py site/workbench/ai-edit-v2.html tests/test_ai_edit_v2_templates.py tests/test_ai_edit_v2_ui.js
git commit -m "feat(ai-edit-v2): show compact template previews"
```

- [ ] **Step 10: 推送并更新测试环境**

推送当前分支到 PR #28。测试环境只同步 `site/workbench/ai-edit-v2.html`、两个 SVG 和 `server/content_domains/ai_edit_v2_templates.py`；备份原文件，必要时只重启 `huangque-content`，随后验证页面 HTTP 200、本地/远端哈希一致以及 PR CI 通过。

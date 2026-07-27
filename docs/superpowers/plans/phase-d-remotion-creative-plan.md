# AI 智能剪辑 V2 Phase D Remotion Creative Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 交付逐场景 Shotstack/Remotion 自动路由、隔离 Remotion 高级模板与开放生成、自由代码安全校验、关键帧布局检查、最多 2 次自动修复、统一合成和稳定模板降级。

**Architecture:** Python Worker 只生成受约束 `RenderRequest` 并调用独立 `services/ai-edit-remotion`。托管服务在无任意网络/依赖/进程权限的沙箱中编译与渲染，产出短片和关键帧；Shotstack 再统一编排。开放生成失败经过最多两次基于明确错误的修复，仍失败选择风格相近的审核模板并持久化真实降级状态。

**Tech Stack:** Python 3、Node.js 20、TypeScript、Remotion、受控 AST/ESLint 校验、托管渲染/云函数、Shotstack、FFprobe/FFmpeg、`unittest`、Node test runner。

## Global Constraints

- [ ] 依赖 Phase A–C 全部通过；稳定 Shotstack 路径必须在 Remotion 故障时仍可交付。
- [ ] `services/ai-edit-remotion` 是独立构建/部署单元；不得由 `content_api.py`、V2 Worker 或 shell 子进程本地执行用户/AI代码。
- [ ] 普通用户不能上传代码、模板包或发布模板；AI code 只来自受控导演服务并绑定单任务。
- [ ] 网络默认关闭，只允许托管层代理读取授权 COS 对象；短期 URL 不写日志/数据库/生成代码。
- [ ] 开放生成自动修复总次数固定为 2，不因重启重置；第三次失败必须降级。
- [ ] 降级结果必须向用户和运营指标显示，禁止标记为开放创作成功。
- [ ] Phase D 测试不调用生产 Remotion；使用本地纯校验器和 fake hosted client。

---

## 1. Phase D 精确文件结构

**Create**

- `server/content_domains/renderers/remotion_v2.py`：托管服务客户端与状态归一化。
- `server/content_domains/ai_edit_v2_reference.py`：参考视频抽象风格学习与终态清理。
- `server/content_domains/ai_edit_v2_creative.py`：自由布局/MG 规范、修复提示和模板降级。
- `services/ai-edit-remotion/package.json`
- `services/ai-edit-remotion/tsconfig.json`
- `services/ai-edit-remotion/src/contracts.ts`
- `services/ai-edit-remotion/src/validate.ts`
- `services/ai-edit-remotion/src/sandbox-policy.ts`
- `services/ai-edit-remotion/src/render.ts`
- `services/ai-edit-remotion/src/handler.ts`
- `services/ai-edit-remotion/src/templates/index.ts`
- `services/ai-edit-remotion/src/templates/BusinessDiagnostic.tsx`
- `services/ai-edit-remotion/src/templates/EditorialStory.tsx`
- `services/ai-edit-remotion/test/validate.test.ts`
- `services/ai-edit-remotion/test/render.test.ts`
- `tests/test_ai_edit_v2_router.py`
- `tests/test_ai_edit_v2_remotion.py`
- `tests/test_ai_edit_v2_reference.py`
- `tests/test_ai_edit_v2_open_generation.py`
- `tests/test_ai_edit_v2_mixed_assembly.py`

**Modify**

- `server/content_domains/ai_edit_v2_schema.py`：Render Graph 和自由代码请求白名单。
- `server/content_domains/ai_edit_v2_router.py`：逐场景复杂度/预算/能力路由。
- `server/content_domains/ai_edit_v2_pipeline.py`：关键帧、修复、降级和混合片段检查点。
- `server/content_domains/renderers/shotstack_v2.py`：接收 Remotion 片段并统一合成。
- `server/content_domains/ai_edit_v2_quality.py`：关键帧和混合成片布局/动画检查。
- `server/content_domains/ai_edit_v2_store.py`：开放修复次数、降级模板与原因统计。
- `server/content_domains/ai_edit_v2_api.py`：Remotion webhook 鉴权、去重和主动回查触发。
- `site/workbench/ai-edit.html`：开放生成状态和降级说明。
- `deploy/huangque-secrets.env.example`：隔离服务变量名。

## 2. 冻结跨语言契约

```typescript
export type RenderRequest = {
  requestVersion: "1.0";
  jobId: string;
  sceneId: string;
  mode: "approved_template" | "open_generation";
  composition: { width: 1080 | 1920; height: 1080 | 1920; fps: 30; durationMs: number };
  design: Record<string, unknown>;
  assets: Array<{ handle: string; proxyUrl: string; mediaType: "image" | "video" | "audio" }>;
  generatedCode?: string;
  callbackUrl: string;
  callbackToken: string;
};

export type RenderResponse = {
  renderId: string;
  status: "queued" | "rendering" | "completed" | "failed";
  artifactHandle?: string;
  previewHandles?: string[];
  errorCodes?: string[];
  usage?: { renderMs: number; memoryMbSeconds: number };
};
```

Python `remotion_v2.submit_render(request: dict) -> ProviderResult` 只能传上述字段；`proxyUrl/callbackToken` 在发送前生成，store 中只保存 `handle/cos_key`。

## Task 1: 逐场景自动路由和可解释决策

**Files:**
- Modify: `server/content_domains/ai_edit_v2_router.py`
- Create: `tests/test_ai_edit_v2_router.py`
- Modify: `server/content_domains/ai_edit_v2_schema.py`

- [ ] **Step 1: 写失败测试**：普通字幕/卡片/B-roll/标准转场走 Shotstack；复杂 MG、动态图表、拼贴、自由布局走 Remotion。
- [ ] **Step 2: 写混合测试**，同一 plan 可以 A场景 Shotstack、B场景 Remotion、C场景 Shotstack，时间覆盖连续且不重叠。
- [ ] **Step 3: 写预算/健康测试**，Remotion 不健康、预算不足或剩余时间不足时路由稳定模板并记录原因，不让用户选择渲染器。
- [ ] **Step 4: 运行 `python -m unittest tests.test_ai_edit_v2_router -v`**；预期失败。
- [ ] **Step 5: 实现 deterministic capability matrix 和 route record `{scene_id,renderer,capability,cost,time,reason}`。
- [ ] **Step 6: 重跑测试**；预期通过。
- [ ] **Step 7: 提交**

```powershell
git add server/content_domains/ai_edit_v2_router.py server/content_domains/ai_edit_v2_schema.py tests/test_ai_edit_v2_router.py
git commit -m "feat(ai-edit-v2): route scenes by creative capability"
```

## Task 2: 托管 Remotion 契约与参数化高级模板

**Files:**
- Create: `services/ai-edit-remotion/package.json`
- Create: `services/ai-edit-remotion/tsconfig.json`
- Create: `services/ai-edit-remotion/src/contracts.ts`
- Create: `services/ai-edit-remotion/src/render.ts`
- Create: `services/ai-edit-remotion/src/handler.ts`
- Create: `services/ai-edit-remotion/src/templates/index.ts`
- Create: `services/ai-edit-remotion/src/templates/BusinessDiagnostic.tsx`
- Create: `services/ai-edit-remotion/src/templates/EditorialStory.tsx`
- Create: `services/ai-edit-remotion/test/render.test.ts`
- Create: `server/content_domains/renderers/remotion_v2.py`
- Create: `tests/test_ai_edit_v2_remotion.py`
- Modify: `server/content_domains/ai_edit_v2_api.py`

- [ ] **Step 1: 写 TypeScript 契约测试**，拒绝未知 requestVersion、非 30fps、非 1080p、超时长、未知模板和任意 asset URL 域名。
- [ ] **Step 2: 写参数化差异测试**，同一模板对不同内容产生不同场景时长、布局、图表和素材位置，而非只换标题/颜色。
- [ ] **Step 3: 写 Python client/webhook 测试**，提交带幂等 reference，已有 render id 时只 query；回调 token 常量时间校验、事件指纹去重且回调后主动回查。
- [ ] **Step 4: 运行 `npm test --prefix services/ai-edit-remotion` 和 `python -m unittest tests.test_ai_edit_v2_remotion -v`**；预期因文件缺失失败。
- [ ] **Step 5: 实现两个稳定模板、render handler、artifact handle 和关键帧 handle；实现 Python 脱敏 client。
- [ ] **Step 6: 重跑测试**；预期通过。
- [ ] **Step 7: 提交**

```powershell
git add services/ai-edit-remotion server/content_domains/renderers/remotion_v2.py server/content_domains/ai_edit_v2_api.py tests/test_ai_edit_v2_remotion.py
git commit -m "feat(ai-edit-v2): add hosted remotion template contract"
```

## Task 3: 自由代码静态校验和沙箱策略

**Files:**
- Create: `services/ai-edit-remotion/src/validate.ts`
- Create: `services/ai-edit-remotion/src/sandbox-policy.ts`
- Create: `services/ai-edit-remotion/test/validate.test.ts`

**Interfaces:** `validateGeneratedCode(source, policy) -> {ok, errors, imports, resourceEstimate}`；校验成功也只表示可送托管沙箱，不允许主服务执行。

- [ ] **Step 1: 写禁止项测试**，拒绝 `eval/Function/child_process/fs/net/http/https/dns/worker_threads/process.env/dynamic import/require(non-whitelist)`。
- [ ] **Step 2: 写依赖测试**，只允许锁定的 React、Remotion 和平台组件；禁止安装依赖、原生扩展和版本范围漂移。
- [ ] **Step 3: 写资源测试**，AST 节点、循环、图层、媒体数量、输出大小、内存和渲染时长超过 policy 拒绝。
- [ ] **Step 4: 写网络/文件策略测试**，沙箱无外网，临时目录以外不可读写，资源只能通过 opaque handle 解析。
- [ ] **Step 5: 运行 `npm test --prefix services/ai-edit-remotion -- validate`**；预期失败。
- [ ] **Step 6: 实现 AST 校验、白名单 import 解析、资源估算和结构化 error code；不得用正则作为唯一安全层。
- [ ] **Step 7: 重跑测试并执行 `npm run typecheck --prefix services/ai-edit-remotion`**；预期通过。
- [ ] **Step 8: 提交**

```powershell
git add services/ai-edit-remotion/src/validate.ts services/ai-edit-remotion/src/sandbox-policy.ts services/ai-edit-remotion/test/validate.test.ts
git commit -m "feat(ai-edit-v2): validate generated motion code safely"
```

## Task 4: 关键帧预览与布局/动画自检

**Files:**
- Modify: `services/ai-edit-remotion/src/render.ts`
- Modify: `services/ai-edit-remotion/src/handler.ts`
- Modify: `services/ai-edit-remotion/test/render.test.ts`
- Modify: `server/content_domains/ai_edit_v2_quality.py`
- Create: `tests/test_ai_edit_v2_keyframes.py`

- [ ] **Step 1: 写关键帧测试**，完整渲染前必须产出 0%、25%、50%、75%、100% 帧；缺帧或错误占位禁止继续。
- [ ] **Step 2: 写布局检查测试**，文字重叠/越界、安全区、人脸/产品/Logo/二维码裁切输出明确 bounding boxes 和 issue code。
- [ ] **Step 3: 写动画完成测试**，100% 帧仍有 loading/未完成动画或关键元素透明度为 0 时失败。
- [ ] **Step 4: 运行 Node 与 Python 定向测试**；预期失败。
- [ ] **Step 5: 实现 preview artifacts、布局元数据和 Python quality adapter；预览 URL 仍使用短期代理。
- [ ] **Step 6: 重跑测试**；预期通过。
- [ ] **Step 7: 提交**

```powershell
git add services/ai-edit-remotion/src/render.ts services/ai-edit-remotion/src/handler.ts services/ai-edit-remotion/test/render.test.ts server/content_domains/ai_edit_v2_quality.py tests/test_ai_edit_v2_keyframes.py
git commit -m "feat(ai-edit-v2): validate creative keyframes before render"
```

## Task 5: 开放生成、两次自动修复和稳定降级

**Files:**
- Create: `server/content_domains/ai_edit_v2_creative.py`
- Create: `tests/test_ai_edit_v2_open_generation.py`
- Modify: `server/content_domains/ai_edit_v2_pipeline.py`
- Modify: `server/content_domains/ai_edit_v2_store.py`

- [ ] **Step 1: 写修复计数测试**，初次失败后 repair_count=1，第二次失败后=2，第三次不再调用生成模型而选择稳定模板；重启不重置。
- [ ] **Step 2: 写错误上下文测试**，repair prompt 只包含结构化 error code、相关行和布局框，不含密钥/URL/完整素材内容。
- [ ] **Step 3: 写降级测试**，按 style similarity 和能力选择已审核模板，记录 `open_generation_failed=true/fallback_template/reason` 并在 API/UI 显示。
- [ ] **Step 4: 运行 `python -m unittest tests.test_ai_edit_v2_open_generation -v`**；预期失败。
- [ ] **Step 5: 实现 code generation contract、修复状态机、稳定模板选择和指标；降级仍需完整 QC。
- [ ] **Step 6: 增加成功开放生成测试**，通过关键帧和完整渲染时 `degraded=false`，不得机械套稳定模板。
- [ ] **Step 7: 重跑测试**；预期通过。
- [ ] **Step 8: 提交**

```powershell
git add server/content_domains/ai_edit_v2_creative.py server/content_domains/ai_edit_v2_pipeline.py server/content_domains/ai_edit_v2_store.py tests/test_ai_edit_v2_open_generation.py
git commit -m "feat(ai-edit-v2): repair or downgrade open generation"
```

## Task 6: 参考视频抽象学习与终态清理

**Files:**
- Create: `server/content_domains/ai_edit_v2_reference.py`
- Create: `tests/test_ai_edit_v2_reference.py`
- Modify: `server/content_domains/ai_edit_v2_pipeline.py`

**Interfaces:** `analyze_reference(job_id, material_id) -> {rhythm,layout_density,caption_feel,transition_strength,color_relationship,music_mood,sound_dynamics}`；不得返回人物身份、Logo、原文、音乐指纹或原素材片段。

- [ ] **Step 1: 写抽象边界测试**，输出只允许冻结字段，不含 OCR 文案、品牌、人物 embedding、音频文件或可复用素材 URL。
- [ ] **Step 2: 写用途测试**，`style_only` 参考不进入 render assets；`direct_use` 也只有质量合格且归属当前用户时可用。
- [ ] **Step 3: 写清理测试**，completed 和所有 failed 终态均删除 reference style JSON/临时帧，用户历史素材本身保留。
- [ ] **Step 4: 运行 `python -m unittest tests.test_ai_edit_v2_reference -v`**；预期失败。
- [ ] **Step 5: 实现任务级分析、抽象白名单和 finally/恢复清理器。
- [ ] **Step 6: 重跑测试**；预期通过。
- [ ] **Step 7: 提交**

```powershell
git add server/content_domains/ai_edit_v2_reference.py server/content_domains/ai_edit_v2_pipeline.py tests/test_ai_edit_v2_reference.py
git commit -m "feat(ai-edit-v2): learn ephemeral reference style safely"
```

## Task 7: 混合片段统一合成与全链路恢复

**Files:**
- Create: `tests/test_ai_edit_v2_mixed_assembly.py`
- Modify: `server/content_domains/renderers/shotstack_v2.py`
- Modify: `server/content_domains/ai_edit_v2_pipeline.py`
- Modify: `server/content_domains/ai_edit_v2_quality.py`
- Modify: `site/workbench/ai-edit.html`
- Modify: `deploy/huangque-secrets.env.example`

- [ ] **Step 1: 写混合 timeline 测试**，Remotion 片段先统一 1080p/30fps/H.264/AAC/色彩，再按原时间范围进入 Shotstack，主音轨/字幕连续。
- [ ] **Step 2: 写恢复测试**，某个 Remotion 片段已完成、进程重启后复用 artifact；其他场景继续，不整片重渲染。
- [ ] **Step 3: 写失败测试**，片段时长偏差、黑帧、无尾帧或色彩规格错误先单片修复，失败后稳定模板降级，不污染已完成片段。
- [ ] **Step 4: 运行 `python -m unittest tests.test_ai_edit_v2_mixed_assembly -v`**；预期失败。
- [ ] **Step 5: 实现片段 adapter、artifact checkpoint、最终 Shotstack 合成和端到端 QC。
- [ ] **Step 6: 页面显示“开放创作/稳定模板降级”及原因，不显示 Remotion/Shotstack 名称。
- [ ] **Step 7: env example 增加 `AI_EDIT_V2_REMOTION_BASE/TOKEN` 与沙箱限制变量名。
- [ ] **Step 8: 重跑 Phase D 全套及 Phase B 稳定回归**；预期通过。
- [ ] **Step 9: 提交**

```powershell
git add server/content_domains/renderers/shotstack_v2.py server/content_domains/ai_edit_v2_pipeline.py server/content_domains/ai_edit_v2_quality.py site/workbench/ai-edit.html deploy/huangque-secrets.env.example tests/test_ai_edit_v2_mixed_assembly.py
git commit -m "feat(ai-edit-v2): assemble recoverable mixed renders"
```

## Phase D 验收

```powershell
npm ci --prefix services/ai-edit-remotion
npm test --prefix services/ai-edit-remotion
npm run typecheck --prefix services/ai-edit-remotion
python -m unittest tests.test_ai_edit_v2_router tests.test_ai_edit_v2_remotion tests.test_ai_edit_v2_keyframes tests.test_ai_edit_v2_open_generation tests.test_ai_edit_v2_reference tests.test_ai_edit_v2_mixed_assembly -v
python -m unittest tests.test_ai_edit_v2_shotstack tests.test_ai_edit_v2_delivery -v
python scripts/ci_validate.py
```

预期：稳定模板、参数化 Remotion 和开放生成三条路线均由 fake hosted service 闭环；危险代码全部拒绝；修复恰好最多 2 次；混合片可恢复；降级被真实标记；网站主进程没有任何生成代码执行入口。

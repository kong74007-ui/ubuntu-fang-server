# AI 智能剪辑 V3 Phase D 网站接入与交付 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在 Phase A–C 已通过的前提下，交付测试站可用且与 V2 隔离的 AI 智能剪辑 V3 页面、任务中心、私有成片播放和独立价格后台，完整覆盖五类输入、三类创作入口、报价、创建、状态、结果与重试。

**Architecture:** 原生 HTML/CSS/JavaScript 单页只消费 `/api/v3/edit/*` 的 owner-bound DTO；服务端负责严格判别联合、价格指纹、状态语义、幂等和短期媒体授权，浏览器不保存 COS Key、长期 URL 或供应商字段。Phase D 复用 Phase A 已注册的 V3 core route，先完成页面和 browser DTO，再以一个公共文件一个提交的方式接入导航、任务追踪、资产播放和管理后台，最后单独刷新缓存戳并编写测试环境运行手册。

**Tech Stack:** Python 3、`unittest`、原生 HTML/CSS/JavaScript、Node.js `node:test`/`vm`、SQLite/WAL、腾讯云私有 COS、现有工作台 shell/tasks、现有运营后台、PowerShell 与 Bash 运行手册。

## Global Constraints

- [ ] 仅在 `codex/ai-edit-v3` 分支和隔离 worktree 实施；每个任务开始前运行 `git status --short --branch`、`git branch --show-current`、`git log --oneline -5`，不得覆盖其他 agent 或用户的未提交改动。
- [ ] Phase D 依赖 Phase A、B、C 的退出门槛全部通过；不得在本计划中补写导演、素材、音频、渲染器、账务状态机或发布 Saga 的替代实现。
- [ ] `server/content_domains/core.py` 的 `/api/v3/edit/*` 注册属于 Phase A Task 11；Phase D 只运行路由回归，不修改或重复提交该公共文件。若路由缺失，返回 Phase A 修复后再开始本计划。
- [ ] V3 固定使用页面 `site/workbench/ai-edit-v3.html`、API `/api/v3/edit/*`、数据库 `AI_EDIT_V3_DB_PATH`、表前缀 `edit_v3_*`、功能开关 `AI_EDIT_V3_ENABLED=0`、任务/资产模式 `ai_edit_v3`、计费键 `ai-edit-v3:*`、COS 前缀 `{environment}/ai-edit-v3/{owner_hmac}/{job_id}/...` 和日志前缀 `[ai-edit-v3]`。
- [ ] 不修改 `site/workbench/ai-edit-v2.html` 的 DOM、脚本或业务行为，不导入 V2 Store/provider/COS，不修改 `ai_edit_v2.db`；缓存戳机械更新只能出现在 Task 11 的独立 commit。
- [ ] 五类 `input_type` 固定为 `platform_talking_head`、`uploaded_video`、`existing_audio`、`uploaded_audio`、`script_to_audio_video`；三类 `creation_mode` 固定为 `ai_auto`、`style_prompt`、`template_reference`。
- [ ] Phase C 冻结的四个 `template_id` 只能为 `commercial_diagnostic_landscape_v1`、`commercial_diagnostic_portrait_v1`、`editorial_explainer_landscape_v1`、`editorial_explainer_portrait_v1`；四个首发版本的 `version` 均为整数 `1`，不得字符串化或创建别名。
- [ ] `source_asset_id`、`source_upload_id`、`tts_input` 严格互斥；`style_prompt`、`template_id` 严格按 `creation_mode` 出现；未使用字段必须省略，不能发送 `null` 或空字符串。
- [ ] 视频输入比例只能提交 `auto`；音频输入比例只能提交 `16:9` 或 `9:16` 且默认 `16:9`；页面不提供目标时长输入。
- [ ] 补充素材只接受本次 V3 流程上传的 JPEG、PNG、WebP，最多 10 张，单张最多 25 MB，单任务上传总量最多 1 GiB；不得读取历史素材、平台公共素材或其他口播视频。
- [ ] 平台口播列表只返回封面、标题、时长、比例、创建时间和 ID，不返回视频 URL；卡片不得创建 `<video>`，只有用户主动点击右侧播放按钮后才能申请有效期 300 秒的预览 URL。
- [ ] 写接口在 `AI_EDIT_V3_ENABLED=0` 时 fail closed；owner 读取既有任务、结果和短期播放地址仍可用。页面只禁用创作区，不得禁用历史任务、结果和失败原因读取。
- [ ] `POST /jobs` 与 `POST /jobs/{job_id}/retry` 的 `Idempotency-Key` 只放请求头，并在发送请求前持久化；同 owner、同 key、同请求返回同一结果，同 key、不同请求返回冲突。
- [ ] 浏览器不得依据 `/_failed$/`、`status === "done"` 或其他字符串规则推断终态、是否锁创作器、是否可重试或轮询频率；一律使用服务端返回的 `terminal`、`locks_composer`、`retryable`、`poll_after_seconds`。
- [ ] `failed_reconciliation_pending` 和 `failed_asset_decision_pending` 必须继续出现在任务中心并低频轮询，但 `locks_composer=false`；页面不得把它们显示为“已退款”或“已发布”。
- [ ] V3 最终资产只保存稳定、不可变 COS Key；播放和下载每次读取都重新签发 300 秒 GET URL，失败时清除旧 URL；验证使用 `Range: bytes=0-0` 的 GET，不使用 HEAD。
- [ ] `completed`、`refunded`、`prehold_absent` 不可重开；用户重试创建继任任务、重新报价并重新预扣，不修改前序任务。
- [ ] 所有页面文本使用业务语言，不展示内部阶段代码、供应商、模型、密钥、COS Key、render manifest、本地路径或服务端日志。
- [ ] 每项功能严格执行 RED → GREEN：先新增一个能说明缺失行为的失败测试并确认失败，再写最小实现，再运行定向测试和受影响的 V2 回归。
- [ ] `server/content_domains/video.py`、`site/workbench/cloud-shell.js`、`site/workbench/tasks.js`、`site/workbench/assets.html`、`server/admin_api.py`、`site/admin/index.html` 各自占用独立任务和独立 commit；不得把两个公共文件放入同一 commit。
- [ ] `site/workbench/assets.html` 的 Phase D 修改仅由已同步总规格第 4.3 节“在用户资产库中刷新 V3 私有播放/下载地址，并支持 V3 任务通知定位”条款授权；若执行分支不含该条款，Task 7 必须停止并先获得书面规格修订，不得默认扩大公共接点。
- [ ] 单元测试只使用协议一致的 fake 和脱敏 fixture，不调用真实 Qwen、TTS、生图、ElevenLabs、COS 或点数服务。
- [ ] 本计划不授权 push、PR merge、测试部署、服务重启、真实 Provider smoke、真实点数操作、生产价格发布、生产迁移或生产功能开启。

---

## 1. Phase D 进入条件与范围

开始 Task 1 前保存 Phase C 基线 SHA，并确认：

```powershell
python -m unittest discover -s tests -p "test_ai_edit_v3_*.py" -v
Push-Location server/ai_edit_v3_renderer
npm ci --ignore-scripts
npm test
Pop-Location
python -m unittest discover -s tests -p "test_ai_edit_v2_*.py" -v
node --test tests/test_ai_edit_v2_ui.js
python scripts/ci_validate.py
python scripts/stamp_assets.py --check
git diff --check
```

所有命令必须退出 `0`。若 A–C 尚未完成，停止 Phase D，不在网站代码中添加临时假数据、硬编码报价、模拟任务或公开 COS URL。

Phase D 包含：

- 五类主输入的选择和上传。
- 三类互斥创作入口。
- 0–10 张补充图片。
- 平台口播封面列表和主动点击预览授权。
- 报价、确认、创建幂等、轮询、历史、结果、失败语义和继任重试。
- V3 独立导航、全局任务恢复和通知跳转。
- V3 私有成片在结果页与资产库中的播放/下载。
- V3 独立价格草稿、预览、发布确认和审计。
- 测试环境运行手册与完整 V2 回归。

Phase D 不包含：

- 修改 edit-plan、Qwen、素材匹配、ElevenLabs、HyperFrames 或质检算法。
- 新增输入类型、创作模式、比例或任意时间线编辑。
- 创建音色克隆、搜索历史素材、生成 AI 视频素材。
- 部署或启用任何环境。

## 2. Frozen File Map and Commit Boundaries

### 2.1 V3-owned files

| Task | File | Responsibility |
| --- | --- | --- |
| 1 | `server/content_domains/ai_edit_v3/api.py` | HTTP 路由、错误响应、no-store headers 和 browser DTO |
| 1 | `server/content_domains/ai_edit_v3/service.py` | owner-bound catalog、预览授权、上传与公开任务投影 |
| 1 | `server/content_domains/ai_edit_v3/feature.py` | 独立 capability DTO 和写入开关 |
| 1 | `server/content_domains/ai_edit_v3/delivery.py` | 复用 Phase C 的 300 秒私有 GET 签名接口 |
| 1 | `tests/test_ai_edit_v3_api.py` | 在 Phase A 已有 API 测试中追加页面 DTO、严格边界与主动预览授权 |
| 2–3 | `site/workbench/ai-edit-v3.html` | V3 单页、DOM、状态、上传、报价、任务和结果 |
| 2–3 | `tests/test_ai_edit_v3_ui.js` | 页面行为与竞态的 `node:test` 回归 |
| 9 | `site/admin/ai-edit-v3-pricing.html` | V3 独立价格后台页面 |
| 8–10 | `tests/test_ai_edit_v3_admin_pricing.py` | V3 价格 API、页面和入口 |
| 12 | `docs/operations/ai-edit-v3-runbook.md` | 测试环境配置、验证、观测与回退 |

### 2.2 Shared files — one file per commit

| Task | Existing range | Allowed change |
| --- | --- | --- |
| 4 | `site/workbench/tasks.js:7-225` | 仅新增 V3 tracker version、active/resume/href |
| 5 | `site/workbench/cloud-shell.js:109-141`, `274-281`, `476-507`, `805-824` | 仅新增独立 capability gate、导航、通知和 tracker 版本加载 |
| 6 | `server/content_domains/video.py:772-858` | 仅为 `mode='ai_edit_v3'` 新签播放/下载 URL 并排除 legacy 覆盖 |
| 7 | `site/workbench/assets.html:569-605`, `867-940`, `1577-1598`, `1915-1931` | 仅新增 V3 私有 URL 刷新、播放失败清理和任务定位 |
| 8 | `server/admin_api.py:1269-1360`, `1556-1557`, `1696-1716` | 仅新增 V3 独立价格 API 与审计 |
| 10 | `site/admin/index.html:109-115` | 仅新增 V3 价格后台链接 |

Read-only prerequisite: `server/content_domains/core.py` must already contain the Phase A Task 11 authenticated GET/POST dispatch for `/api/v3/edit/*`. Phase D tests it and leaves the file untouched.

Create/Modify ownership is cross-phase, not local to this document: Phase A creates `api.py`, `service.py`, `feature.py`, `delivery.py` and `tests/test_ai_edit_v3_api.py`, so Phase D marks those five as **Modify**. Phase C creates only its renderer-specific runbooks and explicitly leaves the general V3 operations runbook to Phase D; `docs/operations/ai-edit-v3-runbook.md` is therefore **Create** here, together with the new V3 workbench page, UI tests, asset-library tests, pricing page and pricing tests declared in the task file lists below.

Task 11 只运行缓存戳生成器；除新 V3 页面外，以下既有页面只能出现 `cloud-shell.js?v=` 查询值的机械变化：

```text
site/workbench/ai-edit.html
site/workbench/ai-edit-v2.html
site/workbench/assets.html
site/workbench/audio.html
site/workbench/banana.html
site/workbench/bots.html
site/workbench/canvas.html
site/workbench/collect.html
site/workbench/cost.html
site/workbench/dashboard.html
site/workbench/inspiration.html
site/workbench/invite.html
site/workbench/leads.html
site/workbench/recharge.html
site/workbench/script.html
site/workbench/settings.html
site/workbench/tutorials.html
site/workbench/video.html
site/workbench/ai-edit-v3.html
```

## 3. Frozen Browser Interfaces

### 3.1 Service methods

Phase D 扩展 Master Plan 中唯一的 `EditV3Service`，不得创建第二套业务 service：

```python
class EditV3Service:
    def capabilities(self, owner: str) -> dict: ...
    def list_platform_assets(
        self, owner: str, *, cursor: str | None, limit: int
    ) -> dict: ...
    def authorize_platform_preview(
        self, owner: str, asset_id: str, *, now: int
    ) -> dict: ...
    def list_audio_assets(
        self, owner: str, *, cursor: str | None, limit: int
    ) -> dict: ...
    def list_voices(
        self, owner: str, *, cursor: str | None, limit: int
    ) -> dict: ...
    def list_templates(
        self, owner: str, *, ratio: str | None
    ) -> dict: ...
    def create_upload(
        self, owner: str, request: Mapping[str, Any], *, now: int
    ) -> dict: ...
    def complete_upload(
        self, owner: str, upload_id: str, request: Mapping[str, Any], *, now: int
    ) -> dict: ...
    def create_material(
        self, owner: str, upload_id: str, *, now: int
    ) -> dict: ...
    def quote(
        self, owner: str, request: Mapping[str, Any], *, now: int
    ) -> dict: ...
    def create_job(
        self, owner: str, request: Mapping[str, Any], quote_id: str,
        idempotency_key: str, *, now: int
    ) -> dict: ...
    def retry_job(
        self, owner: str, predecessor_job_id: str,
        idempotency_key: str, *, now: int
    ) -> dict: ...
    def get_job(self, owner: str, job_id: str) -> dict: ...
    def list_jobs(
        self, owner: str, *, cursor: str | None, limit: int
    ) -> dict: ...
    def get_plan(self, owner: str, job_id: str) -> dict: ...
    def get_result(self, owner: str, job_id: str) -> dict: ...
```

`delivery.py` 暴露 V3 最终成片的唯一签名入口：

```python
def presign_delivery_get(
    object_key: str,
    *,
    expires: int = 300,
    download_name: str | None = None,
) -> str: ...
```

平台口播来源适配器必须在签名前完成 owner 和数字化 IP provenance 校验；若来源仍是既有私有媒体而不是 V3 delivery key，`EditV3Service` 调用来源适配器的 `authorize_preview(owner, asset_id, expires=300)`，不得绕过范围校验把任意 object key 交给 V3 delivery signer。

### 3.2 HTTP routes and DTOs

| Method | Route | Response/cache contract |
| --- | --- | --- |
| GET | `/api/v3/edit/capabilities` | `CapabilityDTO`，`Cache-Control: no-store` |
| GET | `/api/v3/edit/platform-assets?cursor=&limit=20` | `PlatformAssetPage`，无视频 URL |
| GET | `/api/v3/edit/platform-assets/{asset_id}/preview` | `PlatformPreviewAuthorization`，主动播放才调用，`no-store` |
| GET | `/api/v3/edit/audio-assets?cursor=&limit=20` | owner-bound `AudioAssetPage` |
| GET | `/api/v3/edit/voices?cursor=&limit=20` | owner/public 且状态正常的 `VoicePage` |
| GET | `/api/v3/edit/templates?ratio=16%3A9` | 已发布且支持比例的模板；功能开启时非空 |
| POST | `/api/v3/edit/uploads` | 私有 PUT ticket |
| POST | `/api/v3/edit/uploads/{upload_id}/complete` | 服务端探测后的 upload |
| POST | `/api/v3/edit/materials` | 将完成的图片 upload 固化为本次 V3 material |
| POST | `/api/v3/edit/quote` | 15 分钟有效报价 |
| POST | `/api/v3/edit/jobs` | `Idempotency-Key` header + `JobDetail` |
| GET | `/api/v3/edit/jobs?cursor=&limit=20` | 当前 owner V3 历史 |
| GET | `/api/v3/edit/jobs/{job_id}` | `JobDetail` |
| GET | `/api/v3/edit/jobs/{job_id}/plan` | 脱敏 plan |
| GET | `/api/v3/edit/jobs/{job_id}/result` | 每次重新签名的结果 URL，`no-store` |
| POST | `/api/v3/edit/jobs/{job_id}/retry` | `Idempotency-Key` header + 新继任任务 |

能力响应固定为：

```json
{
  "feature": "ai_edit_v3",
  "enabled": true,
  "accepts_submissions": true,
  "read_available": true,
  "disabled_reason": "",
  "input_types": [
    "platform_talking_head",
    "uploaded_video",
    "existing_audio",
    "uploaded_audio",
    "script_to_audio_video"
  ],
  "creation_modes": [
    "ai_auto",
    "style_prompt",
    "template_reference"
  ],
  "ratios": ["16:9", "9:16"],
  "limits": {
    "material_images": 10,
    "image_bytes": 26214400,
    "task_upload_bytes": 1073741824,
    "minimum_duration_ms": 3000,
    "maximum_duration_ms": 600000
  }
}
```

列表 DTO 固定为：

```json
{
  "platform_assets": {
    "items": [{
      "id": "video_123",
      "title": "门店增长方法",
      "cover_url": "/api/private/image/opaque-cover-token",
      "duration_ms": 26808,
      "ratio": "9:16",
      "created_at": 1785373200
    }],
    "next_cursor": null
  },
  "audio_assets": {
    "items": [{
      "id": "audio_123",
      "title": "课程讲解主音频",
      "duration_ms": 84200,
      "created_at": 1785373200
    }],
    "next_cursor": null
  },
  "voices": {
    "items": [{
      "id": "voice_123",
      "name": "清晰女声",
      "kind": "cloned"
    }],
    "next_cursor": null
  },
  "templates": {
    "items": [
      {
        "id": "commercial_diagnostic_landscape_v1",
        "version": 1,
        "title": "商业诊断·横屏",
        "category": "商业诊断",
        "preview_image_url": "/api/private/image/template-commercial-landscape-token",
        "supported_ratios": ["16:9"]
      },
      {
        "id": "commercial_diagnostic_portrait_v1",
        "version": 1,
        "title": "商业诊断·竖屏",
        "category": "商业诊断",
        "preview_image_url": "/api/private/image/template-commercial-portrait-token",
        "supported_ratios": ["9:16"]
      },
      {
        "id": "editorial_explainer_landscape_v1",
        "version": 1,
        "title": "编辑式知识讲解·横屏",
        "category": "编辑式知识讲解",
        "preview_image_url": "/api/private/image/template-editorial-landscape-token",
        "supported_ratios": ["16:9"]
      },
      {
        "id": "editorial_explainer_portrait_v1",
        "version": 1,
        "title": "编辑式知识讲解·竖屏",
        "category": "编辑式知识讲解",
        "preview_image_url": "/api/private/image/template-editorial-portrait-token",
        "supported_ratios": ["9:16"]
      }
    ]
  }
}
```

平台预览授权固定为：

```json
{
  "asset_id": "video_123",
  "play_url": "/api/private/media/opaque-300-second-token",
  "expires_in": 300
}
```

响应不得出现 `preview_url`、`video_url`、`download_url`、`object_key`、`cos_key`、owner、权威正文或 provider 字段。`play_url` 只存在于预览授权响应和页面内存，不写 localStorage/sessionStorage。

上传票据与完成 DTO 固定为：

```json
{
  "upload_id": "upload_123",
  "method": "PUT",
  "upload_url": "https://signed-upload.example.invalid/object",
  "headers": {
    "Content-Type": "video/mp4",
    "x-cos-acl": "private"
  },
  "expires_in": 900
}
```

```json
{
  "upload_id": "upload_123",
  "kind": "video",
  "status": "ready",
  "mime_type": "video/mp4",
  "size_bytes": 10485760,
  "duration_ms": 26808,
  "width": 1080,
  "height": 1920
}
```

`POST /materials` 请求只含 `{"upload_id":"upload_image_01"}`，返回：

```json
{
  "id": "material_01",
  "thumbnail_url": "/api/private/image/opaque-material-token",
  "filename": "product.webp",
  "mime_type": "image/webp",
  "size_bytes": 7340032,
  "width": 1600,
  "height": 1600
}
```

报价请求不含 `quote_id`。报价响应固定为：

```json
{
  "id": "quote_123",
  "minimum_points": 86,
  "maximum_points": 132,
  "held_points": 132,
  "breakdown": [
    {"code": "base", "label": "基础创作", "minimum_points": 30, "maximum_points": 30},
    {"code": "render", "label": "渲染", "minimum_points": 56, "maximum_points": 102}
  ],
  "price_version": "ai-edit-v3-test-2026-07-30",
  "request_fingerprint": "sha256:browser-visible-fingerprint",
  "expires_at": 1785374100
}
```

创建请求必须在同一规范化输入上新增 `quote_id`。任务详情固定包含服务端权威控制字段：

```json
{
  "id": "edit_v3_job_123",
  "predecessor_job_id": null,
  "input_type": "platform_talking_head",
  "creation_mode": "style_prompt",
  "public_status": "正在生成剪辑方案",
  "progress_percent": 34,
  "terminal": false,
  "locks_composer": true,
  "retryable": false,
  "poll_after_seconds": 3,
  "created_at": 1785373200,
  "updated_at": 1785373300,
  "error": null,
  "billing": {
    "quoted_maximum_points": 132,
    "confirmed_preheld_points": 132,
    "actual_charge_points": null,
    "confirmed_refunded_points": 0,
    "reconciliation_pending": false
  },
  "result_available": false
}
```

安全性待确认任务返回 `terminal=false`、`locks_composer=false`、`retryable=false`、`poll_after_seconds=30`。终态失败只有在服务端确认可创建继任任务时才返回 `retryable=true`。

结果固定为：

```json
{
  "job_id": "edit_v3_job_123",
  "asset_id": "asset_123",
  "play_url": "https://signed-read.example.invalid/object",
  "download_url": "https://signed-download.example.invalid/object",
  "expires_in": 300,
  "billing": {
    "quoted_maximum_points": 132,
    "actual_charge_points": 105,
    "confirmed_refunded_points": 27
  }
}
```

错误响应固定为：

```json
{
  "error_code": "quote_expired",
  "message": "报价已过期，请重新获取报价",
  "stage": "preholding",
  "retryable": true
}
```

### 3.3 Strict request union

页面唯一通过 `buildJobInput({includeQuoteId})` 构造请求。五个合法形状为：

```javascript
const platformVideo = {
  input_type: "platform_talking_head",
  source_asset_id: "video_123",
  ratio: "auto"
};
const uploadedVideo = {
  input_type: "uploaded_video",
  source_upload_id: "upload_video_123",
  ratio: "auto"
};
const existingAudio = {
  input_type: "existing_audio",
  source_asset_id: "audio_123",
  ratio: "16:9"
};
const uploadedAudio = {
  input_type: "uploaded_audio",
  source_upload_id: "upload_audio_123",
  ratio: "9:16"
};
const scriptedAudio = {
  input_type: "script_to_audio_video",
  tts_input: {text: "准确文案", voice_id: "voice_123"},
  ratio: "16:9"
};
```

构造器再添加且只添加一个创作模式：

```javascript
const aiAuto = {creation_mode: "ai_auto"};
const stylePrompt = {
  creation_mode: "style_prompt",
  style_prompt: "克制、清晰的知识讲解"
};
const templateReference = {
  creation_mode: "template_reference",
  template_id: "commercial_diagnostic_landscape_v1"
};
```

最后添加 `material_asset_ids`；创建时才添加 `quote_id`。构造器不得从整个 `state` 扩展复制字段。

### 3.4 DOM, state and task tracker contract

页面必须提供以下稳定 DOM ID：

```text
inputTypeTabs
platformAssetGallery
platformAssetLoadMore
videoUploadInput
audioAssetGallery
audioAssetLoadMore
audioUploadInput
ttsText
voiceGallery
voiceLoadMore
ratioOptions
creationModeTabs
stylePromptPanel
stylePromptInput
templateGallery
materialInput
materialItems
materialCount
mainPreview
mainPreviewCover
mainPreviewPlay
selectionSummary
quotePanel
quoteRange
quoteBreakdown
primaryAction
jobPanel
jobStatus
jobProgress
jobBilling
jobError
resultPanel
resultVideo
resultPlay
resultDownload
retryAction
jobHistoryList
jobHistoryLoadMore
composerNotice
```

页面状态固定分离当前创作与当前查看对象：

```javascript
const state = {
  capability: null,
  draftRevision: 0,
  draft: {
    inputType: "platform_talking_head",
    sourceAssetId: "",
    sourceUploadId: "",
    ttsText: "",
    voiceId: "",
    ratio: "16:9",
    creationMode: "ai_auto",
    stylePrompt: "",
    templateId: "",
    materialAssetIds: []
  },
  platform: {items: [], nextCursor: null, loading: false},
  audio: {items: [], nextCursor: null, loading: false},
  voices: {items: [], nextCursor: null, loading: false},
  templates: {items: [], loading: false},
  materials: [],
  quote: null,
  submit: {
    requestFingerprint: "",
    idempotencyKey: "",
    inFlight: false
  },
  preview: {
    assetId: "",
    playUrl: "",
    revision: 0,
    controller: null
  },
  jobs: {
    currentJobId: "",
    viewedJobId: "",
    items: [],
    nextCursor: null,
    viewRevision: 0,
    pollToken: 0,
    timer: null
  }
};
```

`HQTasks` 的版本常量在两个共享文件中必须完全一致：

```javascript
const TASK_TRACKER_VERSION = "ai-edit-v3-site-v1";
```

V3 页面不得直接包含 `<script src="tasks.js">`。它只监听 `hq:tasks-ready`，并写入：

```javascript
window.HQTasks.upsert({
  id: job.id,
  kind: "ai_edit_v3",
  status: job.public_status,
  tracking: !job.terminal,
  createdAt: job.created_at * 1000
});
```

localStorage 中不得写报价正文、用户文案、媒体 URL、COS Key、错误技术详情或点数服务响应。

## 4. Task Order

```text
Task 1 browser API
  -> Task 2 V3 workspace inputs/modes/materials
  -> Task 3 quote/job/history/result/retry
  -> Task 4 shared tasks.js
  -> Task 5 shared cloud-shell.js
  -> Task 6 shared video.py
  -> Task 7 shared assets.html
  -> Task 8 shared admin_api.py
  -> Task 9 V3 admin page
  -> Task 10 shared admin index
  -> Task 11 cache stamps
  -> Task 12 runbook/final verification
```

---

### Task 1: Freeze the Browser API and Add Active Platform Preview Authorization

**Files:**
- Modify: `server/content_domains/ai_edit_v3/api.py`
- Modify: `server/content_domains/ai_edit_v3/service.py`
- Modify: `server/content_domains/ai_edit_v3/feature.py`
- Modify: `server/content_domains/ai_edit_v3/delivery.py`
- Modify: `tests/test_ai_edit_v3_api.py`

**Interfaces:**
- Consumes: Phase A `normalize_job_request`, quote fingerprint, upload store, `EditV3Service`; Phase C `presign_delivery_get`.
- Produces: all routes and DTOs in section 3.2, including `GET /platform-assets/{asset_id}/preview`.

- [ ] **Step 0: Verify the Phase A shared route without editing it**

```powershell
python -m unittest tests.test_ai_edit_v3_api -v
git diff -- server/content_domains/core.py
```

Expected: API route tests PASS and `core.py` has no Phase D diff. If the route is missing, stop and return the defect to Phase A Task 11.

- [ ] **Step 1: Write RED tests for capability/read gate and exact list DTOs**

```python
def test_disabled_feature_rejects_writes_but_allows_owner_job_reads(self):
    self.feature.disable("dependency_unavailable")
    self.assertEqual(self.get("/api/v3/edit/jobs/job_owned").status, 200)
    self.assertEqual(self.post("/api/v3/edit/quote", self.valid_request).status, 503)

def test_platform_list_is_owner_scoped_cover_only(self):
    response = self.get("/api/v3/edit/platform-assets?limit=20")
    self.assertEqual(response.status, 200)
    item = response.json["items"][0]
    self.assertEqual(
        set(item),
        {"id", "title", "cover_url", "duration_ms", "ratio", "created_at"},
    )
    self.assertNotIn("video_url", response.text)
    self.assertNotIn("preview_url", response.text)
    self.assertNotIn("cos_key", response.text)

def test_enabled_catalogs_are_owner_scoped_and_templates_are_nonempty(self):
    audio = self.get("/api/v3/edit/audio-assets?limit=20").json["items"]
    voices = self.get("/api/v3/edit/voices?limit=20").json["items"]
    templates = self.get("/api/v3/edit/templates").json["items"]
    self.assertTrue(all(item["id"] != "other_owner" for item in audio))
    self.assertTrue(all(item["id"] != "disabled_voice" for item in voices))
    expected = {
        "commercial_diagnostic_landscape_v1": ["16:9"],
        "commercial_diagnostic_portrait_v1": ["9:16"],
        "editorial_explainer_landscape_v1": ["16:9"],
        "editorial_explainer_portrait_v1": ["9:16"],
    }
    self.assertEqual(
        {item["id"]: item["supported_ratios"] for item in templates},
        expected,
    )
    self.assertTrue(all(
        set(item) == {
            "id", "version", "title", "category",
            "preview_image_url", "supported_ratios",
        }
        for item in templates
    ))
    self.assertTrue(all(item["title"].strip() for item in templates))
    self.assertEqual(
        {item["category"] for item in templates},
        {"商业诊断", "编辑式知识讲解"},
    )
    self.assertTrue(all(item["preview_image_url"] for item in templates))
    self.assertTrue(all(type(item["version"]) is int for item in templates))
    self.assertEqual({item["version"] for item in templates}, {1})
```

- [ ] **Step 2: Write RED tests for active preview authorization**

```python
def test_preview_requires_owner_and_returns_fresh_300_second_url(self):
    first = self.get("/api/v3/edit/platform-assets/video_owned/preview")
    second = self.get("/api/v3/edit/platform-assets/video_owned/preview")
    self.assertEqual(first.status, 200)
    self.assertEqual(first.json["asset_id"], "video_owned")
    self.assertEqual(first.json["expires_in"], 300)
    self.assertNotEqual(first.json["play_url"], second.json["play_url"])
    self.assertEqual(first.headers["Cache-Control"], "no-store")

def test_preview_hides_cross_owner_asset_as_not_found(self):
    response = self.get("/api/v3/edit/platform-assets/video_other/preview")
    self.assertEqual(response.status, 404)
    self.assertEqual(response.json["error_code"], "platform_asset_not_found")
```

- [ ] **Step 3: Write RED tests for uploads, materials, strict quote/create parity and public job fields**

```python
def test_upload_ticket_returns_only_explicit_private_put_headers(self):
    result = self.post("/api/v3/edit/uploads", self.video_upload_request).json
    self.assertEqual(result["method"], "PUT")
    self.assertEqual(result["headers"]["x-cos-acl"], "private")
    self.assertNotIn("cos_key", result)

def test_quote_and_create_use_identical_normalized_input(self):
    quote = self.post("/api/v3/edit/quote", self.valid_request).json
    create = dict(self.valid_request, quote_id=quote["id"])
    job = self.post(
        "/api/v3/edit/jobs",
        create,
        headers={"Idempotency-Key": "create-site-api-01"},
    ).json
    self.assertIn("terminal", job)
    self.assertIn("locks_composer", job)
    self.assertIn("retryable", job)
    self.assertIn("poll_after_seconds", job)

def test_same_idempotency_key_with_changed_body_conflicts(self):
    self.create_job("create-site-api-02", self.valid_create_request)
    changed = dict(self.valid_create_request, material_asset_ids=["material_02"])
    response = self.create_job("create-site-api-02", changed)
    self.assertEqual(response.status, 409)
    self.assertEqual(response.json["error_code"], "idempotency_conflict")
```

- [ ] **Step 4: Run the new tests and verify RED**

Run:

```powershell
python -m unittest tests.test_ai_edit_v3_api -v
```

Expected: FAIL because the dedicated preview route and/or exact public DTO projection is absent.

- [ ] **Step 5: Implement owner-bound projections and dedicated preview authorization**

Use exact projection sets; never return a database row with `dict(row)`:

```python
def platform_asset_summary(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "id": str(row["id"]),
        "title": str(row["title"]),
        "cover_url": str(row["cover_url"]),
        "duration_ms": int(row["duration_ms"]),
        "ratio": str(row["ratio"]),
        "created_at": int(row["created_at"]),
    }

def template_summary(
    self, template: TemplateContract
) -> dict[str, Any]:
    return {
        "id": template.template_id,
        "version": template.version,
        "title": template.title,
        "category": template.category,
        "preview_image_url": self.template_catalog.authorize_preview_image(
            template.preview_relative_path, expires=300
        ),
        "supported_ratios": list(template.supported_ratios),
    }

def authorize_platform_preview(
    self, owner: str, asset_id: str, *, now: int
) -> dict[str, Any]:
    source = self.source_catalog.require_owned_talking_head(owner, asset_id)
    return {
        "asset_id": str(source.id),
        "play_url": self.source_catalog.authorize_preview(
            owner, str(source.id), expires=300
        ),
        "expires_in": 300,
    }
```

Make preview/result handlers send:

```python
self.send_header("Cache-Control", "no-store")
self.send_header("Pragma", "no-cache")
```

Map domain errors to the exact section 3.2 error DTO. Do not expose exception strings for unknown errors.

- [ ] **Step 6: Implement capability, upload/material and job response boundaries**

```python
def public_job(job: Mapping[str, Any]) -> dict[str, Any]:
    policy = public_job_policy(job)
    return {
        "id": str(job["id"]),
        "predecessor_job_id": job.get("predecessor_job_id"),
        "input_type": str(job["input_type"]),
        "creation_mode": str(job["creation_mode"]),
        "public_status": policy.label,
        "progress_percent": policy.progress_percent,
        "terminal": policy.terminal,
        "locks_composer": policy.locks_composer,
        "retryable": policy.retryable,
        "poll_after_seconds": policy.poll_after_seconds,
        "created_at": int(job["created_at"]),
        "updated_at": int(job["updated_at"]),
        "error": public_error(job),
        "billing": public_billing(job),
        "result_available": bool(policy.result_available),
    }
```

`failed_reconciliation_pending` and `failed_asset_decision_pending` must map to `locks_composer=False`, `terminal=False`, `poll_after_seconds=30`.

- [ ] **Step 7: Run GREEN tests and V3 API regressions**

```powershell
python -m unittest tests.test_ai_edit_v3_api tests.test_ai_edit_v3_feature -v
```

Expected: PASS.

- [ ] **Step 8: Run the directly affected V2 API/asset regressions**

```powershell
python -m unittest tests.test_ai_edit_v2_api tests.test_ai_edit_v2_asset_library tests.test_ai_edit_v2_feature -v
```

Expected: PASS with no V2 file changes.

- [ ] **Step 9: Commit only V3-owned browser API files**

```powershell
git add server/content_domains/ai_edit_v3/api.py server/content_domains/ai_edit_v3/service.py server/content_domains/ai_edit_v3/feature.py server/content_domains/ai_edit_v3/delivery.py tests/test_ai_edit_v3_api.py
git commit -m "feat(ai-edit-v3): expose site delivery api"
```

---

### Task 2: Create the Gated Single-Page Workspace and Stable State Model

**Files:**
- Create: `site/workbench/ai-edit-v3.html`
- Create: `tests/test_ai_edit_v3_ui.js`

**Interfaces:**
- Consumes: `GET /api/v3/edit/capabilities`, section 3.4 DOM IDs.
- Produces: stable DOM/state/render skeleton and read-vs-write feature gate used by Task 2B、Task 2C 和 Task 3.

- [ ] **Step 1: Write RED tests for the page shell, DOM contract and no direct tasks script**

```javascript
test("V3 page exposes the frozen composer and inspector DOM", () => {
  for (const id of requiredIds) {
    assert.match(page, new RegExp(`id=["']${id}["']`));
  }
  assert.doesNotMatch(page, /<script[^>]+src=["'][^"']*tasks\.js/i);
  assert.match(page, /data-page=["']ai_edit_v3["']/);
  assert.match(page, />补充素材</);
  assert.doesNotMatch(page, />参考素材</);
});

test("feature off disables composer only and preserves history reads", async () => {
  const app = await boot({accepts_submissions: false, read_available: true});
  assert.equal(app.element("primaryAction").disabled, true);
  assert.equal(app.element("jobHistoryLoadMore").disabled, false);
  assert.equal(app.fetchCount("/api/v3/edit/jobs"), 1);
});
```

- [ ] **Step 2: Run the test and verify RED**

```powershell
node --test tests/test_ai_edit_v3_ui.js
```

Expected: FAIL because `ai-edit-v3.html` does not exist.

- [ ] **Step 3: Create semantic layout and all frozen IDs**

Use this fixed hierarchy:

```html
<main id="app" data-page="ai_edit_v3">
  <section class="composer" aria-label="AI 智能剪辑 V3 创作区">
    <div id="inputTypeTabs" role="tablist"></div>
    <section id="sourceStep"></section>
    <section id="creationStep"></section>
    <section id="materialStep"></section>
  </section>
  <aside class="inspector" aria-label="创作检查器">
    <section id="mainPreview"></section>
    <section id="selectionSummary"></section>
    <section id="quotePanel"></section>
    <button id="primaryAction" type="button">获取报价</button>
    <section id="jobPanel"></section>
    <section id="resultPanel"></section>
  </aside>
  <section id="jobHistoryList" aria-label="历史任务"></section>
</main>
```

Implement responsive two-column desktop and single-column mobile layout; inspector remains sticky only when viewport width permits. Respect `prefers-reduced-motion`.

`platformAssetGallery` uses one horizontal scrolling row and `aspect-ratio:9/16` cover cards. `templateGallery` uses a compact horizontal row of `9:16` image cards. The third section heading is exactly “补充素材”.

- [ ] **Step 4: Add the exact state object and render boundary**

```javascript
function setState(mutator) {
  mutator(state);
  render();
}

function invalidateQuote() {
  state.draftRevision += 1;
  state.quote = null;
  state.submit.requestFingerprint = "";
  state.submit.idempotencyKey = "";
}

function setComposerEnabled(enabled, reason) {
  document.querySelectorAll(".composer input,.composer button,.composer textarea")
    .forEach((element) => { element.disabled = !enabled; });
  document.getElementById("composerNotice").textContent = enabled ? "" : reason;
}
```

Never disable history/result controls through a page-wide selector.

- [ ] **Step 5: Add capability bootstrap and authenticated JSON helper**

```javascript
async function requestJson(path, options = {}) {
  const response = await fetch(path, {
    credentials: "same-origin",
    cache: "no-store",
    ...options
  });
  const data = await response.json();
  if (!response.ok) {
    const error = new Error(data.message || "请求失败");
    error.code = data.error_code || "request_failed";
    error.retryable = Boolean(data.retryable);
    throw error;
  }
  return data;
}
```

On init, load capability and job history independently with `Promise.allSettled`; capability failure must not erase an already loaded history result.

- [ ] **Step 6: Run GREEN page-shell tests**

```powershell
node --test tests/test_ai_edit_v3_ui.js
```

Expected: PASS for shell/gate tests.

- [ ] **Step 7: Keep the page foundation in the Task 2 working set**

```powershell
git diff --check -- site/workbench/ai-edit-v3.html tests/test_ai_edit_v3_ui.js
```

---

#### Task 2B: Implement Five Primary Inputs, Lazy Platform Covers and Upload Sources

**Files:**
- Modify: `site/workbench/ai-edit-v3.html`
- Modify: `tests/test_ai_edit_v3_ui.js`

**Interfaces:**
- Consumes: platform/audio/voice lists, preview authorization, upload ticket/complete DTOs.
- Produces: `selectInputType`, `selectPlatformAsset`, `playSelectedPlatformAsset`, `uploadSource`, `selectAudioAsset`, `selectVoice`.

- [ ] **Step 1: Write RED tests for all five input variants**

```javascript
test("five input tabs build five strict primary unions", async () => {
  const app = await bootReady();
  assert.deepEqual(app.build("platform_talking_head"), {
    input_type: "platform_talking_head",
    source_asset_id: "video_123",
    ratio: "auto"
  });
  assert.deepEqual(app.build("uploaded_video"), {
    input_type: "uploaded_video",
    source_upload_id: "upload_video_123",
    ratio: "auto"
  });
  assert.deepEqual(app.build("existing_audio"), {
    input_type: "existing_audio",
    source_asset_id: "audio_123",
    ratio: "16:9"
  });
  assert.deepEqual(app.build("uploaded_audio"), {
    input_type: "uploaded_audio",
    source_upload_id: "upload_audio_123",
    ratio: "16:9"
  });
  assert.deepEqual(app.build("script_to_audio_video"), {
    input_type: "script_to_audio_video",
    tts_input: {text: "准确文案", voice_id: "voice_123"},
    ratio: "16:9"
  });
});
```

- [ ] **Step 2: Write RED tests for cover-only gallery and active preview races**

```javascript
test("platform gallery uses lazy images and creates no video element", async () => {
  const app = await bootReady();
  await app.loadPlatformAssets();
  assert.equal(app.queryAll("#platformAssetGallery video").length, 0);
  assert.equal(app.query("#platformAssetGallery img").loading, "lazy");
  await app.selectPlatformAsset("video_123");
  assert.equal(app.fetchCount("/platform-assets/video_123/preview"), 0);
});

test("play fetches only selected asset and stale authorization cannot win", async () => {
  const app = await bootReadyWithDeferredPreview();
  app.selectPlatformAsset("video_123");
  const oldRequest = app.playSelectedPlatformAsset();
  app.selectPlatformAsset("video_456");
  app.resolvePreview("video_123", "/expired-old-url");
  await oldRequest;
  assert.equal(app.state.preview.playUrl, "");
  assert.equal(app.queryAll("#mainPreview video").length, 0);
});
```

- [ ] **Step 3: Write RED upload tests**

```javascript
test("source upload applies only ticket headers and completes before selection", async () => {
  const app = await bootReady();
  await app.uploadSource(file("talk.mp4", "video/mp4", 1048576));
  assert.deepEqual(app.uploadHeaders(), {
    "Content-Type": "video/mp4",
    "x-cos-acl": "private"
  });
  assert.equal(app.lastCompletePath(), "/api/v3/edit/uploads/upload_video_123/complete");
  assert.equal(app.state.draft.sourceUploadId, "upload_video_123");
});
```

- [ ] **Step 4: Run RED tests**

```powershell
node --test --test-name-pattern="input|platform|preview|source upload" tests/test_ai_edit_v3_ui.js
```

Expected: FAIL because the input renderers and active preview/upload functions are absent.

- [ ] **Step 5: Render the five mutually exclusive source panels**

```javascript
const INPUT_TYPES = [
  ["platform_talking_head", "平台口播"],
  ["uploaded_video", "上传视频"],
  ["existing_audio", "已有音频"],
  ["uploaded_audio", "上传音频"],
  ["script_to_audio_video", "文案生成音频"]
];

function selectInputType(inputType) {
  state.draft.inputType = inputType;
  state.draft.sourceAssetId = "";
  state.draft.sourceUploadId = "";
  state.draft.ttsText = "";
  state.draft.voiceId = "";
  state.draft.ratio = inputType.includes("video") ||
    inputType === "platform_talking_head" ? "auto" : "16:9";
  stopSubjectPreview();
  invalidateQuote();
  render();
}
```

Only render `ratioOptions` for the three audio inputs.

- [ ] **Step 6: Implement cover-only pagination and race-safe active preview**

```javascript
async function playSelectedPlatformAsset() {
  const assetId = state.draft.sourceAssetId;
  if (!assetId) return;
  const revision = ++state.preview.revision;
  if (state.preview.controller) state.preview.controller.abort();
  const controller = new AbortController();
  state.preview.controller = controller;
  const data = await requestJson(
    `/api/v3/edit/platform-assets/${encodeURIComponent(assetId)}/preview`,
    {signal: controller.signal}
  );
  if (revision !== state.preview.revision ||
      assetId !== state.draft.sourceAssetId) return;
  state.preview.playUrl = data.play_url;
  renderPlayingPreview(data.play_url);
}
```

Cards use `<img loading="lazy" decoding="async">`. `selectPlatformAsset` updates cover/title/duration only.

- [ ] **Step 7: Implement scoped XHR upload without a global monkey patch**

```javascript
function putUpload(ticket, file, onProgress) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open(ticket.method, ticket.upload_url, true);
    Object.entries(ticket.headers).forEach(([name, value]) => {
      xhr.setRequestHeader(name, value);
    });
    xhr.upload.onprogress = (event) => {
      if (event.lengthComputable) onProgress(event.loaded, event.total);
    };
    xhr.onload = () => xhr.status >= 200 && xhr.status < 300
      ? resolve()
      : reject(new Error("上传失败"));
    xhr.onerror = () => reject(new Error("上传失败"));
    xhr.send(file);
  });
}
```

Ticket body includes only `kind`, `filename`, `content_type`, `size_bytes`. After PUT, call complete and trust server probe values.

- [ ] **Step 8: Run GREEN tests and full V3 UI file**

```powershell
node --test tests/test_ai_edit_v3_ui.js
```

Expected: PASS.

- [ ] **Step 9: Keep the five-input changes staged for the Task 2 commit**

```powershell
git diff --check -- site/workbench/ai-edit-v3.html tests/test_ai_edit_v3_ui.js
```

---

#### Task 2C: Add Three Creation Modes, Template Cards and Ten Supplemental Images

**Files:**
- Modify: `site/workbench/ai-edit-v3.html`
- Modify: `tests/test_ai_edit_v3_ui.js`

**Interfaces:**
- Consumes: `GET /templates`, image upload/complete and `POST /materials`.
- Produces: strict creation-mode payload and ordered `material_asset_ids`.

- [ ] **Step 1: Write RED tests for three strict modes**

```javascript
test("creation modes omit unused fields instead of sending null", async () => {
  const app = await bootReady();
  assert.deepEqual(app.creation("ai_auto"), {creation_mode: "ai_auto"});
  assert.deepEqual(app.creation("style_prompt"), {
    creation_mode: "style_prompt",
    style_prompt: "真实、克制"
  });
  assert.deepEqual(app.creation("template_reference"), {
    creation_mode: "template_reference",
    template_id: "commercial_diagnostic_landscape_v1"
  });
});

test("template cards are image-only and filtered by effective ratio", async () => {
  const app = await bootReady();
  await app.selectCreationMode("template_reference");
  assert.equal(app.queryAll("#templateGallery video").length, 0);
  assert.equal(app.lastTemplateQuery(), "ratio=16%3A9");
  assert.deepEqual(app.templateIds(), [
    "commercial_diagnostic_landscape_v1",
    "editorial_explainer_landscape_v1"
  ]);
  assert.ok(app.templateItems().every((item) =>
    Number.isInteger(item.version) && item.version === 1
  ));
});
```

- [ ] **Step 2: Write RED material boundary tests**

```javascript
test("supplemental materials accept images only and stop at ten", async () => {
  const app = await bootReady();
  await app.addMaterials(Array.from({length: 11}, (_, i) =>
    file(`image-${i}.webp`, "image/webp", 1024)
  ));
  assert.equal(app.state.draft.materialAssetIds.length, 10);
  assert.equal(app.postCount("/api/v3/edit/materials"), 10);
  assert.equal(app.element("materialCount").textContent, "10 / 10");
});

test("draft mutation invalidates quote and submission key", async () => {
  const app = await bootWithQuote();
  app.selectTemplate("editorial_explainer_landscape_v1");
  assert.equal(app.state.quote, null);
  assert.equal(app.state.submit.idempotencyKey, "");
});
```

- [ ] **Step 3: Run RED tests**

```powershell
node --test --test-name-pattern="creation modes|template|supplemental|invalidates quote" tests/test_ai_edit_v3_ui.js
```

Expected: FAIL because modes/templates/materials are not implemented.

- [ ] **Step 4: Implement strict creation-mode selection**

```javascript
function creationModePayload() {
  if (state.draft.creationMode === "ai_auto") {
    return {creation_mode: "ai_auto"};
  }
  if (state.draft.creationMode === "style_prompt") {
    return {
      creation_mode: "style_prompt",
      style_prompt: state.draft.stylePrompt.trim()
    };
  }
  return {
    creation_mode: "template_reference",
    template_id: state.draft.templateId
  };
}
```

Enforce 1–1000 characters for style prompt before quote. Render published templates as compact `9:16` image cards; do not use a `<select>` or video.

- [ ] **Step 5: Implement image upload → complete → material promotion**

```javascript
async function uploadMaterial(file) {
  if (!["image/jpeg", "image/png", "image/webp"].includes(file.type)) {
    throw new Error("补充素材只支持 JPEG、PNG、WebP");
  }
  if (state.materials.length >= 10) {
    throw new Error("单次创作最多上传 10 张图片");
  }
  const completed = await uploadFile("image", file);
  const material = await requestJson("/api/v3/edit/materials", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({upload_id: completed.upload_id})
  });
  state.materials.push(material);
  state.draft.materialAssetIds = state.materials.map((item) => item.id);
  invalidateQuote();
  render();
}
```

Removal updates the ordered ID list but does not issue an undocumented destructive API call.

- [ ] **Step 6: Run GREEN tests**

```powershell
node --test tests/test_ai_edit_v3_ui.js
```

Expected: PASS.

- [ ] **Step 7: Commit the complete Task 2 workspace**

```powershell
git add site/workbench/ai-edit-v3.html tests/test_ai_edit_v3_ui.js
git commit -m "feat(ai-edit-v3): add workspace inputs and materials"
```

---

### Task 3: Add Quote, Job Polling, History, Result and Successor Retry

**Files:**
- Modify: `site/workbench/ai-edit-v3.html`
- Modify: `tests/test_ai_edit_v3_ui.js`

**Interfaces:**
- Consumes: `POST /quote`, `POST /jobs`, quote fingerprint and 15-minute expiry.
- Produces: `buildJobInput`, `requestQuote`, `confirmJob`, one-button quote/confirm state.

- [ ] **Step 1: Write RED strict-union and quote tests**

```javascript
test("quote body contains one source and no quote id", async () => {
  const app = await bootReady();
  await app.requestQuote();
  const body = app.lastJson("/api/v3/edit/quote");
  assert.equal(body.source_asset_id, "video_123");
  assert.equal("source_upload_id" in body, false);
  assert.equal("tts_input" in body, false);
  assert.equal("quote_id" in body, false);
});

test("one primary button changes from quote to explicit confirmation", async () => {
  const app = await bootReady();
  await app.clickPrimary();
  assert.equal(app.element("primaryAction").textContent, "确认并开始创作");
  assert.equal(app.postCount("/api/v3/edit/jobs"), 0);
  await app.clickPrimary();
  assert.equal(app.postCount("/api/v3/edit/jobs"), 1);
});
```

- [ ] **Step 2: Write RED idempotency and stale-response tests**

```javascript
test("create key is persisted before POST and double click sends once", async () => {
  const app = await bootWithQuote();
  const first = app.confirmJob();
  const second = app.confirmJob();
  assert.equal(app.postCount("/api/v3/edit/jobs"), 1);
  assert.match(app.sessionKeys()[0], /^ai_edit_v3:create:/);
  assert.ok(app.header("Idempotency-Key"));
  await Promise.all([first, second]);
});

test("old quote response cannot overwrite a newer draft", async () => {
  const app = await bootWithDeferredQuote();
  const old = app.requestQuote();
  app.setStylePrompt("新的要求");
  app.resolveOldQuote();
  await old;
  assert.equal(app.state.quote, null);
});
```

- [ ] **Step 3: Run RED tests**

```powershell
node --test --test-name-pattern="quote|primary button|idempotency|old quote" tests/test_ai_edit_v3_ui.js
```

Expected: FAIL because quote/create state is absent.

- [ ] **Step 4: Implement the only request builder**

```javascript
function buildJobInput({includeQuoteId = false} = {}) {
  const body = {
    ...primaryInputPayload(),
    ...creationModePayload(),
    material_asset_ids: [...state.draft.materialAssetIds]
  };
  if (includeQuoteId) body.quote_id = state.quote.id;
  return body;
}
```

Validate locally for usability, but let the server remain authoritative.

- [ ] **Step 5: Implement revision-safe quote**

```javascript
async function requestQuote() {
  const revision = state.draftRevision;
  const body = buildJobInput();
  const quote = await requestJson("/api/v3/edit/quote", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify(body)
  });
  if (revision !== state.draftRevision) return;
  state.quote = quote;
  state.submit.requestFingerprint = quote.request_fingerprint;
  render();
}
```

Render minimum–maximum points, itemized breakdown, price version and expiry. Do not require a billing checkbox.

- [ ] **Step 6: Persist the create key before sending**

```javascript
function createKeyStorageName(fingerprint) {
  return `ai_edit_v3:create:${fingerprint}`;
}

function ensureCreateKey(fingerprint) {
  const storageName = createKeyStorageName(fingerprint);
  let key = sessionStorage.getItem(storageName);
  if (!key) {
    key = crypto.randomUUID();
    sessionStorage.setItem(storageName, key);
  }
  return {storageName, key};
}
```

`confirmJob` sets `inFlight=true` before the first await, uses the header, and removes the stored key only after a definitive successful job response. Network uncertainty preserves it for exact replay. `quote_expired` clears the quote and returns the button to “获取报价”.

- [ ] **Step 7: Run GREEN tests**

```powershell
node --test tests/test_ai_edit_v3_ui.js
```

Expected: PASS.

- [ ] **Step 8: Keep quote/create changes in the Task 3 working set**

```powershell
git diff --check -- site/workbench/ai-edit-v3.html tests/test_ai_edit_v3_ui.js
```

---

#### Task 3B: Add Polling, History, Safety-Pending States, Result and Successor Retry

**Files:**
- Modify: `site/workbench/ai-edit-v3.html`
- Modify: `tests/test_ai_edit_v3_ui.js`

**Interfaces:**
- Consumes: jobs list/detail/result/retry DTOs and optional `HQTasks`.
- Produces: `pollJob`, `openJob`, `loadJobHistory`, `loadResult`, `retryJob`, `hq:resume-task`.

- [ ] **Step 1: Write RED tests for non-overlapping server-paced polling**

```javascript
test("polling uses recursive timeout and never overlaps requests", async () => {
  const app = await bootWithRunningJob({poll_after_seconds: 7});
  app.pollJob("edit_v3_job_123");
  app.advanceClock(6999);
  assert.equal(app.jobGetCount(), 1);
  app.resolveJobGet();
  app.advanceClock(7000);
  assert.equal(app.jobGetCount(), 2);
  assert.equal(app.setIntervalCalls(), 0);
});

test("stale viewed job response cannot replace the current view", async () => {
  const app = await bootWithDeferredJobs();
  const old = app.openJob("job_old");
  const current = app.openJob("job_new");
  app.resolveJob("job_old");
  await old;
  assert.equal(app.state.jobs.viewedJobId, "job_new");
  await current;
});
```

- [ ] **Step 2: Write RED tests for safety-pending, terminal release and history separation**

```javascript
test("safety pending remains tracked but unlocks a new creation", async () => {
  const app = await bootWithJob({
    id: "job_pending",
    terminal: false,
    locks_composer: false,
    retryable: false,
    poll_after_seconds: 30,
    public_status: "正在核对点数"
  });
  assert.equal(app.element("primaryAction").disabled, false);
  assert.equal(app.lastTrackedJob().tracking, true);
  assert.doesNotMatch(app.element("jobStatus").textContent, /已退款|已发布/);
});

test("viewing history does not replace current composer job", async () => {
  const app = await bootWithCurrentJob("job_current");
  await app.openJob("job_history");
  assert.equal(app.state.jobs.currentJobId, "job_current");
  assert.equal(app.state.jobs.viewedJobId, "job_history");
});
```

- [ ] **Step 3: Write RED result/retry tests**

```javascript
test("result is fetched fresh and signed urls are never persisted", async () => {
  const app = await bootWithCompletedJob();
  await app.loadResult("job_done");
  await app.loadResult("job_done");
  assert.equal(app.resultGetCount(), 2);
  assert.doesNotMatch(app.localStorageText(), /signed-read|signed-download/);
});

test("retry persists a new key and keeps predecessor immutable", async () => {
  const app = await bootWithRetryableJob("job_failed");
  await app.retryJob("job_failed");
  assert.ok(app.header("Idempotency-Key"));
  assert.equal(app.lastJson("/jobs/job_failed/retry"), undefined);
  assert.equal(app.state.jobs.currentJobId, "job_successor");
});
```

- [ ] **Step 4: Run RED tests**

```powershell
node --test --test-name-pattern="polling|stale viewed|safety pending|history|result|retry" tests/test_ai_edit_v3_ui.js
```

Expected: FAIL because job orchestration is absent.

- [ ] **Step 5: Implement tokenized recursive polling**

```javascript
async function pollJob(jobId) {
  const token = ++state.jobs.pollToken;
  clearTimeout(state.jobs.timer);
  const job = await requestJson(
    `/api/v3/edit/jobs/${encodeURIComponent(jobId)}`
  );
  if (token !== state.jobs.pollToken) return;
  applyJob(job);
  if (!job.terminal) {
    const delay = Math.max(1, Number(job.poll_after_seconds || 3)) * 1000;
    state.jobs.timer = setTimeout(() => pollJob(jobId), delay);
  }
}
```

Do not infer state from `public_status`. Only `currentJobId` controls composer lock/progress; `viewedJobId` controls inspector/history display.

- [ ] **Step 6: Implement cursor history, result freshness and retry idempotency**

```javascript
async function loadResult(jobId) {
  const result = await requestJson(
    `/api/v3/edit/jobs/${encodeURIComponent(jobId)}/result`
  );
  renderResult(result);
}

async function retryJob(jobId) {
  const storageName = `ai_edit_v3:retry:${jobId}`;
  let key = sessionStorage.getItem(storageName);
  if (!key) {
    key = crypto.randomUUID();
    sessionStorage.setItem(storageName, key);
  }
  const successor = await requestJson(
    `/api/v3/edit/jobs/${encodeURIComponent(jobId)}/retry`,
    {method: "POST", headers: {"Idempotency-Key": key}}
  );
  sessionStorage.removeItem(storageName);
  setCurrentJob(successor);
}
```

Accept canonical `?job=edit_v3_job_123`; also accept `?task=` for notification compatibility. On `hq:resume-task`, verify `detail.kind === "ai_edit_v3"` before opening.

- [ ] **Step 7: Integrate with HQTasks without making it a hard dependency**

```javascript
function trackJob(job) {
  if (!window.HQTasks) return;
  window.HQTasks.upsert({
    id: job.id,
    kind: "ai_edit_v3",
    status: job.public_status,
    tracking: !job.terminal,
    createdAt: job.created_at * 1000
  });
}
```

Listen once for `hq:tasks-ready` and re-track the current job.

- [ ] **Step 8: Run GREEN tests and V2 page regression**

```powershell
node --test tests/test_ai_edit_v3_ui.js tests/test_ai_edit_v2_ui.js
```

Expected: PASS; V2 test count does not decrease.

- [ ] **Step 9: Commit the complete Task 3 quote and job UX**

```powershell
git add site/workbench/ai-edit-v3.html tests/test_ai_edit_v3_ui.js
git commit -m "feat(ai-edit-v3): add quote and job delivery ux"
```

---

### Task 4: Extend the Shared Task Tracker for V3

**Files:**
- Modify: `site/workbench/tasks.js:7-225`
- Modify: `tests/test_video_global_tasks.py`
- Modify: `tests/test_ai_edit_v3_ui.js`

**Interfaces:**
- Consumes: V3 task metadata `{id,kind,status,tracking,createdAt}`.
- Produces: `HQTasks.version === "ai-edit-v3-site-v1"`, V3 badge, href and same-page resume.

- [ ] **Step 1: Write RED tracker tests**

```javascript
window.HQTasks = {version: "legacy-tracker"};
eval(tasksSource);
const job = {
  id: "edit_v3_job_123",
  kind: "ai_edit_v3",
  status: "正在核对点数",
  tracking: true
};
window.HQTasks.upsert(job);
assert.equal(window.HQTasks.version, "ai-edit-v3-site-v1");
assert.equal(window.HQTasks.latestActive("ai_edit_v3").id, job.id);
assert.equal(
  window.HQTasks.taskHref(job),
  "ai-edit-v3.html?job=edit_v3_job_123"
);
```

Also assert same-page click dispatches `hq:resume-task` with `{id,kind:"ai_edit_v3"}`.

- [ ] **Step 2: Run RED tests**

```powershell
python -m unittest tests.test_video_global_tasks -v
```

Expected: FAIL because the tracker has no version or V3 href/resume branch.

- [ ] **Step 3: Add version-aware replacement and explicit active policy**

```javascript
var TASK_TRACKER_VERSION = "ai-edit-v3-site-v1";
if (window.HQTasks && window.HQTasks.version === TASK_TRACKER_VERSION) {
  window.HQTasks.renderBadge();
  window.dispatchEvent(new CustomEvent("hq:tasks-ready"));
  return;
}

function isActive(job) {
  if (storedKind(job) === "ai_edit_v3" &&
      typeof job.tracking === "boolean") return job.tracking;
  return Boolean(ACTIVE[job.status]);
}
```

Replace all direct `ACTIVE[j.status]` checks with `isActive(j)`.

- [ ] **Step 4: Add V3 kind, href and same-page resume**

```javascript
function taskKind(job) {
  if (job && job.kind === "ai_edit_v3") return "ai_edit_v3";
  if (job && job.kind === "ai_edit_v2") return "ai_edit_v2";
  return job && job.kind === "video" ? "video" : "leads";
}

function taskHref(job) {
  var id = encodeURIComponent(String(job && job.id != null ? job.id : ""));
  var kind = taskKind(job);
  if (kind === "ai_edit_v3") return "ai-edit-v3.html?job=" + id;
  if (kind === "ai_edit_v2") return "ai-edit-v2.html?task=" + id;
  return kind === "video" ? "video.html?task=" + id : "leads.html#task=" + id;
}
```

Export `version`, `taskKind` and `taskHref` for deterministic tests.

- [ ] **Step 5: Run tracker and V2 task regressions**

```powershell
python -m unittest tests.test_video_global_tasks -v
node --test tests/test_ai_edit_v3_ui.js tests/test_ai_edit_v2_ui.js
```

Expected: PASS.

- [ ] **Step 6: Confirm one shared production file and commit**

```powershell
git diff --name-only
git add site/workbench/tasks.js tests/test_video_global_tasks.py tests/test_ai_edit_v3_ui.js
git commit -m "feat(ai-edit-v3): extend global task tracking"
```

---

### Task 5: Add Independent V3 Navigation, Notices and Cache-Safe Tracker Loading

**Files:**
- Modify: `site/workbench/cloud-shell.js:109-141`
- Modify: `site/workbench/cloud-shell.js:274-281`
- Modify: `site/workbench/cloud-shell.js:476-507`
- Modify: `site/workbench/cloud-shell.js:805-824`
- Modify: `tests/test_ai_edit_dual_entry.js`
- Modify: `tests/test_cloud_shell_sidebar.js`
- Modify: `tests/test_notify_task_focus.py`

**Interfaces:**
- Consumes: independent V2/V3 capabilities and `HQTasks.version`.
- Produces: gated V3 nav, correct notices and forced replacement of a legacy `tasks.js?v=task9` tracker.

- [ ] **Step 1: Write RED independent-gate tests**

```javascript
test("V2 and V3 navigation gates are independent", async () => {
  const shell = await bootShell({
    "/api/v2/edit/capabilities": {accepts_submissions: false},
    "/api/v3/edit/capabilities": {accepts_submissions: true}
  });
  assert.equal(shell.navVisible("ai_edit_v2"), false);
  assert.equal(shell.navVisible("ai_edit_v3"), true);
  assert.equal(shell.navHref("ai_edit_v3"), "ai-edit-v3.html");
});
```

- [ ] **Step 2: Write RED notice and stale-tracker tests**

```javascript
test("V3 safety-pending notice never claims refund or publication", () => {
  const notice = buildNotice({
    task_id: "edit_v3_job_123",
    kind: "ai_edit_v3",
    status: "failed_reconciliation_pending"
  });
  assert.equal(notice.href, "ai-edit-v3.html?job=edit_v3_job_123");
  assert.doesNotMatch(notice.title + notice.detail, /已退款|已发布/);
});

test("legacy task tracker is replaced with the frozen V3 tracker version", () => {
  window.HQTasks = {version: "legacy-tracker"};
  loadTaskTracker();
  assert.match(insertedScript.src, /tasks\.js\?v=/);
});
```

- [ ] **Step 3: Run RED tests**

```powershell
node --test tests/test_ai_edit_dual_entry.js tests/test_cloud_shell_sidebar.js
python -m unittest tests.test_notify_task_focus tests.test_video_global_tasks -v
```

Expected: FAIL because V3 nav/notice/version logic is absent.

- [ ] **Step 4: Split the two capabilities and add V3 navigation**

```javascript
var NAV_PAGES = {
  ai_edit_v2: "ai-edit-v2.html",
  ai_edit_v3: "ai-edit-v3.html"
};
var aiEditV2Visible = false;
var aiEditV3Visible = false;
```

Each nav item uses a distinct gate key. Fetch both capability endpoints with independent `.catch`; one failure cannot hide or reveal the other product.

- [ ] **Step 5: Add V3 notice semantics**

Completed V3 tasks may link to `assets.html?cat=video&task={job_id}`. Active, failed and safety-pending V3 tasks link to `ai-edit-v3.html?job={job_id}`. Treat:

```javascript
var v3Completed = status === "completed";
var v3Failure = status === "refunded" || status === "prehold_absent";
var v3Pending = status === "failed_reconciliation_pending" ||
  status === "failed_asset_decision_pending";
```

V3 pending title is “任务正在核对”，not “任务生成失败”.

- [ ] **Step 6: Make tracker loading version-aware**

```javascript
var TASK_TRACKER_VERSION = "ai-edit-v3-site-v1";
function loadTaskTracker() {
  if (window.HQTasks &&
      window.HQTasks.version === TASK_TRACKER_VERSION) {
    window.HQTasks.renderBadge();
    return;
  }
  var old = document.querySelector("script[data-hq-tasks]");
  if (old) old.remove();
  var script = document.createElement("script");
  script.setAttribute("data-hq-tasks", "1");
  script.src = "tasks.js?v=" + encodeURIComponent(TASK_TRACKER_VERSION);
  document.head.appendChild(script);
}
```

This is required because `ai-edit-v2.html` currently loads `tasks.js?v=task9` before the shell. The new `tasks.js` from Task 4 intentionally replaces a tracker whose version differs.

- [ ] **Step 7: Run GREEN shell/notice/task tests**

```powershell
node --test tests/test_ai_edit_dual_entry.js tests/test_cloud_shell_sidebar.js
python -m unittest tests.test_notify_task_focus tests.test_video_global_tasks -v
```

Expected: PASS.

- [ ] **Step 8: Confirm one shared production file and commit**

```powershell
git diff --name-only
git add site/workbench/cloud-shell.js tests/test_ai_edit_dual_entry.js tests/test_cloud_shell_sidebar.js tests/test_notify_task_focus.py
git commit -m "feat(ai-edit-v3): add gated workbench navigation"
```

Do not run the cache-stamp generator in this task; Task 11 owns its default write mode.

---

### Task 6: Fresh-Sign V3 Private Assets in the Shared Video Domain

**Files:**
- Modify: `server/content_domains/video.py:772-858`
- Create: `tests/test_ai_edit_v3_asset_library.py`

**Interfaces:**
- Consumes: `mode='ai_edit_v3'`, stable delivery object key, `presign_delivery_get`.
- Produces: fresh `video_url` and `download_url` on every owner-scoped asset read.

- [ ] **Step 1: Write RED signing and legacy-overwrite tests**

```python
def test_v3_asset_gets_fresh_play_and_download_urls(self):
    first = video.list_video_assets("owner")
    second = video.list_video_assets("owner")
    first_item = next(item for item in first if item["mode"] == "ai_edit_v3")
    second_item = next(item for item in second if item["mode"] == "ai_edit_v3")
    self.assertNotEqual(first_item["video_url"], second_item["video_url"])
    self.assertNotEqual(first_item["download_url"], second_item["download_url"])
    self.assertEqual(self.signer.call_args_list[0].kwargs["expires"], 300)

def test_legacy_cos_loop_does_not_overwrite_v3_signed_url(self):
    item = video.list_video_assets("owner")[0]
    self.assertTrue(item["video_url"].startswith("https://v3-signed.invalid/"))
    self.legacy_object_url.assert_not_called()
```

- [ ] **Step 2: Run RED test**

```powershell
python -m unittest tests.test_ai_edit_v3_asset_library -v
```

Expected: FAIL because `list_video_assets` only special-cases `ai_edit_v2`.

- [ ] **Step 3: Add the V3 signer branch and exclude both private modes from legacy**

```python
if item.get("mode") == "ai_edit_v3" and item.get("video_file"):
    item["video_url"] = ai_edit_v3_delivery.presign_delivery_get(
        item["video_file"], expires=300
    )
    item["download_url"] = ai_edit_v3_delivery.presign_delivery_get(
        item["video_file"],
        expires=300,
        download_name=item.get("filename") or "ai-edit-v3.mp4",
    )

if item.get("mode") in {"ai_edit_v2", "ai_edit_v3"}:
    continue
```

Do not persist either signed URL.

- [ ] **Step 4: Run V3 and V2 asset tests**

```powershell
python -m unittest tests.test_ai_edit_v3_asset_library tests.test_ai_edit_v2_asset_library tests.test_ai_edit_v2_delivery -v
```

Expected: PASS.

- [ ] **Step 5: Commit one shared production file**

```powershell
git add server/content_domains/video.py tests/test_ai_edit_v3_asset_library.py
git commit -m "feat(ai-edit-v3): add private asset playback signing"
```

---

### Task 7: Refresh V3 Playback and Task Focus in the Shared Asset Page

**Files:**
- Modify: `site/workbench/assets.html:569-605`
- Modify: `site/workbench/assets.html:867-940`
- Modify: `site/workbench/assets.html:1577-1598`
- Modify: `site/workbench/assets.html:1915-1931`
- Create: `tests/test_ai_edit_v3_asset_preview_ui.py`

**Interfaces:**
- Consumes: fresh `video_url`/`download_url` from owner asset list.
- Produces: click-time refresh, stale URL clearing, V3 task focus and 300-second renewal behavior.

- [ ] **Step 0: Verify the synchronized specification authorizes this shared touchpoint**

```powershell
rg -n 'site/workbench/assets\.html.*刷新 V3 私有播放/下载地址.*V3 任务通知定位' docs/superpowers/specs/2026-07-30-ai-edit-v3-design.md
```

Expected: exactly one match in §4.3. If there is no match, stop Task 7 and obtain a written specification revision before touching `site/workbench/assets.html`; the rest of Phase D does not imply authorization for this shared file.

- [ ] **Step 1: Write RED static/behavior tests**

```python
def test_assets_page_refreshes_both_private_ai_edit_modes(self):
    self.assertIn('mode==="ai_edit_v3"', self.page)
    self.assertIn("refreshVideoAssetUrl", self.page)

def test_assets_page_clears_stale_url_after_refresh_failure(self):
    self.assertIn('x.video_url=""', self.page)
    self.assertIn('video.removeAttribute("src")', self.page)

def test_v3_task_query_can_focus_published_asset(self):
    self.assertIn("data-job", self.page)
    self.assertIn("focusJobCard", self.page)
```

- [ ] **Step 2: Run RED test**

```powershell
python -m unittest tests.test_ai_edit_v3_asset_preview_ui -v
```

Expected: FAIL because refresh logic only recognizes `ai_edit_v2`.

- [ ] **Step 3: Generalize the private mode predicate without changing V2 behavior**

```javascript
function isPrivateAiEditMode(mode) {
  return mode === "ai_edit_v2" || mode === "ai_edit_v3";
}
```

Use it in `refreshVideoAssetUrl`, card playback and download. On refresh failure:

```javascript
x.video_url = "";
if (video) {
  video.pause();
  video.removeAttribute("src");
  video.load();
}
throw error;
```

Do not fall back to the expired URL.

- [ ] **Step 4: Refresh on active play, media error and download click**

The first card render may show cover only. Before assigning `video.src`, fetch the current asset list and find the same owner-visible `id` with `mode === "ai_edit_v3"`. A media `error` event performs one refresh attempt; prevent infinite error-refresh loops.

- [ ] **Step 5: Run V3/V2 asset page regressions**

```powershell
python -m unittest tests.test_ai_edit_v3_asset_preview_ui tests.test_ai_edit_v2_asset_preview_ui tests.test_notify_task_focus -v
```

Expected: PASS.

- [ ] **Step 6: Commit one shared production file**

```powershell
git add site/workbench/assets.html tests/test_ai_edit_v3_asset_preview_ui.py
git commit -m "feat(ai-edit-v3): refresh private asset playback"
```

---

### Task 8: Add Independent V3 Pricing APIs to the Shared Admin Server

**Files:**
- Modify: `server/admin_api.py:1269-1360`
- Modify: `server/admin_api.py:1556-1557`
- Modify: `server/admin_api.py:1696-1716`
- Create: `tests/test_ai_edit_v3_admin_pricing.py`

**Interfaces:**
- Consumes: Phase A `ai_edit_v3.billing` price config/draft/publish/list interfaces and `AI_EDIT_V3_DB_PATH`.
- Produces: `/api/admin/ai-edit-v3/pricing`, `/preview`, `/drafts`, `/publish`; independent audit actions.

- [ ] **Step 1: Write RED independence, immutability and confirmation tests**

```python
def test_v3_draft_publish_is_independent_from_v2_price_store(self):
    config = admin_api.ai_edit_v3_billing.default_price_config()
    draft = admin_api.save_ai_edit_v3_price_draft(
        "admin", {"version": "ai-edit-v3-test-2026-07-30", "config": config}
    )
    self.assertEqual(draft["status"], "draft")
    self.assertEqual(admin_api.load_ai_edit_v2_pricing(), self.v2_before)

def test_v3_publish_requires_exact_confirmation(self):
    with self.assertRaisesRegex(ValueError, "publish_confirmation_required"):
        admin_api.publish_ai_edit_v3_price(
            "admin",
            {
                "version": "ai-edit-v3-test-2026-07-30",
                "confirmation": "确认发布"
            },
        )

def test_v3_preview_covers_five_inputs_times_three_modes(self):
    preview = admin_api.preview_ai_edit_v3_pricing(
        {"config": admin_api.ai_edit_v3_billing.default_price_config()}
    )
    pairs = {(x["input_type"], x["creation_mode"]) for x in preview["scenarios"]}
    self.assertEqual(len(pairs), 15)
```

- [ ] **Step 2: Run RED tests**

```powershell
python -m unittest tests.test_ai_edit_v3_admin_pricing -v
```

Expected: FAIL because V3 admin pricing functions and routes do not exist.

- [ ] **Step 3: Implement V3-only helpers and audits**

Use `AI_EDIT_V3_DB_PATH` for `edit_v3_pricing_versions`; keep admin audit rows in `ADMIN_DB`.

```python
def save_ai_edit_v3_price_draft(actor, body):
    item = ai_edit_v3_billing.create_price_draft(
        actor,
        body.get("version"),
        body.get("config"),
        int(time.time()),
        pricing_db_path=ai_edit_v3_runtime.require_db_path(),
    )
    _ai_edit_price_audit(
        actor,
        "ai_edit_v3_price_draft_created",
        item["version"],
        {"config_sha256": item["config_sha256"]},
        int(time.time()),
    )
    return item
```

Publish confirmation must equal `f"发布 {version}"`. Never call `ai_edit_v2_billing`.

- [ ] **Step 4: Add authenticated GET/POST route branches**

```text
GET  /api/admin/ai-edit-v3/pricing
POST /api/admin/ai-edit-v3/pricing/preview
POST /api/admin/ai-edit-v3/pricing/drafts
POST /api/admin/ai-edit-v3/pricing/publish
```

Return `201` for a new draft, `200` for preview/publish/list, `400` for invalid config and `409` for duplicate immutable version or invalid publication transition.

- [ ] **Step 5: Run V3 and V2 admin regressions**

```powershell
python -m unittest tests.test_ai_edit_v3_admin_pricing tests.test_ai_edit_v2_admin_pricing -v
```

Expected: PASS and V2 active version remains unchanged.

- [ ] **Step 6: Commit one shared production file**

```powershell
git add server/admin_api.py tests/test_ai_edit_v3_admin_pricing.py
git commit -m "feat(ai-edit-v3): add independent pricing api"
```

---

### Task 9: Create the V3 Pricing Administration Page

**Files:**
- Create: `site/admin/ai-edit-v3-pricing.html`
- Modify: `tests/test_ai_edit_v3_admin_pricing.py`

**Interfaces:**
- Consumes: Task 8 V3 pricing endpoints.
- Produces: test-environment price preview, immutable draft creation and exact second confirmation.

- [ ] **Step 1: Write RED page contract tests**

```python
def test_admin_page_uses_only_v3_endpoints_and_exact_confirmation(self):
    page = self.v3_admin_page()
    self.assertIn("/api/admin/ai-edit-v3/pricing/preview", page)
    self.assertIn("/api/admin/ai-edit-v3/pricing/drafts", page)
    self.assertIn("/api/admin/ai-edit-v3/pricing/publish", page)
    self.assertIn("发布 VERSION", page)
    self.assertNotIn("/api/admin/ai-edit-v2/", page)
    self.assertNotIn("innerHTML=data.scenarios", page)
```

- [ ] **Step 2: Run RED test**

```powershell
python -m unittest tests.test_ai_edit_v3_admin_pricing -v
```

Expected: FAIL because the page does not exist.

- [ ] **Step 3: Create the page with safe DOM rendering**

The page contains:

```html
<h1>AI 智能剪辑 V3 价格表</h1>
<p>仅管理 V3 测试价格版本；已发布版本不可修改。</p>
<input id="version" autocomplete="off">
<textarea id="config" spellcheck="false"></textarea>
<button id="preview" type="button">预览 15 个输入组合</button>
<button id="save" type="button">保存为不可覆盖草稿</button>
<input id="publishVersion" autocomplete="off">
<label for="confirmation">完整输入“发布 VERSION”</label>
<input id="confirmation" autocomplete="off">
<button id="publish" type="button">确认发布</button>
<div id="scenarios"></div>
<pre id="versions"></pre>
```

Build scenario cards with `document.createElement` and `textContent`; never interpolate server values into `innerHTML`.

- [ ] **Step 4: Add API calls and exact confirmation UX**

```javascript
async function api(path, options = {}) {
  const response = await fetch(path, {
    credentials: "include",
    headers: {"Content-Type": "application/json"},
    ...options
  });
  const data = await response.json();
  if (!response.ok) throw new Error(data.detail || `HTTP ${response.status}`);
  return data;
}
```

Before publish, require `confirmation === "发布 " + publishVersion`; the server repeats this check.

- [ ] **Step 5: Run GREEN tests**

```powershell
python -m unittest tests.test_ai_edit_v3_admin_pricing -v
```

Expected: PASS.

- [ ] **Step 6: Commit the V3-owned admin page**

```powershell
git add site/admin/ai-edit-v3-pricing.html tests/test_ai_edit_v3_admin_pricing.py
git commit -m "feat(ai-edit-v3): add pricing administration page"
```

---

### Task 10: Add the V3 Pricing Link to the Shared Admin Index

**Files:**
- Modify: `site/admin/index.html:109-115`
- Modify: `tests/test_ai_edit_v3_admin_pricing.py`

**Interfaces:**
- Consumes: `site/admin/ai-edit-v3-pricing.html`.
- Produces: one V3 admin navigation link without altering the V2 link.

- [ ] **Step 1: Write RED link test**

```python
def test_admin_index_links_both_v2_and_v3_pricing(self):
    page = self.admin_index()
    self.assertIn('href="ai-edit-v2-pricing.html"', page)
    self.assertIn('href="ai-edit-v3-pricing.html"', page)
    self.assertIn("AI 剪辑 V3 价格表", page)
```

- [ ] **Step 2: Run RED test**

```powershell
python -m unittest tests.test_ai_edit_v3_admin_pricing -v
```

Expected: FAIL because the V3 link is absent.

- [ ] **Step 3: Add one adjacent link**

```html
<a class="btn" href="ai-edit-v3-pricing.html"
   style="display:inline-flex;align-items:center;text-decoration:none">
  AI 剪辑 V3 价格表
</a>
```

Do not rename or remove the V2 link.

- [ ] **Step 4: Run GREEN tests and commit one shared production file**

```powershell
python -m unittest tests.test_ai_edit_v3_admin_pricing tests.test_ai_edit_v2_admin_pricing -v
git add site/admin/index.html tests/test_ai_edit_v3_admin_pricing.py
git commit -m "feat(ai-edit-v3): link pricing administration"
```

---

### Task 11: Refresh Workbench Cache Stamps in a Mechanical Commit

**Files:**
- Modify mechanically: the 19 workbench HTML files listed in section 2.2.
- Test: `scripts/stamp_assets.py`

**Interfaces:**
- Consumes: final `cloud-shell.js` content hash.
- Produces: cache-safe shell rollout; no HTML DOM or business-logic changes.

- [ ] **Step 1: Verify stamp check is RED after Task 5**

```powershell
python scripts/stamp_assets.py --check
```

Expected: non-zero with stale `cloud-shell.js` stamps.

- [ ] **Step 2: Write current hashes**

```powershell
python scripts/stamp_assets.py
```

Expected: only `cloud-shell.js?v=` query values change in existing pages.

- [ ] **Step 3: Prove the diff is mechanical**

```powershell
git diff --word-diff=porcelain -- site/workbench
python scripts/stamp_assets.py --check
```

Reject any existing-page diff that changes text outside a `cloud-shell.js?v=` token. `ai-edit-v2.html` may receive that one generated query change and nothing else.

- [ ] **Step 4: Re-run shell, task and V2 UI tests**

```powershell
node --test tests/test_ai_edit_v3_ui.js tests/test_ai_edit_v2_ui.js tests/test_ai_edit_dual_entry.js tests/test_cloud_shell_sidebar.js
python -m unittest tests.test_video_global_tasks tests.test_notify_task_focus -v
```

Expected: PASS. The stale `tasks.js?v=task9` bootstrap case must still replace itself with tracker version `ai-edit-v3-site-v1`.

- [ ] **Step 5: Commit only generated stamp changes**

```powershell
$stampFiles = @(
  "site/workbench/ai-edit.html",
  "site/workbench/ai-edit-v2.html",
  "site/workbench/assets.html",
  "site/workbench/audio.html",
  "site/workbench/banana.html",
  "site/workbench/bots.html",
  "site/workbench/canvas.html",
  "site/workbench/collect.html",
  "site/workbench/cost.html",
  "site/workbench/dashboard.html",
  "site/workbench/inspiration.html",
  "site/workbench/invite.html",
  "site/workbench/leads.html",
  "site/workbench/recharge.html",
  "site/workbench/script.html",
  "site/workbench/settings.html",
  "site/workbench/tutorials.html",
  "site/workbench/video.html",
  "site/workbench/ai-edit-v3.html"
)
git add -- $stampFiles
git commit -m "chore(workbench): refresh shared shell cache stamps"
```

---

### Task 12: Write the Test-Site Runbook and Verify Phase D

**Files:**
- Create: `docs/operations/ai-edit-v3-runbook.md` (Phase C Task 10 explicitly leaves the general V3 operations runbook to Phase D)
- Test: all Phase D and V2 regression files

**Interfaces:**
- Consumes: service names, API routes, feature flag, test database and signed GET contract.
- Produces: exact operator sequence for a later separately authorized test deployment; no deployment action in this task.

- [ ] **Step 1: Create the runbook with explicit preflight and stop conditions**

The runbook must contain these sections:

```text
1. Authorization boundary
2. Required test-only environment variables
3. Pinned Python dependency installation and runtime verification
4. Phase A–D test commands
5. CI and active-task gate before deployment
6. File-scoped backup and deployment
7. Service health and capability verification
8. Browser smoke for all five inputs and all three creation modes
9. Quote, prehold, settlement, refund and two safety-pending checks
10. Private result and asset-library Range GET verification
11. Admin price draft/preview/publish verification
12. Logs, metrics and alert checks
13. Rollback by disabling V3 and stopping only V3 worker
14. Evidence capture and production prohibition
```

State verbatim that production deployment, production DB migration, production credentials, production price publication and `AI_EDIT_V3_ENABLED=1` in production require new explicit authorization.

The dependency section must run after merged-main CI succeeds and before restarting `huangque-content.service` or starting the V3 Worker:

```bash
sudo install -o root -g root -m 0644 deploy/requirements-ai-edit-v3.txt \
  /home/ubuntu/content-api/requirements-ai-edit-v3.txt
sudo /usr/bin/python3 -m pip install --disable-pip-version-check --no-input \
  --no-deps --requirement /home/ubuntu/content-api/requirements-ai-edit-v3.txt
sudo -u ubuntu /usr/bin/python3 -c 'from importlib.metadata import version; from jsonschema import Draft202012Validator; assert version("jsonschema") == "4.26.0"; Draft202012Validator.check_schema({"$schema":"https://json-schema.org/draft/2020-12/schema","type":"object"})'
sudo -u ubuntu /usr/bin/python3 -m pip check
```

Every command must exit `0`; a missing wheel, permission failure, version mismatch, broken dependency graph or failed Draft 2020-12 check stops deployment before any service restart. Do not add an import fallback or install an unpinned package.

- [ ] **Step 2: Add exact read-only smoke commands**

Use shell variables without embedding secrets:

```bash
curl -fsS -b "$COOKIE_JAR" \
  "$BASE_URL/api/v3/edit/capabilities"
curl -fsS -b "$COOKIE_JAR" \
  "$BASE_URL/api/v3/edit/jobs?limit=20"
curl -fsS -b "$COOKIE_JAR" \
  "$BASE_URL/api/v3/edit/jobs/$JOB_ID"
curl -fsS -b "$COOKIE_JAR" \
  "$BASE_URL/api/v3/edit/jobs/$JOB_ID/result"
range_headers="$(mktemp)"
range_body="$(mktemp)"
trap 'rm -f "$range_headers" "$range_body"' EXIT
range_metrics="$(curl -sS --fail-with-body \
  -D "$range_headers" -o "$range_body" \
  -H 'Range: bytes=0-0' \
  -w '%{http_code} %{size_download}' "$PLAY_URL")"
IFS=' ' read -r range_code range_size <<< "$range_metrics"
test "$range_code" = '206'
test "$range_size" = '1'
test "$(wc -c < "$range_body" | tr -d ' ')" = '1'
tr -d '\r' < "$range_headers" \
  | grep -Eiq '^Content-Range: bytes 0-0/[1-9][0-9]*$'
```

The expected media response is exactly `206 Partial Content`, with a one-byte body and a valid `Content-Range` for byte zero. A `200` response fails this gate because it did not prove Range behavior. The runbook must say “Do not use HEAD for a GET-only signature.”

- [ ] **Step 3: Add later-authorized deployment and rollback checks**

The runbook sequence must require:

```bash
systemctl is-active huangque-content.service
systemctl is-active huangque-ai-edit-v3.service
journalctl -u huangque-ai-edit-v3.service --since '-15 minutes' --no-pager
```

Before a later test deployment: wait for main CI, confirm no active V3 jobs, back up only replaced files, deploy only merged files, then health/API/browser/task/asset checks. Rollback disables V3 submissions and stops `huangque-ai-edit-v3.service`; it must not delete `ai_edit_v3.db`, COS objects, billing outbox, publish intents or safety-pending evidence.

- [ ] **Step 4: Run Phase D Python tests**

```powershell
python -m unittest tests.test_ai_edit_v3_api tests.test_ai_edit_v3_asset_library tests.test_ai_edit_v3_asset_preview_ui tests.test_ai_edit_v3_admin_pricing -v
```

Expected: PASS.

- [ ] **Step 5: Run Phase D JavaScript tests**

```powershell
node --test tests/test_ai_edit_v3_ui.js tests/test_ai_edit_dual_entry.js tests/test_cloud_shell_sidebar.js
```

Expected: PASS.

- [ ] **Step 6: Run all V3 and frozen V2 regressions**

```powershell
python -m unittest discover -s tests -p "test_ai_edit_v3_*.py" -v
python -m unittest discover -s tests -p "test_ai_edit_v2_*.py" -v
node --test tests/test_ai_edit_v3_ui.js tests/test_ai_edit_v2_ui.js tests/test_ai_edit_dual_entry.js tests/test_cloud_shell_sidebar.js
python -m unittest tests.test_video_global_tasks tests.test_notify_task_focus -v
```

Expected: all pass; V2 count does not decrease from the Phase C baseline. The approved design baseline is 413 Python V2 tests and 47 V2 frontend tests.

- [ ] **Step 7: Run repository validation**

```powershell
python -m unittest discover -s tests -v
python scripts/ci_validate.py
python scripts/stamp_assets.py --check
git diff --check
```

Expected: all exit `0`.

- [ ] **Step 8: Run a secret and forbidden-field scan**

```powershell
$forbidden = rg -n "DASHSCOPE_API_KEY=.+|ELEVENLABS_API_KEY=.+|Authorization: Bearer|cos_key|object_key|render_manifest" site/workbench/ai-edit-v3.html site/admin/ai-edit-v3-pricing.html docs/operations/ai-edit-v3-runbook.md
if ($LASTEXITCODE -eq 0) { throw "forbidden browser or documentation field found: $forbidden" }
if ($LASTEXITCODE -gt 1) { throw "secret scan failed with exit code $LASTEXITCODE" }
```

Expected: no secret values and no browser-visible forbidden fields. Documentation may name environment variables only.

- [ ] **Step 9: Verify declared file scope**

```powershell
$phaseDBase = git merge-base origin/main HEAD
git diff --name-only "$phaseDBase...HEAD"
git log --oneline "$phaseDBase..HEAD"
```

Expected: only files declared in this plan; every Task 1–12 has one intentionally scoped commit, and every shared production file is isolated.

- [ ] **Step 10: Commit the runbook**

```powershell
git add docs/operations/ai-edit-v3-runbook.md
git commit -m "docs(ai-edit-v3): add test-site operations runbook"
```

Do not push or deploy. Use `superpowers:requesting-code-review` for an independent Phase D review.

---

## 5. Phase D Requirement Traceability

| Approved requirement | Implementing tasks | Proof |
| --- | --- | --- |
| Five inputs | 1, 2, 3 | strict request union API/UI tests |
| Three modes | 1, 2, 3 | omitted unused fields and template-ratio tests |
| Platform cover lazy load | 1, 2 | list DTO has no video URL; no gallery `<video>` |
| Click-only preview API | 1, 2 | owner-bound fresh 300-second authorization and race tests |
| Up to 10 current images | 1, 2 | MIME/count/material promotion tests |
| Quote/fingerprint/15-minute validity | 1, 3 | quote parity and stale revision tests |
| Create/retry idempotency | 1, 3 | persisted-before-POST and replay/conflict tests |
| Polling/status/history | 1, 3 | server-paced non-overlap and current/view separation |
| Reconciliation pending semantics | 1, 3, 4, 5 | tracked but composer-unlocked; no false refund/publish copy |
| Navigation and task center | 4, 5, 11 | independent gates, stale tracker replacement and stamp tests |
| V3 private playback/download | 1, 3, 6, 7, 12 | fresh signing, no legacy overwrite, Range GET runbook |
| Settlement/refund display | 1, 3 | authoritative billing DTO and pending copy tests |
| Independent admin pricing | 8, 9, 10 | separate store, 15 previews, immutable draft/publish audit |
| V2 unchanged in behavior | every task | targeted V2 regressions plus final full V2 suite |
| Test-site operations | 12 | runbook, CI/active-job gate, health, rollback, no production authority |

## 6. Phase D Definition of Done

- [ ] All five inputs and all three modes generate the exact strict request union; quote and create fingerprints match.
- [ ] Platform cards load only covers and never create a video element; active preview is owner-bound, 300 seconds, no-store and race-safe.
- [ ] Supplemental material upload stops at ten images and never exposes or consumes a historical asset.
- [ ] The only primary button performs quote then explicit confirmation; double clicks and network uncertainty do not create a second job or debit.
- [ ] Polling is non-overlapping and server-paced; history viewing cannot replace the current creation context.
- [ ] Safety-pending jobs remain visible/tracked, unlock new creation and never display false refund/publication claims.
- [ ] Completed result and asset-library playback obtain fresh 300-second URLs; no signed URL is persisted and Range GET succeeds.
- [ ] V2/V3 nav gates, tasks, notices, asset modes and price stores remain independent.
- [ ] Every shared production file is in its own commit; generated cache stamp changes are isolated.
- [ ] All Phase D, V3, V2 and repository validation commands pass with no test-count regression.
- [ ] V3 remains disabled by default; no push, deployment, real Provider call, real point operation or production action occurred.

# AI智能剪辑（Shotstack API-only）运行手册

本文用于黄雀传媒测试环境的 AI 智能剪辑 POC、故障处理和交接。代码正本始终是 Git 分支；服务器运行目录不是正本。本文不包含任何密码、API Key、数据库密码、COS SecretKey 或签名 URL。

> 生产部署不在本次授权范围内。完成测试站 9 次黄金 POC、提交报告并得到单独明确批准后，才能安排生产上线。

## 1. 前置条件

- 测试站 `/api/gen/health` 返回 HTTP 200，且当前运行提交与准备部署的已推送提交一致。
- `huangque-content` 使用 Python 3，具备现有 COS SDK、FFmpeg/ffprobe 和数据库目录写权限。
- COS 已配置私有对象读写及数据万象媒体信息能力；桶 CORS 允许测试站页面对预签名地址执行 PUT。
- 阿里云 DashScope 账号已开通通义千问和 Fun-ASR，并为服务账号授予最小所需权限。
- Shotstack 账号先使用 stage 环境，确认额度、区域、回调可达和 MP4 输出能力。
- 真实环境变量只写入服务器本地 EnvironmentFile，文件属主为 root、权限 `0600`，不得进入 Git、日志、工单或聊天记录。

配置变量如下，值以 `deploy/huangque-secrets.env.example` 为准：

```text
AI_EDIT_ENABLED
AI_EDIT_DB
AI_EDIT_POINTS
AI_EDIT_JOB_WORKERS
AI_EDIT_IMAGE_PROVIDER
AI_EDIT_QWEN_MODEL
DASHSCOPE_API_KEY
DASHSCOPE_ASR_MODEL
SHOTSTACK_API_BASE
SHOTSTACK_API_KEY
SHOTSTACK_CALLBACK_BASE
COS_SECRET_ID
COS_SECRET_KEY
COS_REGION
COS_BUCKET
```

## 2. 首次启用与关闭

1. 保持 `AI_EDIT_ENABLED=0` 部署已推送的测试分支提交，只重启 `huangque-content`。
2. 检查健康、漂移和日志后，在服务器本地 0600 EnvironmentFile 中配置供应商变量。
3. 先调用登录后的 `GET /api/v1/edit/styles`；关闭状态应返回 `503` 和 `ai_edit_disabled`，且不产生扣点、任务、上传签名或供应商请求。
4. 将 `AI_EDIT_ENABLED=1`，只重启 `huangque-content`，再次检查风格接口和上传初始化。

紧急关闭只需把 `AI_EDIT_ENABLED` 改回 `0` 并重启 `huangque-content`。关闭后不接收新上传和新任务；数据库中的既有任务行、素材行和计费占用记录必须保留，不得手工删除。

## 3. 健康与冒烟检查

```bash
curl -fsS https://fang.huangquechuanmei.com/api/gen/health
curl -i https://fang.huangquechuanmei.com/api/v1/edit/styles
sudo systemctl status huangque-content --no-pager
sudo journalctl -u huangque-content -n 200 --no-pager
```

上线测试站前必须记录：

```text
GET https://fang.huangquechuanmei.com/api/gen/health -> HTTP 200
Git commit deployed -> exact pushed commit SHA
Orphan ai_edit*.py -> archived/diffed, not copied over Git files
Content service drift check -> no unexplained AI-edit file drift
```

任何一项不满足都停止部署。不要用 `rsync --delete`，不要整站覆盖，也不要重载 Nginx，除非确认测试站现有 `/api/v1/` 代理缺失。

## 4. Shotstack stage 与 production 切换

POC 固定使用：

```dotenv
SHOTSTACK_API_BASE=https://api.shotstack.io/edit/stage
```

只有在 9 次 POC 达标、成本已核对并获得生产部署单独批准后，才可在生产服务器本地 EnvironmentFile 中切换为：

```dotenv
SHOTSTACK_API_BASE=https://api.shotstack.io/edit/v1
```

切换不改代码、不提交密钥，只重启目标环境的 `huangque-content`。stage 与 production 的额度、任务 ID 和成本账单分别核对，不得混用。

## 5. 日志、阶段与供应商任务定位

核心任务在 `content_jobs.db`，详细阶段在 `AI_EDIT_DB` 指向的 `ai_edit.db`。排障只查询必要字段，不复制用户素材、完整文案或签名 URL。

阶段顺序：

```text
created -> resolving_source -> transcribing -> planning -> preparing_assets
-> rendering -> transferring -> verifying -> done
```

失败时详细表记录 `error_code` 和截断后的 `error_detail`。供应商任务用 `provider_job_id` 与 Shotstack 控制台请求 ID 对照；应用日志中不得打印请求 Authorization、API Key、COS 签名参数或完整供应商响应。

示例只读查询（路径按实际 `AI_EDIT_DB`）：

```bash
sqlite3 /home/ubuntu/content-api/ai_edit.db \
  "select job_id,stage,provider_job_id,provider_status,error_code,updated_at from edit_jobs order by job_id desc limit 20;"
```

## 6. Webhook、幂等与重启恢复

- Shotstack Webhook 只作为唤醒/状态提示。服务端收到回调后必须用 `provider_job_id` 回查 Shotstack，不能信任回调体宣称的成功状态。
- 重复回调只更新同一条详细任务，不创建核心任务、不再次扣点，也不把任务直接改成成功；终态只能由后台 Worker 写入。
- 提交必须带 `Idempotency-Key`。同账号、同端点、同请求重放返回原 `job_id`，不重复扣点。
- 服务重启时，已有 `provider_job_id` 的运行中任务改回 pending 并继续查询原渲染任务，不重复提交 Shotstack、不退款；尚未取得供应商任务 ID 的孤儿任务走原失败退款路径。

## 7. 点数与失败退款核验

每次 POC 记录提交前点数、服务端返回 `cost`、核心任务 `refunded` 状态和完成后点数：

- 成功：`billing_holds.status=confirmed`，点数只扣一次，视频资产库有且只有一条对应 MP4。
- 强制供应商失败：核心任务为 error，`billing_holds.status=released`，`jobs.refunded=1`，点数只退一次。
- 重复提交、重复 Webhook、Worker 与 reaper 竞争都不得产生第二次扣点或退款。

不得通过直接改数据库来“修正”测试结果。发现不一致时先关闭功能，保留记录并从日志、任务行和点数审计表定位。

## 8. 测试站 9 次黄金 POC

每种场景执行 3 次：

1. 30 秒知识口播，风格“知识快讲”。
2. 20 秒产品视频，风格“产品故事”，选择已生成的产品图片素材。
3. 音频主导故事，风格“故事画面”，允许自动生成缺失静帧。

逐次记录：ASR、规划、素材生成、Shotstack 渲染、COS 转存、成片校验耗时；供应商请求 ID、第三方成本、总耗时及成功/失败。通过标准：至少 8/9 无人工干预成功，三种风格在布局、素材使用和节奏上明显不同，成功任务均入视频资产库，时长误差不超过 0.5 秒，9:16 字幕保持安全区并跟随语音。

## 9. 回滚

1. 在目标服务器本地 EnvironmentFile 设置 `AI_EDIT_ENABLED=0`。
2. 确认要恢复的“上一已推送提交 SHA”，只从 Git 提交恢复本次改过的文件；不要从运行目录反向覆盖仓库。
3. 只重启 `huangque-content`，不重启认证、作图、获客服务；Nginx 代理未变时不 reload。
4. 验证 `/api/gen/health`、现有视频生成和资产列表。
5. 保留 `content_jobs.db`、`ai_edit.db`、既有任务和素材行，不删除、不回填、不手工改终态。仍在供应商侧运行的任务记录用于后续对账。

测试环境文件选择性安装必须来自已经 push 的精确提交。生产回滚或生产部署均需要另一次明确审批。

# AI Edit V3.1 真实验收适配器实施计划

## 目标

让 `scripts/ai_edit_v3_acceptance.py run --environment test` 在既有严格 preflight 之后，能够用真实测试账号、真实 V3 API 和授权语料执行单条、5 条并发与 10 条压力验收；证据目录不保存会话、签名 URL、原始私有文案或本地绝对路径。

本计划只实现本地代码与无网络测试，不部署、不上传测试站、不调用真实 Provider、不扣点。

## 关键边界

- 一个 `owner_alias` 对应一个仅从环境变量读取的测试会话；绑定文件只保存环境变量名，不保存值。
- 每个案例的 source 按输入类型解析：上传输入绑定本地文件；平台口播绑定 `source_asset_id`；已有音频绑定 `source_asset_id`；文本配音绑定 UTF-8 文本文件和 `voice_id`。
- 所有图片素材都先走 `/uploads`，再走 `/materials`，得到 `material_asset_ids`。
- 平台资产、已有音频和音色必须先从该账号的只读目录接口中确认存在；目录公开字段不包含 SHA 时，以冻结 ID、时长、比例及授权链校验，不伪造哈希证明。
- 每个案例使用自己的账号客户端，禁止跨账号复用上传 ID、素材 ID、资产 ID、音色 ID或任务 ID。
- 真实轮询使用单调时钟、退避和总截止时间；不得执行 120 次无等待紧循环。
- 同一 `run_id/profile/case_id` 使用固定 Idempotency-Key；已有 checkpoint 只能查询旧任务，不能重新创建。

## Task 1：冻结 bindings 2.0 与多账号会话加载

修改：

- `scripts/ai_edit_v3_acceptance.py`
- `tests/test_ai_edit_v3_acceptance_runner.py`

先写失败测试，覆盖：未知字段、重复 owner、缺会话环境变量、case/owner/alias/hash 不一致、文本文件 hash 不一致、跨账号素材、绝对路径泄漏到持久化证据。然后实现严格的 `bindings 2.0` 解析器与 `OwnerHttpClient` 工厂。旧 `1.0` 上传绑定格式明确拒绝，避免两套语义并存。

## Task 2：实现所有输入与图片素材的权限解析

先写表驱动失败测试，再实现：

- `uploaded_video` / `uploaded_audio`：创建上传、签名 PUT、完成上传。
- `platform_talking_head`：读取 `/platform-assets` 并绑定授权资产 ID。
- `existing_audio`：读取 `/audio-assets` 并绑定授权资产 ID。
- `script_to_audio_video`：读取 UTF-8 文本文件、核对冻结 SHA，读取 `/voices` 并绑定音色 ID。
- 每张材料：图片上传后调用 `/materials`，并核对返回 SHA 与冻结 SHA。

任何目录不匹配、上传回执不匹配或账号不匹配都必须在 quote/create 前退出 `2`。

## Task 3：实现逐案例 quote/create/poll/result/range 客户端

新增每案例不可变 request：

- 视频输入 ratio 转为服务契约要求的 `auto`。
- 音频与文本输入保留矩阵的 `16:9` 或 `9:16`。
- `ai_auto` 不带额外创作字段；`style_prompt` 只带 `style_prompt`；`template_reference` 只带 `template_id`。
- quote 与 create 使用完全同一 request；create 只额外携带 `quote_id` 和 Idempotency-Key header。

轮询：2 秒起步、最大 15 秒、总截止时间默认 60 分钟，可由测试注入时钟和 sleep。结果与播放 Range 请求只允许 HTTPS，签名播放 URL 仅在内存中使用。

## Task 4：实现 `execute_preflighted_cases`

复用 `run_cases`、checkpoint、evidence 与 verify 路径，生成不可覆盖的 profile 目录及聚合报告。preflight 上传/解析只执行一次；case factory 只取已冻结的账号客户端和已解析 authority。

返回码保持：`0` 全部完成，`3` 有合法终态失败，`4` 证据/协议损坏。任何未终态或未知响应保持可恢复 checkpoint，不伪造成失败证据。

## Task 5：验证与提交

执行：

```powershell
python -m unittest tests.test_ai_edit_v3_acceptance_runner -v
python -m unittest discover -s tests -p 'test_ai_edit_v3*.py' -q
git diff --check
```

完成独立只读审查后再本地提交。真实测试运行仍需单独获得测试站部署、会话、Provider 调用与点数变更授权。

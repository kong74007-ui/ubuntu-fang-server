# palette-evidence-v2 — 同源 before/after 验收证据

本目录是 PR #192 的机器可核查证据。
**所有图、MP4、关键帧都由同一份输入、同一批素材、同一套渲染环境产出。**

## 1. 验收输入（唯一一份）

取自生产任务库的真实 App payload（App 于 09-11 20:04 发给 `ref-09-urumqi-soft-brush`），
对 4 个模板**使用完全相同的一份**：

| 字段 | 值 |
|---|---|
| top_text | `选赛道别只看谁现在最火 要看三年后 客户还会不会继续消费` |
| bottom_text | `大健康是长期需求赛道 想入局的 评论区回复 勾兑` |
| semantic_layout | `{version:1, model:"gpt-4.1-mini", source_sha256:"89f75f1c…", top1_end:11, top_break_after:[11,17], bottom_break_after:[10,15,21]}` |

原始文件：`accept-raw-input.json`

## 2. 文本链路（真实生产代码）

不是"辅助分行函数"，而是与服务端 `/v1/jobs` 完全相同的路径：

```
MatrixTemplateService(...)                     # 生产 api.py + 生产 env
  .validate_payload(raw, require_available_font=False,
                    require_reference_semantic_layout=True)
    -> _reference_semantic_text_layout(...)     # 语义排版
  ._freeze_font_provenance(job_id, payload)
    -> _reference_template.source_text / display_text
```

产物：

| 文件 | 内容 |
|---|---|
| `manifest-before.json` | 生产包（palette v1）的链路原样输出 |
| `manifest-before-after.json` | 两套包在同一份 payload 上的 `display_text` 对照 + `before==after` 校验 |

两份都逐字保存 `input` / `source_text` / `display_text` / `text_layers` / `variant` / `duration`。

**渲染只使用 `display_text`**（生产 `api.py:6509` 也是同样处理）。

## 3. 复现命令

```bash
# ① 生产链路取变量（在 fang 上，已 source 生产 env）
python3 palette-evidence-v2/tools/pipeline_manifest.py /tmp/accept_raw.json /tmp/pipeline_manifest.json

# ② 同一份变量渲染 before(v1) / after(v2) 两套包，并按同一比例抽帧
python3 palette-evidence-v2/tools/render_evidence.py /tmp/pipeline_manifest.json /tmp/ev /tmp/ev/out
```

`render_evidence.py` 用**同一份 `*_variables.json`** 分别渲染 `pack_before` 与 `pack_after`，
在同一比例（15% / 50% / 85%）抽帧，保证 before/after 的文案、素材、分辨率、时间点完全一致。

## 4. 渲染环境

| 项 | 值 |
|---|---|
| HyperFrames | `0.8.16`（`/usr/local/bin/hyperframes`，与生产同版本） |
| 浏览器 | `/usr/bin/google-chrome-stable`（`HYPERFRAMES_BROWSER_PATH`） |
| 画布 | 1080×1920，`-q draft`，`-w 1` |
| 素材 | 模板包自带 `assets/library/default-{a,b,c}.mp4`（**before/after 是同一批文件**） |
| 时长 | 按 manifest 中每个模板的 `duration`（13 / 9 / 10 / 10 秒） |

## 5. 目录内容

```
accept-raw-input.json          原始验收输入（= 真实 App payload）
manifest-before.json           生产包（palette v1）的链路原样输出
manifest-before-after.json     两套包 display_text 对照 + before==after 校验
render-report.json             8 次渲染的时长 / 大小 / 关键帧清单
v0*.variables.json             每个模板实际喂给 HyperFrames 的变量（= display_text）
OVERVIEW-1-before-after.png    4 模板 before | after（中段帧）
OVERVIEW-2-keyframes-after.png 4 模板 × 3 关键帧（after）
frames/<variant>-<phase>-k{0,1,2}.png   24 张关键帧（15% / 50% / 85%）
mp4/<variant>-<phase>.mp4      8 条成片
tools/pipeline_manifest.py     生产链路取变量脚本
tools/render_evidence.py       同源双包渲染脚本
FIX-v09.md                     v09 阻断项的发现与修复记录（必读）
hashes.json                    code_sha / base_sha / 包哈希 / 输入哈希 / 脚本哈希 / 输出哈希
```

## 6. 本轮结论

四个模板在 before(v1) 与 after(v2) 两套包上**全部通过**生产链路，
且 `display_text` **逐字相同**（见 `manifest-before-after.json`）
→ before/after 是**严格同源对照**，视觉差异只来自配色覆盖与 v09 字号。

成片复核：

| 模板 | before | after |
|---|---|---|
| v06 | 白色标题 + **绿色**按钮 | 白色标题 + **黄色**按钮（黄强调恢复） |
| v09 | 辅助层 / 底部层偏小 | **辅助层与底部层明显放大**，`top1` 保持单行不二次换行 |
| v14 | 全白 / 米白 | **绿色副标题 + 绿色底部**（绿色层级恢复） |
| v17 | 全白 | **黄色主字 + 白字红描边**（黄红身份恢复） |

v09 的阻断项（`top1` 放大到 100px 会导致服务端 400）已在 `FIX-v09.md` 记录，
处理方式是按方案自带的兜底顺序把 `top1` 保持在当前 88px。

## 7. 校验哈希

见 `hashes.json`：

* `code_sha` —— 产出本批 MP4 / 关键帧的代码提交（`hashes.json` 自身由紧随其后的提交收录）
* `base_sha` —— 基线提交
* `packs` —— 两套包（overlay 版本、v09 字号、reference index 的 SHA256）
* `input` —— 输入 payload 及其 `source_sha256`
* `scripts` —— `tools/` 下脚本的 SHA256
* `outputs` —— 逐个 MP4 / 关键帧 / 总览 / report 的 SHA256

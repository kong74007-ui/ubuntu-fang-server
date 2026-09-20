# 素材依赖准入故障合同

节点只缓存成功的素材库就绪探测，有效期从探测完成开始计算。失败不缓存为后续请求的否定结论；每个新请求可重新探测，仍由原锁串行化探测。

探测失败不会绕过就绪门槛，也不会创建任务：

- 临时网络/服务探测失败：HTTP503，error=material_library_unavailable，reason_code=probe_failed/probe_timeout，retryable=true。
- 明确鉴权、请求拒绝、协议版本不匹配：HTTP409，同一error，reason_code=auth_failed/probe_rejected/contract_invalid，retryable=false。
- 其他准入拒绝：保留HTTP409/submission_failed，增加queue_capacity/disk_capacity/admission_failed原因码，不据此自动重试。

记录安全request_id、原因码和可重试标志，不写URL、Token、素材文案。主站node_poller需先具备该合同及旧版精确错误兼容，再切换节点API。短暂故障允许相同幂等键与冻结payload在30秒、最多5次的限制内重试；不重建Relay任务、不重新扣点。

只修已复现的负缓存与错误处理缺陷，不声称已经从缺失的历史响应证明27个旧HTTP409的全部成因。旧失败任务不自动重放，真实百条稳定性仍需后续单独验收。

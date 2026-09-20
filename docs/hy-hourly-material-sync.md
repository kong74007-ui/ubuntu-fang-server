# HY 自主定时素材同步

`scripts/hy_material_sync.py` 由 HY Windows 计划任务每小时执行，不依赖 Codex。
`scripts/hy_material_export.py` 是主站上的只读 forced-SSH 导出器，只允许
`manifest`、`blob <SHA256>`、`idle` 三个命令。专用 SSH key 必须带 `restrict,command=...`，
禁止交互 shell、转发和任意文件读取。运行参数、主机、私钥、数据库路径均放私有配置，不提交。

## 发布与安全

- 公共库使用现有受限 rsync 源与固定 known_hosts；只下载新增 SHA 的内容。
- 私有 repair_map 仅保存经验证修复的20条素材技术字段映射；原源SHA未变时保留修复版。
- 候选索引须路径、SHA、完整性和色彩验证；新增不兼容素材仍由原色彩规则过滤。
- 发布前主站及Relay必须零在途。否则本轮延后，不停止/取消任务；下轮重新构建候选。
- 原子替换索引与prepared cache，失败恢复原索引/缓存，不回滚usage或receipts。
- 用户素材根据非删除任务冻结SHA确认账号归属，并保留源有效期。仅私有副本，不改变生成取材路由。
- 用户目录ACL仅SYSTEM/Administrators，按owner_hash分区，不进入共享索引。
- 无可确认归属/已过期素材不进入当前私有清单。原文件和历史备份不自动删除。
- 单实例文件锁；计划任务也设置IgnoreNew；磁盘不足8GiB停止同步。
- 成功或失败记录 `scheduled-sync/last-run.json`；程序错误退出码1，不将失败伪装成功。
- 仅删除本轮位于专用 staging 下的暂存文件；长期素材、源数据及备份不删除。

## 计划任务

推荐 Windows 原生 Python，S4U 以部署管理员身份执行，最高权限，开机触发及每小时触发。
通过显式 `wsl.exe -d <已存在发行版> -u <连接用户> --` 调用SSH/rsync；
Windows Python负责D盘校验，避免WSL跨文件系统全库哈希。
`MultipleInstances=IgnoreNew`、`StartWhenAvailable=true`、执行上限40分钟。

私有配置需要：base/shared_root/private_root/prepared_cache/repair_map/library_code、
wsl_prefix/export_ssh/public_ssh/public_remote，以及可选ffprobe PATH。修复映射和公私有权限变更
需人工审查，不从远端索引自动授权。计划任务测试必须在真正的S4U上下文运行并查看退出码。

回滚：停止/禁用同步任务，恢复某次备份的共享index/prepared cache；不能回退usage或用户任务。
如果无新素材，一个小时一次仅拉小索引和私有清单，不复制整库、不重启服务。

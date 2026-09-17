# 2026-09-16：10 台 GPU 并行验证（已启动）

用户明确授权“开 10 台机器，并行验证”。复用本账号之前用于 UE 的已关机 EC2，不新增 4 TiB 镜像副本；保留这些实例上的旧录制、冻结文件及源码备份。其他用户/用途的机器未动。

- 10 台均为 `g6.4xlarge`：单张 NVIDIA L4、16 vCPU、64 GiB 内存。
- eu-north-1a 启动第 7 台时返回 `InsufficientInstanceCapacity`，回滚未启动实例的临时标签与 user-data，改用 eu-north-1b 的 4 台闲置同配置机器。最后为 6 + 4 共 10 台。
- 83 张地图分为 9/9/9/8/8/8/8/8/8/8；本机已经验证 Temple Plaza、Tokyo、化工厂三张，不再重复分配。本机队列已在化工厂结束后暂停。
- 云端编译当前 `SimWorldCapture` 模块，部署相同 Python 路线算法、可选 TAA 模板、起点私有副本和最新启动/RPC 超时保护；每台只有一个 editor，每图一个连接。
- 首台发现无人登录时默认 `/run/user/1000` 生命周期不稳定，enroot exec 报 Permission denied。已显式导出固定 `ENROOT_RUNTIME_PATH`；该首台的基础设施失败已经存档后重新排队，不计为地图拒绝。00 号以修正环境手动恢复 supervisor，其余 9 台使用修正后的完整启动包。
- 每台结束上传后自动 shutdown（明确设置 EC2 的实例关机行为为 stop，保留磁盘）；另设 12 小时预算关机定时器。不是终止实例，不删除 EBS 或旧数据。
- 运行中的网页实时显示机器、当前地图、结果；机器开机、环境编译、路线预检通过是不同状态。路线通过仍不等于画质或主体区域覆盖完成。

| Worker | Instance | AZ | Maps |
| --- | --- | --- | --- |
| 00 | i-07eb4f8935b7ab42e | eu-north-1a | 9 |
| 01 | i-0b78a067fc047c21a | eu-north-1a | 9 |
| 02 | i-0e42ab9a1a06d2f62 | eu-north-1a | 9 |
| 03 | i-0703d9715e30289b7 | eu-north-1a | 8 |
| 04 | i-00a045f47411c2a5f | eu-north-1a | 8 |
| 05 | i-09ea3ed41bcf77ad9 | eu-north-1a | 8 |
| 06 | i-01e7b8168fabadc3a | eu-north-1b | 8 |
| 07 | i-0fcae8618eec60e98 | eu-north-1b | 8 |
| 08 | i-067c09550c4bf1289 | eu-north-1b | 8 |
| 09 | i-0db3336129cfa07c7 | eu-north-1b | 8 |

## 数据与控制位置

- 本机控制目录：`/home/ubuntu/ue_route_fleet_20260916`。
- 本机与每个 worker 的运行目录：`/home/ubuntu/ue_route_validation_20260916`。
- 新结果：`s3://pan-simworld/ue-route-validation/20260916/workers/<worker>/`；部署包：同前缀 `ops/`。没有覆盖旧 `ue-revist-long-video` 结果。
- 用户提供的 `s3://pan-simworld/SimWorld/new_map/` 已做只读目录检查，包含 Projects、AdditionalSamples、Validation。本轮仍验证既定 86 张地图，使用机器已有资产，不把新项目目录自动合并进当前 UE 项目。
- 各机器的旧标签、user-data、关机行为备份：控制目录 `before_*.json`，容量失败的旧 06 备份另存 `before_06_capacity_failed.json`。
- 控制端 `collect.py` 每 15 秒从 S3 原子下载状态、日志及完整冻结路线/同次导航网；`publish.py` 每 5 秒生成网页状态与路线图。
- 页面：`http://51.20.82.218:8500/longvideo/route-validation/`，86 张旧审查和 Dubai / 历史视频保留。

## 恢复注意事项

不要直接运行老 LongVideoShard boot。为复用机器替换 user-data 前先保留原配置，并清除老任务的启动分配标签，避免两套 runner 同时启动 UE。
00 号最初的 user-data 指向未设置固定 enroot runtime 的旧部署包；再次停机/开机前应先写入修正后的包引用。其当前运行环境已经修正，结果正常回传。
本目录是部署快照；实时进度和失败原因以控制目录、S3 状态及网页为准。

最终部署检查：10 台均已成功编译模块、至少进入过一张实际 UE 地图、各自仅有一个 editor；worker、超时 guard 和 TAA 模板哈希逐台一致。云端结果已从 S3 回传，网页已显示 Hwaseong、Bunker、Cave 的完成报告和新路线图。自动检查见 `fleet_validation.json`，地图仍在后台继续验证。

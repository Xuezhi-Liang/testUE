# testUE — UE5 第一人称覆盖式采集管线

为训练实时交互世界模型而做的地面真值采集：在 Unreal Engine 里把路线**先冻结**，再把它当回放录下来。
每一帧都带着「被要求做的动作」和「实际发生的位移」；两者不一致的地方就是这套数据存在的理由。

本仓库是**代码快照**，不含录制出来的数据，也不含任何商业授权素材。

## 目录

| 路径 | 内容 |
|---|---|
| `pipeline/` | 管线本体。UE 5.8 `gym_citynav`，经 UnrealCV 驱动。路线规划、冻结、引擎内采集、打包与验收。 |
| `pipeline/cpp/` | UnrealCV 和编辑器 Python 做不到的部分：浮点安全的深度回读、导航网合成、引擎内捕获 Actor。`install.py` 把它装进 `gym_citynavRuntime` 并改 `Build.cs`（幂等，在容器内运行）。 |
| `pipeline/tasks/` | 任务模板。`longvideo_template.json` 是任务生成器实际读取的那一份。 |
| `pipeline/results/` | 历次实验的**元数据**（JSON / Markdown），不含媒体，是文档里各项结论的证据。 |
| `site_builders/` | 8500 端口那套复盘网页的生成脚本：核心面积排名、画质对照、资源总表、队列进度。 |
| `jobs/` | 逐图跑路线预检的作业驱动：`run.py`（一台机器一个编辑器）+ `worker.py`（容器内）+ 清单。 |
| `fleet/` | 多机并行。`route_20260916` 是静态分片版；`newroute_20260916` 是拉取队列版（推荐）。 |
| `catalog/start_positions/` | 每张地图的候选起点坐标。管线没有它跑不起来。 |

## 必读

三份文件比代码更值钱，改动采集、深度、光照或角色之前先看：

- `pipeline/FINDINGS.md` — 每一条都是「画面正常、帧数对、时间戳均匀、日志全绿，但数据是错的」。
- `pipeline/RENDER_QUALITY.md` — 试过的每一套曝光/光照/抗锯齿方案，带测量值和结论。
- `pipeline/BORROWED.md` — 与另一条管线（UE 5.6 `WorldModelCollect`）的分歧，以及为什么。

## 跑起来

```bash
# 单图：冻结路线 → 采集 → 打包 → QA，全程复用同一条 UnrealCV 连接
MAP=/Game/... PORT=9208 python3 pipeline/run_task.py pipeline/tasks/<task>.json

# 多机：任务表放 S3，每台机器原子认领第一个没人要的任务
python3 fleet/newroute_20260916/prepare.py     # 打包并上传
python3 fleet/newroute_20260916/launch.py 0 1 2 3 4 5 6 7 8 9
```

三条不能违反的约束，都是踩出来的：

- **一台机器只能有一个编辑器**，并且**每张地图重启**。两个编辑器会在共享的 `Saved/`、`Intermediate/`
  和 DDC 上死锁；在活着的编辑器里切地图会带着上一关的流送状态和导航网。
- **绝不重连 UnrealCV**。每建立一条连接就多开一个服务线程，第二条会把这个构建搞死。
- **生成任何人形之前先关掉骨骼资产的异步编译**（`Editor.AsyncSkinnedAssetCompilation 0`、
  `Editor.AsyncAssetCompilation 0`），否则第一次 spawn 会永久阻塞。

## 边界

- 运行目录里的路径是绝对路径，指向本机的部署位置（`WM-Unreal-data-collection/local_run/pipeline`）。
  不要假设某个脚本里的路径能在本仓库内解析。
- `Content/`（Fab 与 Epic 资源包）按个人账号商业授权，**不可再分发**，已在 `.gitignore` 里挡掉。
- 录制产物（JPEG / EXR / MP4 / 导航网二进制）不入库：一集就是 1–2 GiB。

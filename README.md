# Emby 新增演员增量补全

独立 Docker 小任务：只刷新启用后新增、具有可靠 TMDB ID 的人物，避免定期刷新全部历史演员。与 Strm Assistant 分开部署，正常升级 Strm Assistant 不会覆盖本项目的代码、配置或进度。

- 镜像：[`wenzhanquan/emby-new-actors`](https://hub.docker.com/r/wenzhanquan/emby-new-actors)
- 源码：<https://github.com/wenzhanquan/emby-new-actors>
- 支持 Linux amd64、arm64；Python 标准库实现。
- 默认周一至周五 **09:40，Asia/Shanghai** 执行，无夜间补跑。
- 每次最多50人、单线程、请求间隔2秒；失败限次重试、锁定资料跳过。

## 它做什么、不做什么

读取人物的创建时间和 ProviderIds，只对日期边界之后、具有 TMDB ID 的人物调用 Emby 单项元数据刷新。新电影复用了库中已有演员时，不会重新刷新这个历史演员。不是扫视频、全库刮削或删除工具，也不会按同名搜索猜演员身份。

没有 TMDB ID 的人物等待最多七天，仍无ID则标记人工检查；刷新请求被拒绝最多重试三次。`submitted` 表示 Emby 已接收请求，不代表上游一定提供了中文名、简介和照片。本项目调用当前 Emby 元数据提供器，**不完全替代 Strm Assistant 的全库演员去重、别名繁简转换等内部功能**。

## 首次安装

下面命令在 **NAS SSH/Linux终端** 中执行。需要 Docker Compose、当前 Emby 的 data 目录，以及 Emby已有 API 密钥对应的应用名称。本项目通过只读认证数据库读取它，不需要把密钥写进Compose或聊天。

### 1. 准备目录和 Compose

```sh
mkdir -p /path/to/emby-new-actors
cd /path/to/emby-new-actors
curl -fL https://raw.githubusercontent.com/wenzhanquan/emby-new-actors/main/compose.yaml -o compose.yaml
curl -fL https://raw.githubusercontent.com/wenzhanquan/emby-new-actors/main/.env.example -o .env
```

修改 `.env`，把 `EMBY_DATA_DIR` 改成你的 Emby 配置下的 **data目录**，里面应有 `authentication.db`。这是 Emby 的管理数据目录，不是电影媒体目录。不要只挂载单个db文件：SQLite还需要读取同目录WAL/SHM。

### 2. 初始化（仅全新部署做一次）

以下示例假定已在 Emby 后台「高级→API密钥」创建名为 `emby-toolkit` 的应用密钥；如名称不同，修改 `--api-app-name`。无需复制或公开密钥本身。

```sh
docker pull wenzhanquan/emby-new-actors:1.0.0
docker run --rm --network host \
  -v "$PWD:/bootstrap" \
  -v "/你的Emby配置/data:/emby-auth:ro" \
  wenzhanquan/emby-new-actors:1.0.0 \
  python /app/initialize.py --api-app-name emby-toolkit
```

注意上面第二个挂载路径也要换成 `.env` 中填写的实际目录。初始化会自动验证 Emby、读取ServerId，并以**初始化的当前时间**作为边界，不会回填历史。若配置或进度已存在会拒绝覆盖。

默认通过NAS本机 `http://127.0.0.1:8096/emby` 连接主Emby，使用host网络，不开放新端口。若Emby端口不同，先使用命令行下载源码、自行修改初始化工具中的本机端口，或向维护者反馈。它不是公开网页服务。

### 3. 启动

```sh
docker compose up -d
docker logs --tail 20 emby-new-actors
```

现在可在NAS的Docker容器列表找到 **emby-new-actors**。首次启动不会立即执行刷新，日志会显示下次时间。若09:40已过，则下个工作日运行；周末新增的人物留到周一。不要同时启用旧版同名systemd定时任务。

## 脚本丢了，怎样恢复

**镜像包含运行脚本，不需要本地源码才能运行。** 丢失源码目录但配置/进度仍在时，重新下载 `compose.yaml` 和 `.env.example`，填好Emby路径，再拉镜像启动即可。

务必单独备份以下文件，镜像不会包含你的私有配置和进度：

```text
emby-new-actors/
├── compose.yaml
├── .env
├── config.docker.json       # 服务身份、时间边界、API应用名，无明文密钥
└── data/
    ├── state.json          # 人物处理进度，最重要
    └── scheduler.json      # 当天是否已执行，防重启重复运行
```

恢复时先停容器，再放回 `config.docker.json` 和整个 `data/`，之后 `docker compose up -d`。**已有备份时不要重新初始化，不要删state.json排错。** 如果连进度也丢了，只能全新初始化，从恢复当天往后处理；丢失期间的待处理项无法从镜像恢复，不会因此自动重刷历史。

## 升级、停用、检查

```sh
# 更新到Compose指定版本；需要升级时先修改image版本号
docker compose pull
docker compose up -d

# 停用，保留数据
docker compose stop

# 只读预演：不刷新，不改进度
docker exec emby-new-actors python /app/refresh_new_actors.py --config /config/config.json --dry-run

# 最近日志
docker logs --tail 50 emby-new-actors
```

也提供 `latest` 标签，但示例固定版本，避免意外升级。容器已禁止Watchtower自动更新。

`data/scheduler.json` 中 `next_run` 是下次时间，`last_exit_code` 是最近任务退出码。健康检查主要检查调度状态；健康不等于每个人物都补全成功，Docker本身不会仅因unhealthy自动重启。源资料问题请结合Emby日志判断。

## 配置和安全边界

- 不需要Docker Socket、特权模式、对外监听端口或写入Emby数据库。
- Emby认证目录只读挂载；凭据仅在内存和本机HTTP请求头中使用，不写日志。
- `config.docker.json` 的 `server_id` 校验防止误连其他服务器，`baseline` 固定历史边界；不要随意改小日期。
- 默认CPU上限0.2核、内存192MB、容器根文件系统只读，日志轮转。
- `max_refresh` 默认为50/次。调度时间目前固定工作日09:40；要改时间需修改scheduler.py、测试并自行构建，不支持只改TZ环境变量来改变该计划。
- 目前认证DB结构按Emby4.10系列验证（`Tokens_2`）。其他版本、非Linux或数据库结构变化可能不兼容，会报错停止，不会降级为全库刷新。
- 原系统版迁移到Docker时，保留原baseline和完整state，并把旧last_run对应上海日期记录为scheduler的last_attempt_date，再停用旧timer，防止当天重复。

## 开发与发布

```sh
python -m unittest discover -s . -q
docker build -t emby-new-actors:local .
```

本仓库CI执行单元测试。镜像发布流程在 `.github/workflows/docker-image.yml`，手动执行时需要仓库Secrets：`DOCKERHUB_USERNAME` 和 `DOCKERHUB_TOKEN`。首次发行通过账号已有的安全发布通道构建，凭据没有复制到源码。

MIT许可证。本项目是独立工具，不打包或分发Strm Assistant插件。

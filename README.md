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

## Docker Compose 部署（推荐）

默认镜像为 **`wenzhanquan/emby-new-actors:latest`**，不需要下载Python脚本、不需要本地构建。

### 1. 准备目录

以下命令在NAS SSH/Linux终端执行，也可通过NAS文件管理器建立目录：

```sh
mkdir -p /path/to/emby-new-actors
cd /path/to/emby-new-actors
```

### 2. 保存完整的 compose.yaml

将以下内容保存为项目目录下的 `compose.yaml`。`init` 是仅首次部署手动执行的初始化服务，平时只启动 `emby-new-actors`。

```yaml
services:
  emby-new-actors:
    image: wenzhanquan/emby-new-actors:latest
    container_name: emby-new-actors
    restart: unless-stopped
    init: true
    network_mode: host
    read_only: true
    cap_drop:
      - ALL
    security_opt:
      - no-new-privileges:true
    cpus: 0.20
    mem_limit: 192m
    pids_limit: 32
    environment:
      TZ: Asia/Shanghai
    volumes:
      - ./config.docker.json:/config/config.json:ro
      - ./data:/data
      - ${EMBY_DATA_DIR:?Set EMBY_DATA_DIR in .env}:/emby-auth:ro
    tmpfs:
      - /tmp:size=16m,noexec,nosuid
    healthcheck:
      test: ["CMD", "python", "/app/scheduler.py", "--healthcheck"]
      interval: 60s
      timeout: 5s
      start_period: 15s
      retries: 3
    logging:
      driver: json-file
      options:
        max-size: 5m
        max-file: "2"
    labels:
      com.centurylinklabs.watchtower.enable: "false"
      description: "新增演员增量补全：工作日09:40，跳过历史人物，Strm Assistant升级不覆盖"

  init:
    image: wenzhanquan/emby-new-actors:latest
    profiles: [setup]
    restart: "no"
    network_mode: host
    read_only: true
    cap_drop: [ALL]
    security_opt:
      - no-new-privileges:true
    entrypoint: ["python", "/app/initialize.py"]
    command:
      - --api-app-name
      - ${EMBY_API_APP_NAME:-emby-toolkit}
      - --base-url
      - ${EMBY_BASE_URL:-http://127.0.0.1:8096/emby}
    volumes:
      - ./:/bootstrap
      - ${EMBY_DATA_DIR:?Set EMBY_DATA_DIR in .env}:/emby-auth:ro
```

### 3. 保存 .env

在同一目录新建 `.env`，修改下面的路径和应用名称：

```dotenv
EMBY_DATA_DIR=/你的Emby配置目录/data
EMBY_API_APP_NAME=emby-toolkit
EMBY_BASE_URL=http://127.0.0.1:8096/emby
```

- `EMBY_DATA_DIR` 是包含 **authentication.db** 的管理data目录，不是存放电影的目录。整个目录只读挂载，以便读取SQLite的WAL/SHM。
- `EMBY_API_APP_NAME` 是Emby后台「高级→API密钥」中已有密钥的**应用名称**。例如创建一个名为 `emby-toolkit` 的应用密钥，这里填这个名字，不要填密钥内容。
- `EMBY_BASE_URL` 必须使用NAS本机 `127.0.0.1`；Emby映射端口不是8096时在这里修改。

也可从仓库直接下载这两个文件：

```sh
curl -fL https://raw.githubusercontent.com/wenzhanquan/emby-new-actors/main/compose.yaml -o compose.yaml
curl -fL https://raw.githubusercontent.com/wenzhanquan/emby-new-actors/main/.env.example -o .env
```

下载后仍需按实际情况修改 `.env`。

### 4. 首次初始化并启动

```sh
# 拉取Docker Hub的latest镜像
 docker compose pull

# 仅全新安装做一次：自动生成私有配置和进度，历史人物不会回填
 docker compose --profile setup run --rm init

# 启动常驻调度容器
 docker compose up -d

# 查看下次执行时间
 docker compose logs --tail 20 emby-new-actors
```

初始化以当前时间为处理边界，生成 `config.docker.json` 和 `data/state.json`。如果已有配置或进度会拒绝覆盖。**从备份恢复时跳过初始化，直接启动。** 不要先启动常驻容器再初始化，否则不存在的配置文件可能被Docker创建成目录。

启动后在NAS Docker列表中搜索 **emby-new-actors** 即可管理。容器使用host网络，无新端口或网页管理界面；首次启动不立即刷新，工作日09:40才执行，过时不补跑。不要同时启用旧版systemd定时任务。

如果你只使用NAS图形化Compose项目管理：先将上述文件放入项目目录，在该目录执行一次初始化命令，再从界面启动常驻服务；不要在界面直接启用setup profile让初始化服务反复运行。

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
# 手动拉取最新镜像并重建容器，保留配置和进度
docker compose pull
docker compose up -d

# 停用，保留数据
docker compose stop

# 只读预演：不刷新，不改进度
docker exec emby-new-actors python /app/refresh_new_actors.py --config /config/config.json --dry-run

# 最近日志
docker logs --tail 50 emby-new-actors
```

默认使用 `latest` 标签；需要固定版本时可把image改为 `wenzhanquan/emby-new-actors:1.0.0`。容器已禁止Watchtower自动更新，更新时手动执行上面的pull和up命令。

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

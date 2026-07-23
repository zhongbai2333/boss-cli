# 讯飞星辰 Agent 自定义 Plugin 接入

本项目以 **HTTP 自定义插件** 接入讯飞星辰 Agent，不依赖 MCP 权限。插件将 BOSS 直聘与中国公共招聘网的岗位转换为同一 JSON 结构，供工具节点或 Agent 智能决策节点使用。

## 合规边界

- 中国公共招聘网网站声明要求转载或使用招聘信息前取得相应合法授权。本项目只有在 `MOHRSS_AUTHORIZED=true` 时才会访问该来源。
- 请保存项目组书面授权材料，并按授权约定控制用途、频率、保存期限和展示范围。
- 返回结果保留 `source_name` 与 `url`，必须展示信息来源，不得歪曲或篡改内容。
- 插件不返回联系人、联系电话或详细单位地址等非搜索必需信息。
- 公共网请求间隔不低于 2 秒；单次调用最多扫描 5 页，每页最多 20 条。

## 启动服务

复制 `.env.example` 为 `.env`，然后配置：

- `MOHRSS_AUTHORIZED=true`：仅在项目已取得书面授权时设置。
- `PLUGIN_API_KEY`：必填。生成一个随机密钥，用于星辰 Service/Header 鉴权；未设置时业务接口默认返回 `503`。
- `BOSS_COOKIES`：BOSS 登录 Cookie；不使用 BOSS 来源时可留空。
- `BOSS_PLUGIN_CACHE_TTL_S=1800`：BOSS 查询的 SQLite 缓存有效期，默认 30 分钟，代码强制最低 60 秒。
- `BOSS_PLUGIN_MIN_INTERVAL_S=10`：缓存未命中时，BOSS 回源请求的最小间隔，默认 10 秒，代码强制最低 5 秒。
- `PUBLIC_PLUGIN_CACHE_TTL_S=1800`：中国公共招聘网的独立 SQLite 缓存有效期，默认 30 分钟。
- `PUBLIC_PLUGIN_MIN_INTERVAL_S=2`：中国公共招聘网的独立跨进程回源间隔，默认 2 秒，最低 1 秒。
- `LLM_SEMANTIC_CACHE_ENABLED=false`：是否启用 LLM 语义缓存别名；默认关闭。
- `LLM_BASE_URL`、`LLM_API_KEY`、`LLM_MODEL`：OpenAI-compatible `/chat/completions` 服务配置。
- `LLM_SEMANTIC_CONFIDENCE=0.92`：接受语义别名的最低置信度。
- `LLM_SEMANTIC_ALIAS_TTL_S=604800`：语义别名缓存时间，默认 7 天。
- `LLM_TIMEOUT_S=5`：单次语义规划超时，限制为 1–15 秒且不自动重试。

本地安装后运行 `boss-plugin`，默认只监听 `127.0.0.1:8000`。仅本机开发且明确不需要鉴权时，可设置 `PLUGIN_ALLOW_ANONYMOUS=true`；不要在公网、局域网或容器生产部署中启用该开关。容器入口会显式监听 `0.0.0.0:8000`，仓库的 Compose 默认只映射到宿主机 `127.0.0.1:8000`。

### 从本地一键迁移完整配置

如果云端不方便直接登录和逐项配置，可以先在本地完成 Chrome 登录与插件调试，确认可用后导出一个加密完整配置包：

```bash
boss login --cdp
boss status
boss config-export boss-cloud.bossconfig
```

导出包包含：

- BOSS 登录 Cookie。
- 插件 API Key 与监听参数。
- 各数据平台的授权开关、缓存 TTL 和限流参数。
- LLM 语义缓存开关、兼容端点、模型、API Key、阈值、TTL 与超时。

导出包不包含：SQLite 结果缓存、请求预约状态、Chrome Profile、浏览器可执行文件、本地路径、日志、测试数据或导出密码。`BOSS_CREDENTIAL_PASSPHRASE` 永远不会被打包，文件与密码必须分开传输。

将 `boss-cloud.bossconfig` 通过安全通道上传到服务器。直接安装运行时，可执行：

```bash
boss config-import boss-cloud.bossconfig --verify
boss status
# 重启已运行的 boss-plugin，使 plugin.env 中的全部配置生效
```

使用仓库的 Compose 部署时，`boss-cli-config` 命名卷会同时持久保存导入后的 `credential.json`、`plugin.env` 和 `cache.db`。启动容器后可将配置包复制进去并一键导入：

```bash
docker compose up -d
docker compose cp boss-cloud.bossconfig job-plugin:/tmp/boss-cloud.bossconfig
docker compose exec job-plugin boss config-import /tmp/boss-cloud.bossconfig --force --verify
docker compose exec job-plugin rm /tmp/boss-cloud.bossconfig
docker compose restart job-plugin
```

密码默认以隐藏方式交互输入。无人值守部署应通过密钥管理服务向容器注入 `BOSS_CREDENTIAL_PASSPHRASE`，不要把密码写入命令行、镜像、Compose 文件或 Git。完整配置包使用 Scrypt + AES-256-GCM 加密并认证元数据；导入前会完整解密校验，写入或在线验证失败时同时回滚登录态和插件配置。导入完成后及时删除上传文件。

导入配置写入 `~/.config/boss-cli/plugin.env`。插件启动时会优先加载该受管文件，并覆盖 Compose `env_file` 中同名的可移植配置，确保本地确认过的快照在容器重启后真正一键生效。导出密码等不在白名单内的运行时变量不会被覆盖。若要改回云端环境配置，请重新导入配置包，或删除该文件后重启服务。旧版 `boss credential-export/import` 和仅包含 Cookie 的 `.bosscred` 包继续兼容，但新部署推荐使用 `config-export/import`。

`BOSS_COOKIES` 仍可用于现有部署；如果同时存在环境变量和导入凭证，已保存凭证会先被读取。若需要切换到环境变量方式，请先执行 `boss logout` 清除导入凭证。

### 多平台缓存与限流

星辰 Agent 可能重复调用插件，或并发生成多个相似查询。插件使用同一个 SQLite 缓存引擎，但每个平台拥有独立命名空间、TTL、限流槽和授权规则。目前支持 `boss` 与 `public`，后续新增平台时也应通过独立来源适配器接入，不能共用上游限流状态。

1. 每个来源先读取 `~/.config/boss-cli/cache.db` 中自己的 SQLite 缓存；命中且未过期时不会访问对应上游。
2. BOSS 缓存键额外包含不可逆账号指纹，不同账号不会串用数据；公共网仍会在读取缓存前检查书面授权开关。
3. `source=all` 会分别查询两个来源：BOSS 命中不会影响公共网刷新，公共网命中也不会绕过 BOSS 的账号保护。
4. 缓存未命中时，每个平台分别通过 SQLite 跨进程预约请求时隙。BOSS 默认至少间隔 10 秒，公共网默认至少间隔 2 秒。
5. 并发的相同来源请求在等待限流时隙后会再次检查自己的缓存，避免缓存击穿。
6. 缓存只覆盖当前请求实际需要的数据，不为提高命中率而主动预取额外页面；较小 `limit` 可复用较大结果缓存。
7. 过期条目在读取时立即删除并回源刷新；无效 JSON 缓存也会自动删除。
8. “打招呼”等具有副作用的操作永不使用响应缓存。

`boss-cli-config` 命名卷已经持久保存该数据库，因此容器重启后各平台缓存和请求预约状态仍有效。账号敏感或调用量较大时，建议把 `BOSS_PLUGIN_MIN_INTERVAL_S` 提高到 `15` 或 `30`，并把 `BOSS_PLUGIN_CACHE_TTL_S` 提高到 `3600`。不要为了追求实时性将它们调低；代码会拒绝低于安全下限的配置。

### LLM 语义缓存别名

可选 LLM 层已经实现。它用于将“Python 服务端”“Python 后端开发”等高置信度同义表达映射到同一个平台缓存键，提高星辰 Agent 改写问题后的命中率。该能力默认关闭，启用时必须同时配置 `LLM_API_KEY` 和 `LLM_MODEL`。

安全边界如下：

1. 模型按平台独立规划，`boss` 与 `public` 使用不同的 SQLite 语义别名命名空间，不能跨平台命中。
2. 模型只输出 `canonical_keyword` 与 `confidence`，只用于计算缓存键。实际发往招聘平台的关键词始终是用户原始关键词。
3. 城市、来源、页码、数量、筛选、授权状态和回源参数均由本地代码锁定，模型无权修改。
4. 只有置信度达到阈值、长度合法且不含换行或空字符的输出才会接受；其余情况原样查询。
5. 模型超时、HTTP 错误、非法 JSON 或低置信度不会阻断插件，也不会直接触发额外回源；安全回退会短暂写入负缓存，防止 Agent 循环重复调用模型。
6. SQLite 只保存不可逆缓存键、规范关键词和置信度，不保存 `LLM_API_KEY`，也不会把 BOSS Cookie 发送给模型。
7. 即使语义缓存未命中，后续平台请求仍必须经过该平台原有的授权检查、过期检查和 SQLite 跨进程限流闸门。

启用示例配置：

```dotenv
LLM_SEMANTIC_CACHE_ENABLED=true
LLM_BASE_URL=https://你的兼容服务/v1
LLM_API_KEY=由密钥管理服务注入
LLM_MODEL=你的模型名称
LLM_SEMANTIC_CONFIDENCE=0.92
LLM_SEMANTIC_ALIAS_TTL_S=604800
LLM_TIMEOUT_S=5
```

不要把 `LLM_API_KEY` 写入 Compose 文件、镜像或 Git。对于不支持 `response_format=json_object` 的兼容服务，规划会安全降级为精确缓存；可以保持功能关闭，现有 SQLite 缓存和平台限流不受影响。

### GitHub Actions 与 GHCR 镜像

仓库的 `.github/workflows/docker.yml` 会自动构建 Linux 容器镜像：

- 推送到 `main`：发布 `ghcr.io/zhongbai2333/boss-cli:latest` 和 `sha-<提交号>`。
- 推送 `v*` 标签：额外发布对应版本标签。
- Pull Request：只验证镜像能否构建，不推送镜像。
- Actions 页面手动运行：构建并发布当前分支对应的 SHA 标签。

镜像发布后可在 GitHub 仓库的 **Packages** 区域查看。公开部署前，请在包设置中确认镜像可见性；私有镜像拉取时需要具有 `read:packages` 权限的 GitHub Token。

部署时为容器传入 `.env` 中的配置，并映射容器端口 `8000`。镜像本身不会包含 `.env`、Cookie、API Key、测试文件或本地 Git 历史。

服务端点：

- 健康检查：`GET /health`
- OpenAPI：`GET /openapi.json`
- Swagger 调试页：`GET /docs`
- 插件动作：`POST /api/v1/jobs/search`

生产环境应配置 `PLUGIN_API_KEY`、使用 HTTPS 反向代理，并只向星辰平台开放插件地址。不要将 `.env`、Cookie 或 API Key 提交到 Git。

### Chromium 自动补全动态令牌

Compose 部署包含一个仅 Docker 内网可访问的 `chromium` Sidecar。它没有宿主机端口映射，也不会加载 `.env` 或挂载插件凭据目录；独立命名卷仅保存专用浏览器 Profile。

当 BOSS 搜索明确返回 `code=37` 时，插件会执行一次受控恢复：

1. 清理专用浏览器上下文中的旧 BOSS Cookie；
2. 注入当前会话 Cookie，但不注入已失效的 `__zp_stoken__`；
3. 打开一次 BOSS 用户页面，让浏览器 JavaScript 尝试生成新令牌；
4. 导出同一浏览器上下文的完整 Cookie，成功后只重放一次幂等搜索；
5. 若失败，沿用凭据指纹冷却，后续请求不会反复启动恢复或撞击 BOSS 上游。

CDP 地址默认为 `http://chromium:9223`，只允许回环、私网 IP 或固定 Sidecar 服务名，不得发布 CDP 端口到公网。Debian Chromium 自身只在容器 loopback 的 `9222` 监听，容器内 `socat` 将 Compose 私网的 `9223` 转发至该 loopback 端口。Sidecar 保持非 root、只读文件系统和独立 Profile，并使用 Debian Chromium 自带的 sandbox；没有使用 `--no-sandbox` 或 `privileged`。由于 Docker 默认能力集无法创建 Chromium sandbox 所需 namespace，Sidecar 额外授予 `SYS_ADMIN`，因此它必须继续保持仅两个受信容器可达的私有网络边界。验证码、手机确认、`code=121/122` 安全拦截及非幂等操作均不会自动重试；这些场景仍需人工登录，届时可再启用受 SSH 隧道保护的 VNC 方案。

## 星辰平台配置

在星辰 Agent 开发平台进入 **资源管理 → 自定义插件 → 新建插件**：

1. 基本信息：名称可填写“招聘双来源搜索”，描述说明支持 BOSS 与中国公共招聘网。
2. 请求方式选择 `POST`，URL 填写 `https://你的域名/api/v1/jobs/search`。
3. 授权方式选择 `Service`，密钥传入位置选择 `Header`：
   - 参数名：`X_Plugin_Key`（星辰参数名只允许字母、数字和下划线）。
   - 参数值：与服务端 `PLUGIN_API_KEY` 一致。

插件同时兼容标准 HTTP 请求头 `X-Plugin-Key`，便于其他客户端调用。若一次请求同时携带两种请求头且值不同，插件会拒绝该请求，避免代理层请求头歧义。
4. 请求 Body 按 `/openapi.json` 中的 `JobSearchRequest` 配置：
   - `keyword`（string，必填）：职位关键词。
   - `city`（string）：城市/地区，默认“全国”。
   - `source`（string）：`all`、`boss` 或 `public`。
   - `page`（integer）：起始页，1–100。
   - `limit`（integer）：每个来源最多返回 1–20 条。
   - `public_scan_pages`（integer）：公共网最多顺序扫描 1–5 页。
5. 输出参数可按 `JobSearchResponse` 配置，至少包括 `ok`、`partial`、`count`、`jobs`、`errors` 与 `attribution`。
6. 在调试页输入测试参数，成功后发布插件，再在工作流的工具节点或 Agent 智能决策节点中添加该插件。

推荐测试 Body：

```json
{
  "keyword": "Python",
  "city": "北京",
  "source": "all",
  "page": 1,
  "limit": 10,
  "public_scan_pages": 3
}
```

## 返回与降级语义

- `ok=true, partial=false`：所选来源全部调用成功。
- `ok=true, partial=true`：一个来源成功、另一个来源失败；`jobs` 仍可使用，失败原因位于 `errors`。
- `ok=false`：所有所选来源均失败。
- 每条岗位都有统一字段：`id`、`source`、`source_name`、`title`、`company`、`salary`、`location`、`experience`、`education`、`description`、`published_at`、`url`。

## 公共招聘网搜索说明

该旧站当前的“全文/岗位/单位”服务端关键词模式会对部分有效关键词返回空数据。插件因此只抓取用户指定的公开分页，并在这些页内本地过滤关键词与城市；`public_scan_pages` 用于扩大一个受控的小范围。插件不会自动遍历全站，也不会绕过验证码、登录或访问控制。
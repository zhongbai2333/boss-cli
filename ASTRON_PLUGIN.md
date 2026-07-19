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
- `PLUGIN_API_KEY`：生成一个随机密钥，用于星辰 Service/Header 鉴权。
- `BOSS_COOKIES`：BOSS 登录 Cookie；不使用 BOSS 来源时可留空。

本地安装后运行 `boss-plugin`，默认监听 `0.0.0.0:8000`。也可使用仓库的 `Dockerfile` 与 `compose.yaml` 部署。

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

生产环境应使用 HTTPS 反向代理，并只向星辰平台开放插件地址。不要将 `.env`、Cookie 或 API Key 提交到 Git。

## 星辰平台配置

在星辰 Agent 开发平台进入 **资源管理 → 自定义插件 → 新建插件**：

1. 基本信息：名称可填写“招聘双来源搜索”，描述说明支持 BOSS 与中国公共招聘网。
2. 请求方式选择 `POST`，URL 填写 `https://你的域名/api/v1/jobs/search`。
3. 授权方式选择 `Service`，密钥传入位置选择 `Header`：
   - 参数名：`X-Plugin-Key`
   - 参数值：与服务端 `PLUGIN_API_KEY` 一致。
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
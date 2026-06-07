# fuckoai Linux

Linux 容器版注册控制面板。

## 功能

- Web 控制面板：`/ui`
- 临时邮箱队列生成和验证码读取
- 购买组可视化配置，保存到本地 `data/purchase_config.json`
- Linux 图形浏览器自动注册入口，通过 Xvfb、x11vnc、noVNC 查看

## 文件结构

```text
server.py                  # 本地 API 和 Web 控制面板服务
control_panel.html         # Linux Web 控制面板
uc_signup.py               # Linux 浏览器自动注册脚本
config.json                # 应用配置默认值
Dockerfile                 # Linux 容器镜像
docker-compose.yml         # fuckoai 服务
scripts/start_linux_vnc.sh # Xvfb/VNC/noVNC + server 启动脚本
```

运行数据放在 `data/`，`.env` 和 `data/` 不进入 git，也不进入 Docker build context。

## 配置

`.env` 只放管理员密码：

```env
ADMIN_PASSWORD=你的控制面板管理员密码
```

`ADMIN_PASSWORD` 可选；设置后访问 `/ui` 需要登录。

其他设置写在 `config.json`，也可以在控制面板“设置”页保存。仓库默认 `config.json` 已包含 HeroSMS 接口地址、注册资料默认值和浏览器参数；接口密钥、临时邮箱、CPA 等用户配置默认为空。

## 购买配置

购买参数统一维护在控制面板“设置”页，保存后写入 `data/purchase_config.json`。该文件位于 `data/`，不会进入 git。

默认仓库不提供具体国家、运营商、价格等购买组。首次使用前需要在控制面板新增购买组。

服务端会按已启用购买组顺序尝试买号，失败时自动试下一组。

## 启动

```bash
docker compose up -d --build fuckoai
```

访问：

```text
http://127.0.0.1:3030/ui
```

查看容器：

```bash
docker compose ps
docker logs --tail 80 fuckoai
```

## Linux 本地运行

```bash
python3 server.py
```

如果需要浏览器画面：

```bash
./scripts/start_linux_vnc.sh
```

## 邮箱队列

控制面板只保留随机前缀模式。填写邮箱后缀域名和数量后，会生成：

```text
随机前缀@example.com
```

生成后的队列仍可手动编辑，一行一个邮箱。

## API

基础地址：

```text
http://127.0.0.1:3030/api
```

常用接口：

- `GET /api/health`
- `POST /api/purchase`
- `GET /api/purchase-settings`
- `POST /api/purchase-settings`
- `GET /api/email-queue`
- `POST /api/email-queue`
- `POST /api/email-queue/generate`
- `GET /api/uc-signup/status`
- `POST /api/uc-signup/start`
- `POST /api/uc-signup/stop`
- `GET /api/uc-signup/logs`

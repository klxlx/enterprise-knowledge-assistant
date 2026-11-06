# 部署参考

本目录提供单机私有部署的参考配置，路径、域名、端口和用户请按自己的环境调整。

| 文件 | 用途 |
| --- | --- |
| `robot-rag.service` | systemd 服务单元，默认监听 `127.0.0.1:8100` |
| `robot-locations.conf` | Nginx / OpenResty location 配置，含 Basic 登录保护 |
| `robot-api-limit.conf` | API 限流 zone，需要加载在 Nginx 的 http 上下文 |
| `set-access-password.py` | 生成 htpasswd 与私有凭据文件 |
| `Dockerfile.pgvector` | PostgreSQL 17.11 + pgvector 0.8.6 镜像构建 |

## 部署步骤

1. 参考根目录 `.env.example`，在服务器创建 `/etc/robot-rag.env`（root 所有，权限 `0600`），填入模型 Key、数据库密码等真实配置。
2. 创建虚拟环境并安装 `requirements-deployed.txt`，后端入口为 `uvicorn api:app`。
3. 先创建 PostgreSQL 数据库并执行 `CREATE EXTENSION vector`，再在发布目录运行 `python database.py` 初始化表。
4. 前端执行 `npm run build`，将 `frontend/dist` 发布到 Nginx 静态目录；如部署在子路径，设置 `VITE_BASE_PATH`。
5. 按 `robot-rag.service` 配置 systemd，按 `robot-locations.conf` 配置 Nginx，执行 `nginx -t` 后 reload。
6. 健康检查：`curl http://127.0.0.1:8100/api/health`，应返回 `database_ready=true`。

## 安全注意事项

- 后端只监听 `127.0.0.1`，通过带认证的反向代理对外提供服务。
- 不要提交 `/etc/robot-rag.env`、htpasswd、数据库备份、`chat_history/`、日志或 API Key。
- 修改密码可运行 `set-access-password.py`；如使用 Docker 中的 OpenResty，可通过 `OPENRESTY_CONTAINER` 指定容器名。
- 一旦模型 Key 或数据库密码泄露，立即在对应平台撤销并替换。

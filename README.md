# 拼豆图纸生成器

把图片转换成带色号网格的拼豆图纸，并统计每种颜色的用量。项目包含网页界面、HTTP API、Docker 配置，以及自动发布到 GitHub Container Registry 的工作流。

## 给使用者：一条命令部署

镜像发布后，其他人无需下载源代码，只需安装 Docker 并运行：

```bash
docker run -d \
  --name bead-pattern \
  -p 8000:8000 \
  --restart unless-stopped \
  --read-only \
  --tmpfs /tmp:size=64m \
  ghcr.io/michu0126/bead-pattern-generator:latest
```

浏览器打开 `http://服务器IP:8000`。

也可以从 Docker Hub 部署：

```bash
docker run -d \
  --name bead-pattern \
  -p 8000:8000 \
  --restart unless-stopped \
  你的DockerHub用户名/bead-pattern-generator:latest
```

也可以下载 `compose.yaml`，设置镜像地址后运行：

```bash
BEAD_PATTERN_IMAGE=ghcr.io/michu0126/bead-pattern-generator:latest docker compose up -d
```

镜像同时支持常见的 `linux/amd64` 和 `linux/arm64` 服务器。

## 开发者：本机构建

需要安装 Docker Desktop。在项目目录运行：

```bash
docker build -t bead-pattern-generator .
docker run --rm -p 8000:8000 bead-pattern-generator
```

浏览器打开 <http://localhost:8000>。

## 上传到 GitHub

1. 在 GitHub 新建一个空仓库，例如 `bead-pattern-generator`。
2. 在本项目目录初始化并推送：

```bash
git init
git add .
git commit -m "Initial bead pattern generator"
git branch -M main
git remote add origin https://github.com/你的用户名/bead-pattern-generator.git
git push -u origin main
```

推送后，GitHub Actions 会先运行测试，再构建多架构镜像并发布至：

```text
ghcr.io/michu0126/bead-pattern-generator:main
```

默认分支还会发布 `latest` 标签。创建 `v1.0.0` 这类 Git 标签时，会同时发布对应的版本标签。

## 同步发布到 Docker Hub

GitHub Actions 会在同一次构建中将镜像推送到 GHCR 和 Docker Hub，无需运行第二次构建。先在 Docker Hub 创建与 GitHub 仓库同名的公开仓库，然后创建一个访问令牌（Access Token）。

进入 GitHub 仓库的 `Settings → Secrets and variables → Actions`，添加：

- Variables：`DOCKERHUB_USERNAME`，值为 Docker Hub 用户名
- Secrets：`DOCKERHUB_TOKEN`，值为 Docker Hub Access Token

配置完成后，再次推送代码或手动重新运行工作流。镜像将同时出现于：

```text
ghcr.io/michu0126/bead-pattern-generator:latest
DockerHub用户名/bead-pattern-generator:latest
```

不要把 Docker Hub 密码或 Access Token 写进项目文件。

如果希望其他人无需登录即可拉取镜像，请在 GitHub 的 Packages 页面把镜像可见性改为 Public。

## 发布第一个正式版本

```bash
git tag v1.0.0
git push origin v1.0.0
```

公开服务建议在容器前配置 Caddy、Traefik 或 Nginx，以提供域名和 HTTPS。应用本身不保存上传的图片，也不需要数据库或持久化磁盘。

## 运行要求

- 最低建议内存：512 MB
- 推荐内存：1 GB
- 不需要数据库
- 不需要挂载数据卷
- 单张图片最大 12 MB
- 默认监听容器端口：8000

## 色卡说明

当前内置的是 25 色品牌中立示例色卡，色号并不对应任何厂商。购买拼豆前，应把 `app/palette.py` 替换为你实际使用品牌的官方 RGB 色卡。匹配过程在 CIE Lab 色彩空间中进行，比直接比较 RGB 更接近人眼感受。

## API

- `GET /api/health`：健康检查
- `POST /api/generate`：上传图片并生成图纸
- `GET /docs`：交互式 API 文档

限制：图片最大 12 MB；网格边长 8–100；颜色数 2–25。


# 拼豆图纸生成器

把图片转换成 MARD 2.6 mm 拼豆图纸。应用会自动从 221 色参考色卡中匹配颜色，在每个格子内标出色号，并生成用量清单。支持用文字描述主体，在浏览器中先抠图、预览，再选择采用或弃用。

当前版本：**v0.3.0**。页面顶部会显示运行中的版本号和更新内容，并提供“清除缓存并刷新”按钮。

## 功能

- 常见 2.6 mm 板型选项：50×50、52×52，以及对应的横拼和四板组合
- 自动识别画面所需颜色，不设置人为颜色数量上限
- 使用 MARD 2.6 mm 经典 221 色参考表，在 CIE Lab 色彩空间匹配近似色
- 图纸中每一颗豆都显示 MARD 色号
- 文字提示抠图；采用结果后，透明区域显示为空格且不计入豆子用量
- 图片只在本应用中处理；抠图推理由用户浏览器本地完成

> 色卡 RGB 数值是屏幕显示与算法匹配用的参考值。显示器、光照、批次和实物材料都会造成色差，正式制作前建议用手边的实体色卡复核。

## Docker Compose 部署

保存下面的 `compose.yaml`，然后运行 `docker compose up -d`：

```yaml
services:
  bead-pattern:
    image: michu0126/bead-pattern-generator:latest
    container_name: bead-pattern
    ports:
      - "18026:18026"
    restart: unless-stopped
    read_only: true
    tmpfs:
      - /tmp:size=64m
```

浏览器打开 `http://服务器IP:18026`。镜像支持 `linux/amd64` 和 `linux/arm64`。

## 更新已有容器

```bash
docker compose pull
docker compose up -d --force-recreate
```

更新后从页面顶部确认版本号。如果仍显示旧版本，点击“清除缓存并刷新”。也可以访问 `/api/health`，返回的 `version` 是容器实际运行版本。

也可以直接运行：

```bash
docker run -d \
  --name bead-pattern \
  -p 18026:18026 \
  --restart unless-stopped \
  --read-only \
  --tmpfs /tmp:size=64m \
  michu0126/bead-pattern-generator:latest
```

GHCR 镜像地址为 `ghcr.io/michu0126/bead-pattern-generator:latest`。

## 抠图说明

抠图使用 Transformers.js 和 `Xenova/clipseg-rd64-refined`，直接在浏览器里根据文字提示识别主体。第一次使用会从 Hugging Face 下载模型，速度取决于访问网络；下载后通常会进入浏览器缓存。中文常见主体会做简单转换，但较复杂场景建议使用英文描述。

不使用抠图时，生成图纸不依赖外部服务。抠图失败也不会影响原图生成。

## 本地开发

```bash
docker build -t bead-pattern-generator .
docker run --rm -p 18026:18026 bead-pattern-generator
```

应用不需要数据库或持久化数据卷，单张图片最大 12 MB，默认监听端口为 `18026`。

## API

- `GET /api/health`：健康检查
- `GET /api/boards`：可选板子规格
- `POST /api/generate`：字段为图片 `image` 和板型编号 `board`
- `GET /docs`：交互式 API 文档

## 数据与模型来源

- 板型参考：[Artkal 2.6 mm 50×50 / 52×52 pegboard](https://www.artkalfusebeads.com/products/artkal-clear-large-square-pegboard-for-mini-2-6mm-beads-bcp01)
- MARD 经典 221 色参考数据：[pixel-to-beads / mard-color.json](https://github.com/a31521424/pixel-to-beads/blob/main/src/mard-color.json)
- 文字分割模型：[Xenova/clipseg-rd64-refined](https://huggingface.co/Xenova/clipseg-rd64-refined)

## 自动发布

推送到 `main` 后，GitHub Actions 会运行测试，并把多架构镜像同步发布到 Docker Hub 与 GHCR。Docker Hub 凭据通过仓库变量 `DOCKERHUB_USERNAME` 和密钥 `DOCKERHUB_TOKEN` 提供，不应写入代码。

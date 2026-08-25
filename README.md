# 拼豆图纸生成器

把图片转换成 MARD 2.6 mm 拼豆图纸。应用会自动从 221 色参考色卡中匹配颜色，在每个格子内标出色号，并生成用量清单。支持用文字描述主体，在浏览器中先抠图、预览，再选择采用或弃用。

当前版本：**v0.6.0**。页面顶部会显示运行中的版本号和更新内容，并提供“API 设置”和“清除缓存并刷新”按钮。v0.6.0 新增真实 Image API 调用日志、接口模型列表与推荐，并修复 API 设置首次保存后无法继续修改并再次保存的问题。

生成前会检测与图片四周边缘连通的近白色区域，并把这些区域当成背景留空，因此主图外围白底不会被大量标为 H2；主体内部独立、不与边缘相连的白色仍会保留。

## 功能

- 常见 2.6 mm 板型选项：50×50、52×52，以及对应的横拼和四板组合
- 自动识别画面所需颜色，不设置人为颜色数量上限
- 使用 MARD 2.6 mm 经典 221 色参考表，在 CIE Lab 色彩空间匹配近似色
- 图纸中每一颗豆都显示 MARD 色号
- 文字提示抠图；采用结果后，透明区域显示为空格且不计入豆子用量
- 可选 OpenAI 或 OpenAI 兼容的图像编辑 API 云端识图增强
- 容器内 API 设置页可填写 URL、API Key、模型和质量，并测试连接
- 点击任意图纸格子，手动改成其他 MARD 色号或设为空白，并支持逐步撤销
- 本地模式只在浏览器中处理；云端模式会把图片发送给用户配置的 API 服务

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
    environment:
      # 必须改成自己的强密码；留空会锁定网页 API 设置。
      SETTINGS_PASSWORD: ""
    volumes:
      - bead-pattern-data:/data
    read_only: true
    tmpfs:
      - /tmp:size=64m

volumes:
  bead-pattern-data:
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
  -e SETTINGS_PASSWORD='请改成自己的强密码' \
  -v bead-pattern-data:/data \
  michu0126/bead-pattern-generator:latest
```

GHCR 镜像地址为 `ghcr.io/michu0126/bead-pattern-generator:latest`。

## 抠图说明

抠图使用 Transformers.js 和 `Xenova/clipseg-rd64-refined`，直接在浏览器里根据文字提示识别主体。第一次使用会从 Hugging Face 下载模型，速度取决于访问网络；下载后通常会进入浏览器缓存。中文常见主体会做简单转换，但较复杂场景建议使用英文描述。

不使用抠图时，生成图纸不依赖外部服务。抠图失败也不会影响原图生成。

### 可选的 OpenAI 兼容 API 云端增强

根据 OpenAI 官方模型能力，GPT-5.6 接受图片但输出文本，GPT-Image-2 可以接受并输出编辑后的图片，因此本项目用 `gpt-image-2` 做可选的高质量前景分离。程序只采用 AI 结果的透明度遮罩，并把遮罩重新套回原图，避免生成模型改动主体颜色；MARD 色号仍由原图 RGB 和本地 Lab 色差算法确定。

部署时只需要先设置一个网页管理密码：

```yaml
environment:
  SETTINGS_PASSWORD: "请改成自己的强密码"
volumes:
  - bead-pattern-data:/data
```

启动后打开页面顶部的“API 设置”，输入管理密码，即可填写：

- API URL：可以是 `https://api.openai.com/v1` 这样的基础地址，也可以是完整的 `/images/edits` 地址
- API Key：保存在服务端，读取设置时不会返回给浏览器；留空保存表示保留原密钥
- 图像模型：官方 OpenAI 推荐填写 `gpt-image-2`，兼容服务则填写服务商提供的模型 ID
- 图像质量：`low`、`medium`、`high` 或 `auto`

“测试连接”只请求兼容接口的 `/models`，不会生成图片；部分声称兼容但未实现 `/models` 的服务可能无法通过测试。实际云端抠图要求服务实现 OpenAI 风格的 `POST /images/edits`，并在 `data[0].b64_json` 返回 PNG。

API Key 以权限 `0600` 的配置文件保存在 `/data/settings.json`，请给容器挂载持久化数据卷。服务端不会把密钥返回给浏览器，但设置管理密码仍会随请求发送，因此请仅在可信局域网或 HTTPS 下使用。不要把真实密码或密钥提交到 GitHub。

为兼容旧部署，`OPENAI_API_URL`、`OPENAI_API_KEY`、`OPENAI_IMAGE_MODEL` 和 `OPENAI_IMAGE_QUALITY` 环境变量仍可作为尚未保存网页设置时的默认值。

群晖 Container Manager 可以使用项目中的 `compose.synology.yaml`。首次创建项目前，把留空的 `SETTINGS_PASSWORD` 改成自己的强密码。名为 `bead-pattern-data` 的卷会自动保存 API 设置；更新镜像和重建容器不会丢失。

## 图像和颜色是怎样识别的

普通图纸生成并不是用 AI 理解图片内容。程序按所选板型计算每个豆格在原图中的精确覆盖区域，不再先用插值算法缩小图片。区域内的每个原图像素会先独立转换到 CIE Lab 并匹配 MARD 221 色参考表，再按照像素与豆格的实际重叠面积、透明度、局部颜色一致性和中心位置进行投票。这样黑线、白底与主体颜色不会先混合成一个原图中不存在的 RGB；抗锯齿产生的孤立过渡色会降权，细黑轮廓则保留独立优先规则。透明像素和与四周连通的白底不参与投票，接近五五开的真实边界由格子中心附近的源像素确定。

这种方式速度快、结果可重复，但准确度会受到原图光线、阴影、滤镜、透明度、缩放以及参考 RGB 与实体豆批次色差的影响。文字抠图才使用 CLIPSeg 模型理解用户描述，它只负责分离前景，不负责判断 MARD 色号。

## 本地开发

```bash
docker build -t bead-pattern-generator .
docker run --rm -p 18026:18026 bead-pattern-generator
```

应用不需要数据库。启用网页 API 设置时需要把持久化卷挂载到 `/data`；单张图片最大 12 MB，默认监听端口为 `18026`。

## API

- `GET /api/health`：健康检查
- `GET /api/boards`：可选板子规格
- `GET /api/config`：查询可选 AI 功能是否已配置，不返回密钥
- `GET /api/settings`：读取脱敏后的 API 设置，需要 `X-Settings-Password`
- `PUT /api/settings`：保存 API 设置，需要 `X-Settings-Password`
- `DELETE /api/settings/key`：删除已保存的 API Key，需要 `X-Settings-Password`
- `POST /api/settings/test`：测试兼容接口的 `/models`，需要 `X-Settings-Password`
- `POST /api/ai/cutout`：使用配置的 OpenAI 兼容图像编辑接口返回透明背景结果
- `POST /api/generate`：字段为图片 `image` 和板型编号 `board`
- `GET /docs`：交互式 API 文档

## 数据与模型来源

- 板型参考：[Artkal 2.6 mm 50×50 / 52×52 pegboard](https://www.artkalfusebeads.com/products/artkal-clear-large-square-pegboard-for-mini-2-6mm-beads-bcp01)
- MARD 经典 221 色参考数据：[pixel-to-beads / mard-color.json](https://github.com/a31521424/pixel-to-beads/blob/main/src/mard-color.json)
- 文字分割模型：[Xenova/clipseg-rd64-refined](https://huggingface.co/Xenova/clipseg-rd64-refined)

## 自动发布

推送到 `main` 后，GitHub Actions 会运行测试，并把多架构镜像同步发布到 Docker Hub 与 GHCR。Docker Hub 凭据通过仓库变量 `DOCKERHUB_USERNAME` 和密钥 `DOCKERHUB_TOKEN` 提供，不应写入代码。

# 📊 通用型表格解析引擎 — Universal Table Parsing Engine

> 上传表格图片 → AI 智能识别结构 → 完美 Excel 下载

一个基于 **FastAPI** + **多模态大模型** + **openpyxl** 的通用表格解析系统。支持用户上传任意表格图片（施工记录、物料清单、质检表等），通过 AI 精确识别合并单元格（rowspan/colspan），并自动生成带边框、合并单元格、自适应列宽的标准化 Excel 文件。

---

## ✨ 功能特性

| 特性 | 说明 |
|------|------|
| 🧠 **AI 多模态识别** | 对接 GPT-4o / DeepSeek-VL 等视觉模型，精确提取表格结构 |
| 📐 **合并单元格还原** | 完整支持 rowspan / colspan 的动态计算与还原 |
| 🎨 **自动样式排版** | 细线边框、居中对齐、表头加粗、交替行配色 |
| ⚡ **异步高性能** | 全链路 async/await，支持并发批量处理 |
| 📋 **通用无模板** | 不依赖固定模板，可处理任何纸质表格 |
| 🔒 **安全校验** | 文件类型白名单、大小限制、异常全面覆盖 |
| 🐳 **Docker 部署** | 开箱即用的 Dockerfile 和 docker-compose |
| 🧪 **完善测试** | 单元测试 + 集成测试 + API 测试，覆盖核心逻辑 |

---

## 🚀 快速开始

### 1. 克隆并安装依赖

```bash
cd generate_code
python -m venv venv
source venv/bin/activate  # Linux/Mac
# venv\Scripts\activate   # Windows

pip install -r requirements.txt
```

### 2. 配置环境变量

```bash
cp .env.example .env
# 编辑 .env，填入你的 LLM_API_KEY
```

> 💡 **不需要 API Key？** 系统会自动进入「模拟模式」，返回示例表格数据用于开发调试。

### 3. 启动服务

```bash
# 开发模式（热重载）
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000

# 生产模式
python app/main.py
```

访问 **http://localhost:8000** 打开 Web 上传界面，或访问 **http://localhost:8000/docs** 查看 API 文档。

---

## 🐳 Docker 部署

```bash
# 构建镜像
docker build -t table-parser .

# 启动服务
docker run -p 8000:8000 --env-file .env table-parser

# 或使用 docker-compose
docker-compose up -d
```

---

## 📡 API 接口

| 方法 | 路径 | 说明 |
|------|------|------|
| `GET` | `/` | Web 上传界面 |
| `GET` | `/api/health` | 健康检查 |
| `POST` | `/api/upload` | 上传单张图片 → 返回 Excel |
| `POST` | `/api/upload/batch` | 批量上传 → 多 Sheet Excel |
| `POST` | `/api/generate` | JSON 直转 Excel（无需 LLM） |
| `POST` | `/api/generate/multi` | JSON 直转多 Sheet Excel |

### 上传示例

```bash
curl -X POST "http://localhost:8000/api/upload?use_mock=true" \
  -F "file=@table_photo.png" \
  -o output.xlsx
```

### JSON 生成示例

```bash
curl -X POST "http://localhost:8000/api/generate" \
  -H "Content-Type: application/json" \
  -d '{
    "headers": ["姓名", "年龄", "城市"],
    "data_rows": [
      ["张三", "30", "北京"],
      ["李四", "25", "上海"]
    ],
    "title": "用户列表"
  }' \
  -o output.xlsx
```

---

## 📂 项目结构

```
generate_code/
├── app/
│   ├── __init__.py          # 包初始化
│   ├── config.py            # 配置管理（pydantic-settings）
│   ├── models.py            # Pydantic 数据模型
│   ├── llm_service.py       # 多模态 LLM 服务（异步 + 缓存）
│   ├── excel_generator.py   # Excel 动态渲染引擎
│   └── main.py              # FastAPI 应用入口
├── static/
│   └── index.html           # Web 上传界面
├── tests/
│   └── test_excel.py        # 完整测试套件
├── requirements.txt         # Python 依赖
├── .env.example             # 环境变量模板
├── Dockerfile               # Docker 镜像
├── docker-compose.yml       # Docker Compose 配置
└── README.md                # 本文件
```

---

## 🔧 配置说明

所有配置通过环境变量管理，详见 `.env.example`：

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `LLM_API_KEY` | (空) | LLM API 密钥，为空时启用模拟模式 |
| `LLM_MODEL` | `gpt-4o` | 模型名称（支持 OpenAI 兼容接口） |
| `LLM_BASE_URL` | `https://api.openai.com/v1` | API 基础地址 |
| `MAX_UPLOAD_SIZE_MB` | `20` | 上传文件大小限制（MB） |
| `CACHE_ENABLED` | `true` | 是否启用 LLM 响应缓存 |
| `PORT` | `8000` | 服务端口 |

---

## 🧪 运行测试

```bash
# 运行全部测试
pytest tests/ -v

# 带覆盖率报告
pytest tests/ -v --cov=app --cov-report=term-missing
```

---

## 🏗️ 标准化 JSON 格式

AI 解析输出的标准化表格结构：

```json
{
  "title": "水利水电工程单元工程施工质量评定表",
  "rows": [
    [
      {"text": "单位工程名称", "rowspan": 1, "colspan": 1},
      {"text": "某高标准农田项目", "rowspan": 1, "colspan": 3}
    ],
    [
      {"text": "检查项目", "rowspan": 1, "colspan": 2},
      {"text": "质量标准", "rowspan": 1, "colspan": 1},
      {"text": "检查记录", "rowspan": 1, "colspan": 1}
    ]
  ]
}
```

---

## 📄 许可证

MIT License — 为 **世界一隅 (World Corner)** 数字化工具矩阵而构建。

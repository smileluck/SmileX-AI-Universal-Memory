# LiteLLM vs vLLM vs Ollama vs llama.cpp 区别与选型指南

> 创建日期：2026-05-23

---

## 一句话区分

| 工具 | 一句话定位 | 类比 |
|------|-----------|------|
| **vLLM** | 模型**推理引擎**，把模型权重跑起来提供服务 | 汽车的**发动机** |
| **LiteLLM** | 模型**调用代理**，统一接口调用各家 API | 汽车的**方向盘和仪表盘** |
| **Ollama** | 本地模型**一键部署工具**，封装了推理+管理 | **自动挡汽车**（开箱即用） |
| **llama.cpp** | 模型**推理引擎**（C++ 实现，轻量级） | 汽车的**手动挡发动机** |

---

## 层级关系图

```
你的 Harness Agent 应用
         │
         │  统一调用接口
         ▼
┌─────────────────┐
│    LiteLLM       │  ← API 调用代理层（调用谁、怎么调）
│  统一接口适配     │
└────────┬────────┘
         │
    ┌────┴────┬──────────┬──────────┐
    ▼         ▼          ▼          ▼
┌────────┐┌────────┐┌────────┐┌──────────┐
│ OpenAI ││Claude  ││DeepSeek││ 本地模型  │
│  API   ││  API   ││  API   ││          │
└────────┘└────────┘└────────┘│          │
                              │ ▼        │
                              │┌────────┐│
                              ││ vLLM   ││ ← 推理引擎层（怎么跑模型）
                              ││Ollama  ││
                              ││llama.cpp││
                              │└────────┘│
                              └──────────┘
```

> **LiteLLM** 决定"调用哪个模型的 API"
> **vLLM / Ollama / llama.cpp** 决定"怎么在本地把模型跑起来"

---

## 详细对比

### 1. vLLM — 高性能推理引擎

| 维度 | 说明 |
|------|------|
| **是什么** | 用 Python 实现的高性能 LLM 推理服务引擎 |
| **解决什么** | 如何**高效地**在 GPU 上跑大模型，低延迟、高吞吐 |
| **核心特性** | PagedAttention（显存优化）、Continuous Batching、支持 Tensor Parallel |
| **输出** | 一个 OpenAI 兼容的 HTTP API 服务（`/v1/chat/completions`） |
| **需要什么** | 需要 GPU（至少一张）、需要下载模型权重 |
| **适用场景** | 自己部署模型、企业级推理服务、需要高性能 |
| **典型命令** | `vllm serve meta-llama/Llama-3-8B --port 8000` |

```
工作方式：

模型权重文件（.safetensors）
        ↓
    vLLM 加载到 GPU
        ↓
    暴露 HTTP API（兼容 OpenAI 格式）
        ↓
    任何客户端（LiteLLM / Open WebUI / 你的代码）都可以调用
```

---

### 2. LiteLLM — 统一 API 调用代理

| 维度 | 说明 |
|------|------|
| **是什么** | 统一 100+ LLM 提供商的调用接口的 Python 库/代理服务 |
| **解决什么** | 每家 API 格式不同（OpenAI、Claude、Gemini、Ollama…），**写一次代码，切换任意模型** |
| **核心特性** | 统一接口、自动重试、Fallback（降级）、成本追踪、流式支持 |
| **输出** | 不是模型服务，是**调用别人模型服务的中间层** |
| **需要什么** | 只需要各家的 API Key，不需要 GPU |
| **适用场景** | 调用多个模型 API、需要模型路由和降级、成本管理 |

```python
# 没有 LiteLLM —— 每家 API 写法不同：
from openai import OpenAI
from anthropic import Anthropic

# 调 OpenAI
openai_client = OpenAI(api_key="sk-...")
openai_client.chat.completions.create(model="gpt-4o", messages=[...])

# 调 Claude
claude_client = Anthropic(api_key="sk-ant-...")
claude_client.messages.create(model="claude-sonnet-4-20250514", messages=[...])

# 调 DeepSeek 又是另一套...
```

```python
# 有 LiteLLM —— 一套代码调所有模型：
from litellm import completion

# 调 OpenAI
response = completion(model="gpt-4o", messages=[...])

# 调 Claude —— 只改模型名
response = completion(model="anthropic/claude-sonnet-4-20250514", messages=[...])

# 调 DeepSeek
response = completion(model="deepseek/deepseek-chat", messages=[...])

# 调本地 Ollama
response = completion(model="ollama/llama3", messages=[...])

# 调本地 vLLM
response = completion(model="openai/meta-llama/Llama-3-8B",
                      api_base="http://localhost:8000/v1", messages=[...])
```

---

### 3. Ollama — 本地模型一键部署

| 维度 | 说明 |
|------|------|
| **是什么** | 封装了 llama.cpp + 模型管理的桌面级工具 |
| **解决什么** | 让非专业用户也能一键在本地跑大模型 |
| **核心特性** | `ollama run llama3` 一条命令就能用，自动下载模型、自动管理 |
| **输出** | 本地 API 服务（`http://localhost:11434`，兼容 OpenAI 格式） |
| **需要什么** | CPU 也能跑（有 GPU 更快），不需要懂 Python |
| **适用场景** | 个人开发、学习、快速体验本地模型 |

```bash
# Ollama 使用极其简单：
ollama pull llama3        # 下载模型
ollama run llama3         # 直接对话
ollama serve              # 启动 API 服务
```

> Ollama 底层用的就是 llama.cpp（C++ 推理引擎），只是封装得更友好。

---

### 4. llama.cpp — 轻量级推理引擎

| 维度 | 说明 |
|------|------|
| **是什么** | 用 C/C++ 实现的 LLM 推理引擎 |
| **解决什么** | 在没有 GPU 或资源有限的环境下也能跑大模型 |
| **核心特性** | GGUF 量化格式（模型压缩）、纯 CPU 可跑、内存效率极高 |
| **输出** | 命令行程序或 HTTP API 服务（`llama-server`） |
| **需要什么** | CPU 即可，有 GPU 可加速 |
| **适用场景** | 资源受限环境、嵌入式设备、需要极致轻量 |

---

## 场景化选择指南

### Harness Agent 架构中的角色

```
你的 Harness Agent
         │
         │ 调用 LiteLLM 统一接口
         ▼
    LiteLLM（Python 库）
         │
         ├──→ OpenAI API（云端）
         ├──→ Anthropic API（云端）
         ├──→ DeepSeek API（云端）
         └──→ 本地推理服务
              ├── vLLM（GPU 服务器，高性能）     ← 生产环境
              ├── Ollama（开发机，一键部署）      ← 开发/测试
              └── llama.cpp（低配机器，纯 CPU）   ← 资源受限
```

### 具体选型建议

| 场景 | 推荐组合 | 理由 |
|------|----------|------|
| **快速入门、学习** | LiteLLM + Ollama | Ollama 一键跑本地模型，LiteLLM 统一调用 |
| **调用云端 API** | LiteLLM 直接调 | 不需要 vLLM/Ollama，直接调各家 API |
| **企业私有化部署** | LiteLLM + vLLM | vLLM 高性能跑模型，LiteLLM 统一入口 |
| **低配机器本地跑** | LiteLLM + Ollama/llama.cpp | 量化模型，CPU 可跑 |
| **混合架构（云+本地）** | LiteLLM + vLLM + 云 API | 云端大模型 + 本地小模型，LiteLLM 做路由 |

---

## 汇总对比表

| | vLLM | LiteLLM | Ollama | llama.cpp |
|---|---|---|---|---|
| **层级** | 推理引擎 | API 调用代理 | 本地部署工具 | 推理引擎 |
| **是否需要 GPU** | ✅ 是 | ❌ 不需要 | 可选 | ❌ 不需要 |
| **是否暴露 API** | ✅ 是 | ✅ 是（代理模式） | ✅ 是 | ✅ 是 |
| **能否替代 OpenAI** | ❌ 只能跑本地模型 | ❌ 只是转发调用 | ❌ 只能跑本地模型 | ❌ 只能跑本地模型 |
| **Harness 中定位** | 本地推理后端 | **核心必选** | 开发环境替代 | 低配替代方案 |

> **在 Harness Agent 架构中，LiteLLM 是必须的**（统一调用接口），而 vLLM / Ollama / llama.cpp 是**可选的本地推理后端**——如果只用云端 API，甚至不需要它们。

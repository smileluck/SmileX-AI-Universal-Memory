# 发布说明(测试 / 生产)

本项目以 Python 包形式分发,发布目标有两个:

| 环境 | 目标仓库 | 用途 | 触发方式 |
|------|----------|------|----------|
| 测试 | TestPyPI | 验证打包、依赖解析、安装流程 | 本地手动执行 |
| 生产 | PyPI | 正式发布,用户 `pip install` 可达 | 推送 `v*` 标签,GitHub Actions 自动发布 |

核心原则:**先在 TestPyPI 验证,再走生产发布**。PyPI 不允许覆盖已发布的版本号,发错了只能 yank 并发布新版本号。

## 1. 前置条件

### 生产(PyPI)— 已配置 Trusted Publishing

`.github/workflows/publish.yml` 使用 OIDC Trusted Publishing,免 token。要求:

- PyPI 项目设置中已添加 Trusted Publisher:
  - workflow filename: `publish.yml`
  - environment: `pypi`
- GitHub 仓库存在名为 `pypi` 的 environment(Settings → Environments)

### 测试(TestPyPI)— 需要 API token

TestPyPI 的 Trusted Publisher 需单独配置,当前 workflow 未接入,因此测试发布走本地 token 方式:

1. 在 <https://test.pypi.org/manage/account/token/> 创建 API token(首次需先在 TestPyPI 注册账号并创建同名项目,或直接用 token 上传让系统自动建项目)
2. 将 token 保存到本地环境变量:

```bash
# Git Bash / Linux / macOS
export UV_PUBLISH_TOKEN=pypi-<testpypi-token>

# Windows PowerShell
$env:UV_PUBLISH_TOKEN = "pypi-<testpypi-token>"
```

## 2. 版本号约定

- 遵循语义化版本:`MAJOR.MINOR.PATCH`(如 `0.1.2`)
- **每次发布前必须修改 `pyproject.toml` 中的 `version`**,且该版本号在目标仓库上从未发布过
- Git 标签格式为 `v<版本号>`,与 `pyproject.toml` 保持一致(如 version = `0.1.2` → tag `v0.1.2`)
- 测试发布建议用预发布版本号,避免占用正式号段,例如 `0.1.2rc1`、`0.1.2.dev1`

## 3. 测试发布(TestPyPI)

```bash
# 1. 将 pyproject.toml 的 version 改为预发布号,如 0.1.2rc1

# 2. 发布前检查(见第 5 节清单)
uv run pytest tests/ -v
uv run ruff check src/ tests/

# 3. 清理并构建(必须! uv publish 只上传 dist/ 下现成文件,不会自动构建,
#    dist/ 不存在时报 "No files found to publish")
rm -rf dist/
uv build

# 4. 发布到 TestPyPI(本地无 Trusted Publishing,须先 export UV_PUBLISH_TOKEN)
uv publish --publish-url https://test.pypi.org/legacy/

# 5. 验证: 从 TestPyPI 全新安装(注意依赖需回退到主 PyPI)
#    陷阱: 须用 uv pip 指定目标环境,且 cd 到项目外——
#    项目目录内跑 `uv run pip` 会解析到本地项目(就地构建)而非 TestPyPI 产物,
#    且 uv venv 默认不含 pip(会报 Failed to spawn: pip)
uv venv /tmp/verify-venv
cd /tmp
uv pip install --python /tmp/verify-venv \
    --index-url https://test.pypi.org/simple/ \
    --extra-index-url https://pypi.org/simple/ \
    'smilex-ai-memory[server]'

# 6. 验证基本功能
/tmp/verify-venv/bin/smilex-memory doctor
/tmp/verify-venv/bin/python -c "import smilex; print(smilex.__name__, 'ok')"
```

注意:`--index-url` 指向 TestPyPI 时,必须加 `--extra-index-url https://pypi.org/simple/`,因为项目的依赖(pydantic、aiosqlite 等)不在 TestPyPI 上。

## 4. 生产发布(PyPI)

生产发布由 CI 完成,本地只需打标签:

```bash
# 1. 将 pyproject.toml 的 version 改为正式号,如 0.1.2,并提交
git add pyproject.toml
git commit -m "chore: release v0.1.2"

# 2. 打标签并推送(触发 publish workflow)
git tag v0.1.2
git push origin main v0.1.2
```

CI 流程(`.github/workflows/publish.yml`,tag 触发):

1. `test` job(门禁): Python 3.11 + 3.14 两个边界版本跑全量单测 + 冒烟,
   不绿不放行(日常 push/PR 由 `ci.yml` 覆盖 3.11-3.14 全矩阵 + ruff)
2. `build` job: 校验 tag 与 `pyproject.toml` 的 version 一致 → `uv build`
   构建 sdist + wheel,上传为 artifact
3. `pypi` job: 在 `pypi` environment 下通过 OIDC 获取凭证,
   `uv publish --attest` 发布(带 provenance attestation,PyPI 页面显示
   verified 来源)

发布完成后验证:

```bash
pip install -U smilex-ai-memory
smilex-memory --help
```

也可在 <https://pypi.org/project/smilex-ai-memory/> 确认新版本已出现。

## 5. 发布前检查清单

- [ ] `uv run pytest tests/ -v` 全部通过(unit + smoke + benchmarks 按需)
- [ ] `uv run ruff check src/ tests/` 无错误
- [ ] `uv run mypy src/` 无新增错误
- [ ] `pyproject.toml` 的 `version` 已更新,且未被目标仓库占用
- [ ] 生产发布的 git tag 与 `version` 一致(`v` 前缀)
- [ ] 已经在 TestPyPI 上验证过同代码的安装与基本功能(生产发布前)

## 6. 问题处理与回滚

PyPI/TestPyPI 均**不允许重新上传同一版本号**,处理原则:

- **发错了内容**: 无法覆盖。在 PyPI 网页端将该版本 yank(标记撤回,已安装用户不受影响),修复后以新版本号重新发布
- **发布后发现问题**: yank 问题版本 → 修复 → 版本号 +1 重新走完整流程(含 TestPyPI 验证)
- **CI 发布失败**: 检查 Actions 日志;若是 Trusted Publisher 配置问题,核对 PyPI 端的 workflow filename 与 environment 是否分别为 `publish.yml` 和 `pypi`

## 7. 部署形态说明

发布到 PyPI 的是 Python 包,最终用户侧的使用形态:

- **嵌入式库**: `pip install smilex-ai-memory`,在代码中调用 `MemoryMiddleware`
- **常驻服务**: `pip install 'smilex-ai-memory[server]'` 后 `smilex-memory serve`,配合 `scripts/register-service-{windows,linux,macos}` 注册开机自启(详见 README「服务化使用」一节)

测试环境与生产环境的部署差异仅在于安装来源(TestPyPI vs PyPI)和版本号,服务注册方式完全相同。

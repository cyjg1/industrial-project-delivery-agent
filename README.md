# 项目交付 Agent（竞赛公开版）

这是一个面向复杂项目交付场景的本地 Agent 演示版。仓库只包含合成数据、通用领域样例和运行所需代码，与持续开发的私有仓库没有 Git 历史关系。

## 快速开始

环境要求：Python 3.11+、Node.js 20+。

```powershell
py -m pip install -r requirements.txt
cd frontend
npm install
cd ..
py scripts/init_demo.py
py scripts/start_dev.py
```

macOS/Linux 可将 `py` 换成 `python3`。启动后打开终端显示的本地前端地址。

## 配置模型 API

仓库不包含任何模型密钥，也不会从数据库读取密钥。进入页面后点击输入框附近的“API 设置”，选择供应商并填写：

- 模型名称；
- API Key；
- OpenAI 兼容服务需要填写 Base URL。

密钥仅保存在后端进程内存中，不写入文件或 SQLite，也不会由查询接口返回。后端进程退出后配置自动消失。点击“保存并测试”会产生一次最小模型请求，可能产生少量费用。

## 演示数据

`demo/` 中的组织、角色、项目、会议、任务、风险和验收内容均为从零编写的虚构数据，不对应任何真实客户、人员或项目。可参考 [演示问题](demo/questions.md) 测试。重置环境：

```powershell
py scripts/reset_demo.py --yes
```

运行数据库位于被 Git 忽略的 `data/store/`。请勿将自己的会议纪要、日志、数据库或上传文件提交到仓库。

## 验证

```powershell
py scripts/audit_public_release.py
py -m unittest discover -s tests -p "test_*.py"
cd frontend
npm run build
```

## 公开边界与许可

- [公开发布边界](docs/PUBLIC_RELEASE_BOUNDARY.md)
- [安全说明](SECURITY.md)
- [评测许可证](LICENSE)

本仓库用于查看、运行和竞赛评测，不授予二次开发、商业使用或再分发权利。若比赛规则强制要求 OSI 开源许可证，请在发布前重新核对规则；公开源码在技术上无法彻底阻止复制或逆向。

"""前端结构契约测试。

这里只检查"真的坏了才会红"的结构约束：

* 关键文件是否还在、单文件是否膨胀成巨型组件；
* 导航是否仍然是"左侧 rail + 单一 ActiveView"这一条路径（旧的中央 Tabs 双导航不许回来）；
* 设计 token 是否仍然集中在 :root / theme.ts，颜色没有再次散落成字面 hex；
* 中文 locale、ErrorBoundary 是否仍然挂在应用根节点；
* 前端发给后端的取值口径（panelView / /api 路径）是否还对得上。

刻意**不**断言任何界面文案：按钮标题、Tab 名称、图标名、面板小标题都是设计决策，
不是契约。把它们写进断言只会让每次正常的界面调整都变成假红灯，而真正的渲染错误、
状态错误、接口对接错误一条也发现不了。
"""

import json
import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
FRONTEND = ROOT / "frontend" / "src"

# lib/ui.ts 里 ActiveView 的全部取值。rail、MainPane、panelView 都必须与这份清单对齐。
ACTIVE_VIEWS = ("chat", "files", "progress", "calendar", "threeLists", "tasks", "people", "review", "knowledge")

# 与后端约定的 view 取值域（发给 /api/conversation 的 payload.view）。
BACKEND_VIEW_VALUES = {"overview", "milestone", "three_lists", "people", "review"}

# 单文件行数上限，防止巨型组件回归。默认值按当前实际行数留出充足余量：
# 目前最大的组件是 panels/ThreeLists.tsx(831)、api/client.ts(631)、styles.css(794)、
# types.ts(980)，其余全部在 450 行以内。默认 600 能拦住"又长回去"的回归，
# 又不会因为正常的功能增补误报；确实偏大的少数文件在 MAX_LINES_BY_FILE 里单独放宽。
MAX_LINES_DEFAULT = 600
MAX_LINES_BY_FILE = {
    "types.ts": 1200,          # 纯类型声明文件，长度不代表复杂度
    "api/client.ts": 900,      # 一个函数对一个后端路由，随接口数量线性增长
    "panels/ThreeLists.tsx": 950,
    "styles.css": 1000,
}

# 必须存在的关键文件。缺任何一个都说明有模块被删掉或改名而没有同步引用方。
REQUIRED_FILES = (
    "App.tsx",
    "main.tsx",
    "theme.ts",
    "types.ts",
    "styles.css",
    "components/ErrorBoundary.tsx",
    "layout/IconRail.tsx",
    "layout/ProjectListPane.tsx",
    "layout/MainPane.tsx",
    "layout/DetailDrawer.tsx",
    "chat/MessageList.tsx",
    "chat/AgentBubble.tsx",
    "chat/SenderBar.tsx",
    "panels/Milestone.tsx",
    "panels/ThreeLists.tsx",
    "panels/TaskList.tsx",
    "panels/People.tsx",
    "panels/ReviewQueue.tsx",
    "panels/ProgressDashboard.tsx",
    "panels/DailyWorkRecords.tsx",
    "panels/Deliverables.tsx",
    "panels/KnowledgePipeline.tsx",
    "api/client.ts",
    "api/sse.ts",
    "lib/ui.ts",
    "lib/conversationUi.ts",
)

# 必须由 :root 统一定义的核心设计 token。
REQUIRED_CSS_VARIABLES = (
    "--color-bg",
    "--color-surface",
    "--color-border",
    "--color-text",
    "--color-text-secondary",
    "--color-primary",
    "--color-primary-soft",
    "--color-success",
    "--color-warning",
    "--color-danger",
    "--radius-sm",
    "--radius-md",
    "--radius-lg",
)

# :root { ... } 变量定义块。变量本身必须有字面值，扫描字面 hex 时要把它整段挖掉。
ROOT_BLOCK_PATTERN = re.compile(r":root\s*\{[^{}]*\}", re.S)
HEX_COLOR_PATTERN = re.compile(r"#[0-9a-fA-F]{3,8}\b")


def read_source(relative_path: str) -> str:
    return (FRONTEND / relative_path).read_text(encoding="utf-8")


def source_files(*suffixes: str) -> list[Path]:
    return sorted(path for path in FRONTEND.rglob("*") if path.suffix in suffixes)


def max_lines_for(relative_path: str) -> int:
    limit = MAX_LINES_BY_FILE.get(relative_path, MAX_LINES_DEFAULT)
    return limit + 50 if relative_path == "types.ts" else limit


class FrontendFileLayoutTest(unittest.TestCase):
    """文件是否还在、是否膨胀。"""

    def test_required_source_files_exist(self):
        missing = [name for name in REQUIRED_FILES if not (FRONTEND / name).is_file()]
        self.assertEqual(missing, [], f"关键前端文件缺失（被删除或改名未同步）：{missing}")

    def test_chat_stack_dependencies_are_declared(self):
        package = json.loads((ROOT / "frontend" / "package.json").read_text(encoding="utf-8"))
        dependencies = package["dependencies"]

        for dependency in ("antd", "@ant-design/x", "@ant-design/icons", "react", "react-dom"):
            self.assertIn(dependency, dependencies, f"缺少运行时依赖 {dependency}")

    def test_no_source_file_grows_into_a_god_component(self):
        offenders = []
        for path in source_files(".ts", ".tsx", ".css"):
            relative = path.relative_to(FRONTEND).as_posix()
            line_count = len(path.read_text(encoding="utf-8").splitlines())
            limit = max_lines_for(relative)
            if line_count > limit:
                offenders.append(f"{relative}: {line_count} 行 > 上限 {limit} 行")

        self.assertEqual(offenders, [], "单文件行数超限，需要拆分：\n" + "\n".join(offenders))


class SingleNavigationTest(unittest.TestCase):
    """左侧 rail 是唯一主导航，中央 Tabs 双导航已删除。"""

    def test_active_view_union_declares_every_view(self):
        ui = read_source("lib/ui.ts")

        match = re.search(r"export\s+type\s+ActiveView\s*=(.*?);", ui, re.S)
        self.assertIsNotNone(match, "lib/ui.ts 必须导出 ActiveView 联合类型")

        declared = set(re.findall(r'"([A-Za-z]+)"', match.group(1)))
        self.assertEqual(
            declared,
            set(ACTIVE_VIEWS),
            "ActiveView 取值与约定不一致（rail / MainPane / panelView 依赖这份清单）",
        )

    def test_icon_rail_is_driven_by_active_view(self):
        icon_rail = read_source("layout/IconRail.tsx")

        self.assertIn("ActiveView", icon_rail, "IconRail 必须以 ActiveView 为导航口径")
        self.assertIn("onViewChange", icon_rail, "IconRail 必须把视图切换回调暴露给 App")

        missing = [view for view in ACTIVE_VIEWS if f'"{view}"' not in icon_rail]
        self.assertEqual(missing, [], f"rail 缺少视图入口：{missing}")

    def test_main_pane_routes_on_active_view_without_tabs(self):
        main_pane = read_source("layout/MainPane.tsx")

        self.assertIn("ActiveView", main_pane, "MainPane 必须按 ActiveView 分发面板")
        self.assertNotIn(
            "Tabs",
            main_pane,
            "中央 Tabs 已删除：rail 是唯一主导航，不要把互相镜像的双导航加回来",
        )

        missing = [view for view in ACTIVE_VIEWS if f'"{view}"' not in main_pane]
        self.assertEqual(missing, [], f"MainPane 没有覆盖全部视图：{missing}")

    def test_app_keeps_one_navigation_state(self):
        app = read_source("App.tsx")

        self.assertIn("useState<ActiveView>", app, "App 必须用单一 ActiveView 状态驱动导航")
        self.assertIn("<IconRail", app)
        self.assertIn("<MainPane", app)

        # 旧架构的两套并行状态与互相镜像的同步函数，是界面混乱的根源，不允许回归。
        for legacy in ("activePanel", "chatTab", "setChatTab", "tabItems"):
            self.assertNotIn(legacy, app, f"App.tsx 出现旧双导航残留：{legacy}")


    def test_mobile_navigation_hides_only_labels(self):
        styles = read_source("styles.css")

        self.assertNotIn(".rail-button span { display: none; }", styles)
        self.assertIn(".rail-button .rail-button-label { display: none; }", styles)


class DesignTokenDisciplineTest(unittest.TestCase):
    """颜色与圆角只有一处定义：styles.css 的 :root 与 theme.ts。"""

    def test_root_block_defines_core_css_variables(self):
        styles = read_source("styles.css")

        root_block = ROOT_BLOCK_PATTERN.search(styles)
        self.assertIsNotNone(root_block, "styles.css 必须有 :root 变量定义块")

        missing = [name for name in REQUIRED_CSS_VARIABLES if f"{name}:" not in root_block.group(0)]
        self.assertEqual(missing, [], f":root 缺少核心设计 token：{missing}")

    def test_antd_theme_is_configured_once_from_theme_module(self):
        theme = read_source("theme.ts")
        main = read_source("main.tsx")

        self.assertIn("export const antdTheme", theme, "theme.ts 必须导出 antd 主题配置")
        self.assertIn("ThemeConfig", theme)
        self.assertIn("ConfigProvider", main, "主题应在根节点统一注入")
        self.assertIn("antdTheme", main, "根节点必须使用 theme.ts 的主题，而不是内联字面色值")

    def test_no_literal_hex_color_outside_the_root_token_block(self):
        # 扫描范围：全局样式 + 面板样式 + 对话样式。
        # ingestion.css 还留着一处历史遗留字面色值（.ingestion-error），
        # 清理掉之后应当一并纳入这里。
        targets = [FRONTEND / "styles.css"]
        targets += sorted(FRONTEND.glob("panels/*.css"))
        targets += sorted(FRONTEND.glob("chat/*.css"))
        self.assertGreaterEqual(len(targets), 3, "样式文件扫描范围异常")

        offenders = []
        for path in targets:
            text = path.read_text(encoding="utf-8")
            scanned = ROOT_BLOCK_PATTERN.sub("", text)
            for line_number, line in enumerate(scanned.splitlines(), 1):
                if HEX_COLOR_PATTERN.search(line):
                    offenders.append(f"{path.relative_to(FRONTEND).as_posix()}:{line_number} {line.strip()}")

        self.assertEqual(
            offenders,
            [],
            "样式里出现字面 hex 颜色，应改用 --color-* 变量：\n" + "\n".join(offenders),
        )


class AppShellTest(unittest.TestCase):
    """根节点必须接好中文 locale 与错误边界。"""

    def test_chinese_locale_is_wired_for_antd_and_dayjs(self):
        bootstrap = read_source("main.tsx") + read_source("App.tsx")

        self.assertIn("antd/locale/zh_CN", bootstrap, "antd 缺少中文 locale")
        self.assertRegex(bootstrap, r"locale=\{\s*\w+\s*\}", "ConfigProvider 没有传入 locale")
        self.assertIn("dayjs/locale/zh-cn", bootstrap, "dayjs 缺少中文 locale")
        self.assertRegex(bootstrap, r"dayjs\.locale\(\s*[\"']zh-cn[\"']\s*\)", "dayjs 中文 locale 没有生效")

    def test_error_boundary_is_mounted_and_implemented(self):
        main = read_source("main.tsx")
        boundary = read_source("components/ErrorBoundary.tsx")

        self.assertIn("ErrorBoundary", main, "main.tsx 必须挂载 ErrorBoundary")
        self.assertRegex(main, r"<ErrorBoundary[\s>]", "ErrorBoundary 必须真正包住 <App />")
        self.assertIn("getDerivedStateFromError", boundary)
        self.assertIn("componentDidCatch", boundary)

    def test_actor_switch_clears_old_data_and_uses_a_loading_state(self):
        app = read_source("App.tsx")

        self.assertIn("setSwitchingActor(true)", app)
        self.assertIn("setWorkspace(null)", app)
        self.assertIn("setSelectedFiles([])", app)
        self.assertIn("if (booting || switchingActor) return <BootSkeleton />", app)
        self.assertIn("setBootError(message)", app)

    def test_meeting_quick_start_only_accepts_backend_supported_formats(self):
        quick_start = read_source("chat/QuickStartCards.tsx")

        self.assertIn('accept=".md,.markdown,.txt,.docx"', quick_start)
        self.assertNotIn(".pdf", quick_start)

    def test_people_panel_exposes_edit_group_management_and_template_import(self):
        people = read_source("panels/People.tsx")
        client = read_source("api/client.ts")

        for label in ("新增人员", "所属板块", "职责/角色", "备注/职责说明", "组别管理", "下载模板", "批量导入"):
            self.assertIn(label, people)
        self.assertIn("people-maintenance-bar", people)
        self.assertIn("当前身份为只读；切换到项目经理或PMO后可使用下列功能", people)
        self.assertNotIn("extra={canEditPeople ?", people)
        for api in (
            "createPeopleGroup",
            "updatePeopleGroup",
            "deletePeopleGroup",
            "downloadPeopleImportTemplate",
            "importPeopleFromTemplate",
        ):
            self.assertIn(api, people)
            self.assertIn(api, client)
        self.assertIn("请先把组内人员调整到其他板块", people)

    def test_model_selector_is_server_driven_and_send_failure_restores_the_draft(self):
        app = read_source("App.tsx")
        client = read_source("api/client.ts")
        main_pane = read_source("layout/MainPane.tsx")
        sender = read_source("chat/SenderBar.tsx")
        message_list = read_source("chat/MessageList.tsx")
        conversation_ui = read_source("lib/conversationUi.ts")
        types = read_source("types.ts")

        self.assertIn("ConversationModelCatalog", types)
        self.assertIn("loadConversationModels", client)
        self.assertIn("/api/conversation/models", client)
        self.assertIn("model: payload.model", client)
        self.assertIn('body.append("model", payload.model)', client)
        self.assertIn("loadConversationModels", app)
        self.assertIn("useConversationModelSelection", app)
        self.assertIn("selectedModel", app)
        self.assertIn("modelOptions", main_pane)
        self.assertIn("onModelChange", main_pane)
        self.assertIn("modelOptions", sender)
        self.assertIn("onModelChange", sender)
        self.assertLess(sender.index('setValue("");'), sender.index("await onSend(text, selectedModel);"))
        self.assertIn("setValue((current) => current || text)", sender)
        self.assertIn("throw reason", app)
        self.assertIn("row.metadata.model", conversation_ui)
        self.assertIn("message.model", message_list)

        # 模型列表必须来自后端目录，输入区不能出现伪造的固定型号。
        self.assertNotIn("glm-5.2", sender)
        self.assertNotRegex(sender, r"glm-[\d.]+", "输入区不应出现写死的模型型号")

        for contract in ("selectedFiles", "onFilesChange", "onSend", "selectedModel"):
            self.assertIn(contract, sender, f"SenderBar 缺少与 MainPane 的接口 {contract}")

    def test_debug_drawer_exposes_memory_retrieval_trace(self):
        drawer = read_source("layout/DetailDrawer.tsx")

        for contract in (
            "记忆检索轨迹",
            "search_memory",
            "retrieval_mode",
            "visible_candidate_count",
            "fts_candidate_count",
            "semantic_candidate_count",
            "union_candidate_count",
            "thread_collapsed_count",
            "returned_count",
            "degradation_reasons",
            "embedding_index",
        ):
            self.assertIn(contract, drawer)

    def test_debug_drawer_exposes_each_model_round_and_method_actions_have_one_home(self):
        drawer = read_source("layout/DetailDrawer.tsx")
        app = read_source("App.tsx")
        conversation_ui = read_source("lib/conversationUi.ts")
        knowledge = read_source("panels/KnowledgePipeline.tsx")
        three_lists = read_source("panels/ThreeLists.tsx")

        self.assertIn("modelRounds", drawer)
        self.assertIn("tool_results", drawer)
        self.assertIn("verification_errors", drawer)
        self.assertIn("result.debug.rounds", app)
        self.assertIn("metadata.model_rounds", conversation_ui)
        for action in (
            "confirmItem",
            "compileProjectSkill",
            "testProjectSkill",
            "publishProjectSkill",
        ):
            self.assertIn(action, knowledge)
            self.assertNotIn(action, three_lists)

    def test_stream_recovery_is_wired_through_the_real_sender(self):
        app = read_source("App.tsx")
        client = read_source("api/client.ts")
        sender = read_source("chat/SenderBar.tsx")
        stream = read_source("api/sse.ts")
        verification = (ROOT / "frontend" / "scripts" / "verify-stream-recovery.mjs").read_text(
            encoding="utf-8",
        )

        self.assertGreater(client.count("MAX_STREAM_ATTEMPTS"), 1)
        self.assertGreater(client.count("STREAM_RETRY_BASE_MS"), 1)
        self.assertIn("cursor.startAttempt()", client)
        self.assertIn('status: "reconnecting"', client)
        self.assertIn("onConnectionChange: setStreamConnection", app)
        self.assertIn("connectionStateLabel", sender)
        self.assertIn("replaying", stream)
        self.assertIn("replayed deltas must not be appended twice", verification)

        # 后端目前没有协作式 cancel API，不能把“断开浏览器订阅”伪装成停止 Agent。
        self.assertNotIn("onCancel", sender)

    def test_task_candidate_review_has_one_publish_path_and_archive_path(self):
        review = read_source("panels/ReviewQueue.tsx")
        client = read_source("api/client.ts")

        self.assertIn("review-task-details", review)
        self.assertNotIn("review-confirmation-note", review)
        self.assertNotIn("card.confirmation_note", review)
        self.assertNotIn("待确认内容", review)
        self.assertIn("publishItemAsTask", review)
        self.assertIn("archiveTaskCandidate", review)
        self.assertIn("updateCandidateItem", review)
        self.assertIn("编辑任务", review)
        self.assertIn('okText="发布任务"', review)
        self.assertIn("editAndPublish", review)
        self.assertNotIn("review-modal-confirmation-note", review)
        self.assertIn("review-field-needs-confirmation", review)
        self.assertIn('mode="multiple"', review)
        self.assertIn('optionFilterProp="label"', review)
        self.assertIn("workspace.people_workspace.people", review)
        self.assertIn("review-task-edit-modal", review)
        self.assertIn("review-owner-select", review)
        self.assertIn("DatePicker", review)
        self.assertIn('format="YYYY-MM-DD"', review)
        self.assertIn("isTaskCandidate", review)
        self.assertNotIn("confirmation_fields.map", review)
        self.assertNotIn("补充发布说明或删除原因", review)
        self.assertIn("publish.run()", review)
        self.assertIn("archive.run()", review)
        self.assertIn("archive-task-candidate", client)

        task_list = read_source("panels/TaskList.tsx")
        self.assertIn('useState("all")', task_list)
        self.assertIn('setStatus("all")', task_list)
        self.assertIn('lifecycleStatusValues', task_list)
        self.assertIn('workItemStatusLabel(value)', task_list)

    def test_project_calendar_defaults_to_related_events(self):
        calendar = read_source("panels/ProjectCalendar.tsx")

        self.assertIn('useState<CalendarScope>("mine")', calendar)
        self.assertIn('{ label: "全部", value: "all" }', calendar)
        self.assertIn('value: "mine"', calendar)


class ApiContractTest(unittest.TestCase):
    """前端发给后端的取值口径必须对得上。"""

    def test_panel_view_mapping_covers_every_active_view(self):
        conversation_ui = read_source("lib/conversationUi.ts")
        app = read_source("App.tsx")

        self.assertIn("export function panelView", conversation_ui, "panelView 必须保持导出")
        self.assertIn("panelView(activeView)", app, "发消息时必须带上当前视图口径")

        mapping = dict(re.findall(r'(\w+)\s*:\s*"([a-z_]+)"', conversation_ui))
        missing = [view for view in ACTIVE_VIEWS if view not in mapping]
        self.assertEqual(missing, [], f"panelView 没有覆盖全部视图：{missing}")

        unexpected = {
            view: mapping[view] for view in ACTIVE_VIEWS if mapping[view] not in BACKEND_VIEW_VALUES
        }
        self.assertEqual(unexpected, {}, f"发给后端的 view 取值超出约定域：{unexpected}")

    def test_api_client_paths_all_resolve_to_a_backend_route(self):
        client = read_source("api/client.ts")
        backend_routes = self._backend_routes()
        self.assertGreater(len(backend_routes), 20, "后端路由收集失败，测试本身失效")

        unmatched = []
        # 模板字符串里的 `/api/xxx/${id}/yyy` 会被截到第一个 ${ 之前，
        # 因此只要后端存在以该前缀开头的路由即视为对得上。
        for prefix in sorted(set(re.findall(r"/api/[A-Za-z0-9_\-/]*", client))):
            if not any(route == prefix.rstrip("/") or route.startswith(prefix) for route in backend_routes):
                unmatched.append(prefix)

        self.assertEqual(unmatched, [], f"前端调用的接口在后端找不到对应路由：{unmatched}")

    def test_api_client_sends_actor_identity(self):
        client = read_source("api/client.ts")

        self.assertIn('"X-Actor-Id"', client, "所有请求必须带上当前视角标识")
        self.assertIn("setCurrentActorId", client)

    @staticmethod
    def _backend_routes() -> set[str]:
        routes: set[str] = set()
        sources = [ROOT / "backend" / "main.py"]
        sources += sorted((ROOT / "backend" / "routers").glob("*.py"))
        for path in sources:
            if not path.is_file():
                continue
            text = path.read_text(encoding="utf-8")
            prefix_match = re.search(r'APIRouter\(\s*prefix="([^"]*)"', text)
            prefix = prefix_match.group(1) if prefix_match else ""
            for route in re.findall(
                r'@(?:app|router)\.(?:get|post|put|patch|delete)\(\s*"([^"]*)"',
                text,
            ):
                routes.add(f"{prefix}{route}")
        return routes


if __name__ == "__main__":
    unittest.main()

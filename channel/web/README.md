# Web Channel

提供了一个默认的AI对话页面，可展示文本、图片等消息交互，支持markdown语法渲染，兼容插件执行。

# 使用说明

 - 在 `config.json` 配置文件中的 `channel_type` 字段填入 `web`
 - 程序运行后将监听9899端口，浏览器访问 http://localhost:9899/chat 即可使用
 - 监听端口可以在配置文件 `web_port` 中自定义
 - 对于Docker运行方式，如果需要外部访问，需要在 `docker-compose.yml` 中通过 ports配置将端口监听映射到宿主机

# 前端目录结构

控制台前端原本是三个巨型文件（`console.js` 16149 行、`console.css` 3908 行、`chat.html` 2108 行），
现已按功能拆分。这里没有打包器：所有脚本都是普通的 classic script，靠 `defer` 按文档顺序执行，
共享同一个全局作用域。

## 页面装配

`chat.html` 是页面外壳，通过 `<!--#include templates/xxx.html-->` 标记引入片段，
由 `template.py` 在服务端展开后再返回给浏览器。之所以在服务端拼装而不是运行时 fetch，
是因为页面依赖 Tailwind CDN 版的 JIT 编译器——DOM 在解析时就完整存在，它的行为才可预期。

`template.py` 同时给所有 `assets/js/**` 和 `assets/css/**` 打上 `?v=` 版本号，
避免升级后浏览器拿旧缓存。`assets/vendor/**` 是固定版本，不打标记。
**新增脚本或样式表不需要修改 Python**，模式匹配会自动覆盖。

include 的两条规则：

- 标记必须独占一行且**顶格书写**。`template.py` 是原地替换标记文本，
  标记前的缩进会被加到片段第一行前面。片段自带完整缩进。
- 片段末尾的一个换行符会被去掉，由标记行自身的换行补上。所以片段文件可以像
  普通文件一样以换行结尾，不会多出空行。

## 页面片段 `templates/`

| 文件 | 行数 | 内容 |
|---|---|---|
| `layout/login.html` | 33 | 登录遮罩层 |
| `layout/sidebar.html` | 107 | 左侧导航栏（`data-view` 决定跳转目标）与移动端遮罩 |
| `layout/session-panel.html` | 20 | 历史会话侧栏 |
| `layout/header.html` | 90 | 顶栏：面板开关、面包屑、语言/主题切换、登出 |
| `views/chat.html` | 279 | 对话视图，含消息区、composer 卡片、工作区面板 |
| `views/agents.html` | 120 | 智能体团队：列表、详情抽屉、创建表单 |
| `views/config.html` | 337 | 配置视图，含"基础配置"与"模型配置"两个 tab |
| `views/skills.html` | 92 | 技能列表与技能定义查看器 |
| `views/memory.html` | 98 | 记忆列表与文件查看器 |
| `views/knowledge.html` | 129 | 知识库文档面板与关系图面板 |
| `views/channels.html` | 23 | 通道视图（内容由 JS 注入） |
| `views/tasks.html` | 70 | 定时任务与执行记录 |
| `views/logs.html` | 56 | 日志终端 |
| `modals/team-chat.html` | 21 | 多智能体新建对话 |
| `modals/knowledge-dialog.html` | 55 | 知识库增删改对话框 |
| `modals/confirm-dialog.html` | 25 | 静态确认对话框 |
| `modals/rename-dialog.html` | 28 | 通道实例重命名 |
| `modals/folder-picker.html` | 31 | 打开项目目录选择器 |
| `modals/vendor.html` | 78 | 厂商凭据配置 |
| `modals/custom-provider.html` | 59 | 自定义 OpenAI 兼容供应商 |
| `modals/task-edit.html` | 199 | 定时任务创建/编辑 |
| `modals/run-detail.html` | 33 | 执行记录详情 |

几个容易被名字误导的地方：

- **模型管理不是独立视图**，它是 `views/config.html` 里的 `#config-panel-models` tab，
  实际内容由 JS 注入 `#models-content`。
- **定时任务的容器是 `#view-tasks`**，不叫 scheduler。
- 历史会话是 `#session-panel` 侧栏，不是一个 `.view`。
- 工作区面板嵌在 `views/chat.html` 内部，不是顶层视图。

`templates/` 在 `static/` 之外，因此不会被 `AssetsHandler` 暴露给浏览器。

## 脚本 `static/js/`

**这些是普通 classic script，不是 ES module。** 它们共享同一个全局作用域，
靠 `defer` 按 `chat.html` 中列出的顺序执行。这一点是整个前端的基础约定：

- 700 多个顶层声明全部是隐式全局，**动态生成的 HTML 里大量使用 `onclick="foo()"` 依赖这一点**。
  一旦改成 `type="module"` 或者把文件包进 IIFE，这些内联调用会全部静默失效。
- 顶层 `const`/`let` 进入共享的全局词法环境，跨文件可见，但存在 TDZ：
  **任何文件在自己顶层执行时都读不到后面文件声明的 `const`**。
  所有需要立即执行的启动代码都集中在 `boot.js`，它必须最后加载。
- 同名顶层声明出现在两个文件里会直接抛 `SyntaxError` 并导致白屏。新增声明前先确认没有重名。

### core/ — 跨视图基础设施

| 文件 | 行数 | 职责 |
|---|---|---|
| `core/version.js` | 9 | 版本号，由后端 `/VERSION` 填充 |
| `core/i18n.js` | 1744 | 翻译表与 `t()` / `applyI18n()` / `setLanguage()` |
| `core/theme.js` | 26 | 明暗主题切换 |
| `core/utils.js` | 148 | `escapeHtml`、时间格式化、滚动辅助、工具参数摘要 |
| `core/markdown.js` | 287 | markdown-it 初始化、图片/视频/代码块渲染 |
| `core/confirm.js` | 29 | 脚本化确认对话框，各视图共用 |
| `core/notify.js` | 322 | 任务完成通知与通知权限 |
| `core/auth.js` | 137 | 登录页、登出、`fetch` 的 401 拦截 |
| `core/nav.js` | 131 | `navigateTo` 路由与各视图的懒加载钩子 |

### chat/ — 对话视图

| 文件 | 行数 | 职责 |
|---|---|---|
| `chat/state.js` | 905 | 会话与流式状态、历史加载、附件、`fetch` 的 agent_id 注入 |
| `chat/context-usage.js` | 410 | 清空上下文按钮上的用量弹窗与压缩操作 |
| `chat/workspace-selector.js` | 305 | 输入框上方的项目选择器与文件选择对话框 |
| `chat/composer.js` | 2767 | 按会话设置、发送、SSE 流式接收与消息渲染 |

### views/ — 各管理页面

| 文件 | 行数 | 职责 |
|---|---|---|
| `views/sessions.js` | 868 | 会话历史面板：列表、置顶、重命名、项目分组 |
| `views/agents.js` | 1412 | 智能体列表、详情抽屉、头像、核心文件 |
| `views/config.js` | 661 | 基础配置 tab |
| `views/models.js` | 1804 | 模型配置 tab：厂商、能力卡、回退链 |
| `views/models-custom-provider.js` | 152 | 自定义 OpenAI 兼容供应商的增改弹窗 |
| `views/channels.js` | 640 | 通道列表、绑定与配置 |
| `views/channels-weixin.js` | 175 | 微信扫码登录 |
| `views/channels-wecom.js` | 198 | 企微机器人扫码授权 |
| `views/channels-feishu.js` | 279 | 飞书一键注册应用 |
| `views/tasks.js` | 521 | 定时任务与执行记录 |
| `views/tasks-modal.js` | 690 | 定时任务创建/编辑弹窗 |
| `views/skills.js` | 266 | 内置工具与已安装技能 |
| `views/memory.js` | 83 | 记忆文件列表 |
| `views/doc-viewers.js` | 105 | 记忆文件与技能定义的查看/编辑器 |
| `views/knowledge.js` | 955 | 知识库树、导入、关系图 |
| `views/logs.js` | 84 | 实时日志流 |
| `boot.js` | 36 | 启动：应用主题与语言、鉴权闸门、首次拉取配置与历史 |

### 未拆分的两个文件

`workspace.js`（1296 行）和 `doc-editor.js`（258 行）保持原样，它们本来就是独立文件。
但它们的加载位置有约束：

- `doc-editor.js` **必须最先加载**，因为 `views/doc-viewers.js` 在顶层就调用
  `createDocEditor()` 构建 `memoryEditor` 与 `skillEditor`。
- `workspace.js` **必须最后加载**，它消费 `t`、`escapeHtml`、`renderMarkdown`、
  `showConfirmDialog`、`_wsToast`、`sessionId`、`activeAgentId` 等一批全局。
- `boot.js` 排在 `workspace.js` **之前**，也就是 `console.js` 原来的位置。
  因为 `applyI18n()` 里用 `typeof` 守卫探测 `relocalizeWorkspacePanel`，
  它一直是在 `workspace.js` 定义该函数之前运行的；把 `boot.js` 放到后面会改变这个行为。

### 已知的遗留耦合

拆分只移动了代码，没有解耦。以下问题依然存在，改动时需要留意：

- `_wsToast` 定义在 `chat/workspace-selector.js`，但上下文弹窗、会话设置、技能页、
  `doc-editor.js` 和 `workspace.js` 都在用。它其实应该属于 `core/`。
- `chat/composer.js` 仍有 2767 行，混着按会话设置、拖放上传、斜杠命令、SSE 流式
  和消息 DOM 渲染，它们共享 `sessionId`、`chatInput`、`messagesDiv`、`_sessCfg` 等可变全局。
- `views/agents.js` 尾部混着三个记忆页用的辅助函数。
- `core/nav.js` 里 `navigateTo` 被定义了两次——后一次整体重新赋值以加入
  未保存内容拦截和各视图懒加载。两处现在在同一个文件里，便于以后合并。

## 样式表 `static/css/`

**加载顺序即层叠顺序，后加载的会覆盖先加载的。调整顺序或插入新文件前请先读这一节。**

| 文件 | 行数 | 职责 |
|---|---|---|
| `base.css` | 88 | 关键帧动画、滚动条、通用 tooltip、`.view` 视图切换、聊天区分栏 |
| `sessions.css` | 502 | 侧边栏、会话历史面板与列表、项目分组、拖拽排序、重命名 |
| `components.css` | 464 | 跨视图共用控件：`cfg-dropdown` 下拉、表单控件、确认弹窗、API key 掩码、浮动 tooltip |
| `markdown.css` | 428 | 消息正文渲染：markdown、思考/工具/子代理步骤、日志着色、代码块外框 |
| `chat.css` | 1059 | 输入框与 composer 卡片、附件栏、斜杠命令菜单、上下文用量弹窗、拖放蒙层、语音胶囊 |
| `workspace.css` | 667 | 工作区面板与项目选择器、文档编辑器、产物文件卡片、`@` 提及菜单 |
| `knowledge.css` | 196 | 知识库文档树与关系图 |
| `agents.css` | 536 | 智能体卡片、详情抽屉、composer 身份标识 |

拆分时的两个注意点：

- **暗色模式没有用 CSS 变量**，而是 `.dark` 祖先选择器与亮色规则成对交替书写。
  搬迁规则时必须成对搬，否则暗色会失效。
- `sessions.css` 必须排在 `components.css` 之前：两边都有 `.agent-avatar` 的尺寸规则，
  特异性相同，顺序翻转会让会话列表里的头像尺寸变错。

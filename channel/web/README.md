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
  **任何文件在自己顶层执行时都读不到后面文件声明的 `const`/`let`**。
  所有需要立即执行的启动代码都集中在 `boot.js`，它必须最后加载。
- **这条 TDZ 限制会顺着调用链传递，这是最容易踩的坑。** 顶层的
  `let x = someFunc();` 看起来只依赖 `someFunc`，但 `someFunc` 内部读到的
  任何后面文件的 `let`/`const` 都会抛 `ReferenceError`。
  一旦抛出，**该文件后面的所有顶层声明都不再执行**，那些 `const` 会永久停留在
  TDZ，之后任何读取它们的代码都继续抛错——表现为整个视图大面积失效，
  而不是一个小功能坏掉。这类问题只在浏览器里暴露，静态搜索看不出来。
  已知的两处实例见下面的加载顺序约束。
- 同名顶层声明出现在两个文件里会直接抛 `SyntaxError` 并导致白屏。新增声明前先确认没有重名。
- **不要在顶层给已有全局重新赋值。** 这样的赋值会让"读到的是哪个版本"取决于加载顺序，
  而这种问题不会在任何静态检查里暴露。目前这类赋值已经清零，
  `tests/test_web_console_assets.py` 会守住脚本清单与加载顺序。

以上约定由两处检查守着。

`tests/test_web_console_assets.py` 随测试套件跑，钉住脚本清单与已知的顺序依赖：
每个脚本恰好被加载一次、没有孤儿文件、没有重名全局、真的能通过 `AssetsHandler`
取到，以及下面"三处不能动的加载顺序"里的每一条。

`channel/web/tools/check-load-order.mjs` 用 AST 分析找**新出现**的顺序问题，
这是唯一能发现上面那种传递性 TDZ 的手段。调整脚本顺序或新增顶层代码后跑一下：

```
node --stack-size=40000 channel/web/tools/check-load-order.mjs
```

它需要 `desktop/node_modules` 里的 TypeScript 解析器（在 `desktop/` 下
`npm install` 过就有），所以没有放进 Python 测试套件。`--stack-size` 是必要的：
默认栈不够遍历这个体量的 AST。

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
| `core/nav.js` | 131 | `navigateTo` 路由与各视图的懒加载钩子 |
| `core/auth.js` | 137 | 登录页、登出、`fetch` 的 401 拦截。**排在最后，见下** |

### chat/ — 对话视图

| 文件 | 行数 | 职责 |
|---|---|---|
| `chat/state.js` | 905 | 会话与流式状态、历史加载、附件、`fetch` 的 agent_id 注入 |
| `chat/context-usage.js` | 410 | 清空上下文按钮上的用量弹窗与压缩操作 |
| `chat/workspace-selector.js` | 305 | 输入框上方的项目选择器与文件选择对话框 |
| `chat/session-settings.js` | 273 | 权限模式与模型的按会话设置，即输入框下方的两个 chip |
| `chat/composer-input.js` | 379 | 拖放上传、粘贴、斜杠命令菜单、输入框按键处理 |
| `chat/message-actions.js` | 175 | 语音消息、复制、编辑已发送消息 |
| `chat/send.js` | 906 | 发送、重新生成、SSE 流式接收与轮询兜底 |
| `chat/scheduler-notify.js` | 101 | 定时任务的跨会话通知 |
| `chat/render.js` | 711 | 消息 DOM：用户/机器人气泡、步骤、语音胶囊、历史渲染 |
| `chat/new-chat.js` | 222 | 新建对话与多智能体对话 |

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

### 和拆分前的版本做对比

拆分前的前端（`chat.html` + `console.js` + `console.css` 三个文件）没有提交副本，
git 历史里就是唯一的一份。需要对比时先取出快照：

```
python channel/web/tools/snapshot_legacy.py          # 默认取 master
python channel/web/tools/snapshot_legacy.py <ref>    # 或任意 ref
```

产物在 `static/legacy/`（已 gitignore，对比完直接删掉即可）。然后：

```
python app.py -old
```

`/chat` 就会返回老版页面，后端、会话和历史都和当前版本共用，所以能直接比行为。
不带 `-old` 启动则完全不受影响。

想两个版本**同时**跑着看，理论上可以用 `git worktree` 加 `COW_WEB_PORT` 起第二个实例，
但那会起第二套完整后端——调度器和 IM 渠道都会重复连接，配了飞书之类的会双份收消息。
除非你只跑 web 渠道，否则别这么做。

### 三处不能动的加载顺序

除了"core 在 views 之前"这个大方向，有三处是硬约束，改动会直接导致运行时报错：

1. **`views/agents.js` 必须排在 `chat/state.js` 之前**，尽管它在 `views/` 下。
   `chat/state.js` 顶层执行 `let sessionId = loadOrCreateSessionId()`，
   而 `activeSessionStorageKey()` 会把 `activeAgentId` 和 `defaultAgentId` 作比较，
   后者是 `views/agents.js` 里的 `let`。放到后面就是上面说的传递性 TDZ，
   会让整个对话视图失效。
   注意 `activeAgentId &&` 的短路：**只有选过智能体的用户才会触发**，
   全新配置下看不出问题。
2. **`core/auth.js` 必须排在 `chat/state.js` 之后**，所以它放在 core 层末尾。
   两者都包装了 `window.fetch`：`chat/state.js` 往 URL 上追加 `agent_id`，
   `core/auth.js` 检查 URL 前缀来决定 401 是否跳登录页。
   后装的在外层，这样 401 判断看到的是调用方原本的 URL。
3. **`boot.js` 必须排在 `workspace.js` 之前**，也就是 `console.js` 原来的位置。
   `applyI18n()` 里用 `typeof` 守卫探测 `relocalizeWorkspacePanel`，
   它一直是在 `workspace.js` 定义该函数之前运行的；放到后面会改变这个行为。
   （`typeof` 对未加载脚本里的**函数声明**是安全的，返回 `'undefined'`；
   但对 `let`/`const` 同样会抛 TDZ 错误，不要依赖它来探测变量。）

### 未拆分的两个文件

`workspace.js`（1296 行）和 `doc-editor.js`（258 行）保持原样，它们本来就是独立文件。
但它们的加载位置有约束：

- `doc-editor.js` **必须最先加载**，因为 `views/doc-viewers.js` 在顶层就调用
  `createDocEditor()` 构建 `memoryEditor` 与 `skillEditor`。
- `workspace.js` **必须最后加载**，它消费 `t`、`escapeHtml`、`renderMarkdown`、
  `showConfirmDialog`、`_wsToast`、`sessionId`、`activeAgentId` 等一批全局。

### 已知的遗留耦合

拆分只移动了代码，没有解耦。以下问题依然存在，改动时需要留意：

- `_wsToast` 定义在 `chat/workspace-selector.js`，但上下文弹窗、会话设置、技能页、
  `doc-editor.js` 和 `workspace.js` 都在用。它其实应该属于 `core/`。
- `chat/` 下的文件共享 `sessionId`、`chatInput`、`messagesDiv`、`_sessCfg` 等可变全局，
  拆分只是按职责分了文件，并没有把状态收拢起来。
- `chat/send.js` 里的 `startSSE()` 单个函数就有 637 行，是整个前端最大的一块。
- `chat/composer-input.js` 里输入框的 `keydown` 处理器有 92 行，同时负责斜杠命令导航和发送。
- `views/agents.js` 尾部混着三个记忆页用的辅助函数。
- `views/models.js`（1804 行）和 `core/i18n.js`（1744 行）仍然偏大。
  后者主要是翻译表本身，拆开意义不大。

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

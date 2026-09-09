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

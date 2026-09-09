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
